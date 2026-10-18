"""LARR-E Slack front end.

Connects over Socket Mode (outbound WebSocket), so it needs no public URL. That
suits hackathon Wi-Fi and a leader Pi behind NAT.

What it does:
* @mention the bot, DM it, or use /larr-e <request>: the agent answers in a thread.
  Replies in that thread continue the same conversation, with no further @mention needed.
* Plots the agent draws are uploaded into the thread.
* Every hardware action posts an Approve / Deny card and waits for a human click.
  Approvers can be restricted, and the two-person rule enforced, in config.py.
* Alerts from listener.py (MQTT larre/alerts/+) go to the alert channel with a
  "Plot" button. The listener's triage note (larre/triage/+) is threaded under its
  alert, and replies in that thread go to an agent that already knows the alert.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import anthropic
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk.errors import SlackApiError

from agent import LarreAgent
from linkwatch import LinkWatch
from openai_compat import LLMError
from config import Settings, settings as default_settings
from listener import build_client
from tools import PioreactorTools, ToolError, utc_timestamp

log = logging.getLogger("larre.slack")

SLACK_STYLE = """You are replying inside a Slack thread. Use Slack mrkdwn: *bold*, _italic_, `code`,
and "•" bullets. No Markdown headings or tables. Charts you plot are uploaded to the thread
automatically, so point to the chart instead of listing its points."""

ACTION_TITLES = {
    "dose": "Run a pump",
    "set_target_temperature": "Change temperature setpoint",
    "set_stirring_rpm": "Change stirring speed",
    "start_job": "Start a job",
    "stop_job": "Stop a job",
    "start_experiment": "Start an experiment",
    "stop_experiment": "Stop an experiment early",
}

HELP_TEXT = """*LARR-E*: talk to your Pioreactor cluster.
• `/larr-e status`: live telemetry (no AI)
• `/larr-e alerts`: recent anomaly alerts
• `/larr-e plot the last 10 minutes of OD against temperature`
• `/larr-e pump 1ml of media into worker1`: asks for approval before touching hardware
• `/larr-e run the vial at 37C and 180 rpm for 18 hours, update me every 2 hours`
• `/larr-e experiment`: the running experiment plan (no AI)
• `/larr-e units`: every unit in the cluster, real and digital twin (no AI)
Or @mention me in a channel, or DM me. Reply in my thread to keep the conversation going."""


def to_mrkdwn(text: str) -> str:
    """Patch the Markdown habits that render badly in Slack."""
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    text = re.sub(r"__(.+?)__", r"_\1_", text)
    text = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"<\2|\1>", text)
    return text


def format_args(args: dict[str, Any]) -> str:
    return ", ".join(f"{k}=`{v}`" for k, v in args.items() if v is not None) or "(none)"


def format_status(state: dict[str, Any]) -> str:
    if not state.pop("available", False):
        return f":warning: {state.get('note', 'No live telemetry.')}"
    lines = [f"{'unit':<10} {'OD':<16} {'temp °C':>8} {'growth':>8} {'RPM':>6}  last seen"]
    for unit, s in sorted(state.items()):
        od = ",".join(f"{k}={v['value']:.3f}" for k, v in sorted(s.get("od", {}).items())) or "—"
        get = lambda key, fmt: format(s[key]["value"], fmt) if key in s else "—"  # noqa: E731
        lines.append(
            f"{unit:<10} {od:<16} {get('temperature', '.2f'):>8} {get('growth_rate', '.3f'):>8} "
            f"{get('stirring', '.0f'):>6}  {s.get('last_seen', '—')[11:19]}"
        )
    return "*Live status*\n```" + "\n".join(lines) + "```"


@dataclass
class PendingApproval:
    id: str
    tool: str
    args: dict[str, Any]
    requester: str
    channel: str
    thread_ts: str | None
    ts: str = ""
    approved: bool = False
    decided_by: str | None = None
    event: threading.Event = field(default_factory=threading.Event)


@dataclass
class Proposal:
    """A setpoint change drawn on a plan card with the stepper buttons, not yet applied."""
    values: dict[str, float] = field(default_factory=dict)  # metric -> proposed setpoint
    proposers: set[str] = field(default_factory=set)  # everyone who adjusted it: none of them may Apply alone


# Stepper buttons on the plan card: action_id -> (metric, step).
STEPPERS = {
    "larre_step_temp_down": ("temperature", -1.0),
    "larre_step_temp_up": ("temperature", 1.0),
    "larre_step_rpm_down": ("stirring", -50.0),
    "larre_step_rpm_up": ("stirring", 50.0),
}
STEPPER_LABELS = {"larre_step_temp_down": "−1 °C", "larre_step_temp_up": "+1 °C",
                  "larre_step_rpm_down": "−50 rpm", "larre_step_rpm_up": "+50 rpm"}
SETPOINT_UNITS = {"temperature": "°C", "stirring": "rpm"}
SETPOINT_TOOLS = {"temperature": "set_target_temperature", "stirring": "set_stirring_rpm"}


@dataclass
class Conversation:
    channel: str
    thread_ts: str | None  # None means top-level replies (DMs)
    agent: LarreAgent | None = None
    requester: str = ""
    preamble: str = ""  # alert context, prepended to the next question
    status_ts: str | None = None
    tool_log: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)


class LarreBot:
    def __init__(self, cfg: Settings = default_settings, app: App | None = None, bot_user_id: str | None = None):
        self.cfg = cfg
        self.app = app or App(token=cfg.slack.bot_token)
        self.client = self.app.client
        self.bot_user_id = bot_user_id or self.client.auth_test()["user_id"]
        self.tools = PioreactorTools(cfg, render_terminal=False)
        self.pool = ThreadPoolExecutor(max_workers=cfg.slack.workers, thread_name_prefix="larre-slack")
        self.lock = threading.Lock()
        self.conversations: OrderedDict[tuple[str, str], Conversation] = OrderedDict()
        self.approvals: dict[str, PendingApproval] = {}
        self.alerts: dict[str, dict[str, Any]] = {}
        self.alert_threads: dict[str, tuple[str, str]] = {}
        self.proposals: dict[str, Proposal] = {}  # unit -> setpoint change waiting on the plan card
        self.plan_lock = threading.RLock()  # read-modify-write of saved plans (progress loop vs. button clicks)
        self.file_ready_delay_s = 2.0  # Slack may need a moment before an upload can appear in a message
        self._register_handlers()

    # ------------------------------------------------------------ conversations

    def conversation(self, channel: str, thread_ts: str | None) -> Conversation:
        key = (channel, thread_ts or "")
        with self.lock:
            conv = self.conversations.get(key)
            if conv:
                self.conversations.move_to_end(key)
                return conv
            conv = Conversation(channel, thread_ts)
            conv.agent = LarreAgent(
                "exec",
                cfg=self.cfg,
                confirm=lambda name, args, c=conv: self.request_approval(c, name, args),
                verbose=False,
                extra_system=SLACK_STYLE,
                on_tool_result=lambda *a, c=conv: self.on_tool_result(c, *a),
                render_terminal=False,
            )
            self.conversations[key] = conv
            while len(self.conversations) > self.cfg.slack.max_conversations:
                self.conversations.popitem(last=False)
            return conv

    def has_conversation(self, channel: str, thread_ts: str | None) -> bool:
        with self.lock:
            return (channel, thread_ts or "") in self.conversations

    def submit(self, fn: Callable[..., Any], *args: Any) -> None:
        def guarded() -> None:
            try:
                fn(*args)
            except Exception:
                log.exception("Background task %s failed", getattr(fn, "__name__", fn))

        self.pool.submit(guarded)

    def handle_prompt(self, channel: str, thread_ts: str | None, user: str, text: str) -> None:
        self.submit(self._answer, channel, thread_ts, user, text)

    def _answer(self, channel: str, thread_ts: str | None, user: str, text: str) -> None:
        conv = self.conversation(channel, thread_ts)
        with conv.lock:  # one question at a time per thread
            conv.requester = user
            conv.agent.requester = user
            conv.tool_log = []
            conv.status_ts = self.client.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text=":brain: working on it…"
            )["ts"]
            prompt = f"(Slack user <@{user}> asks)\n{text}"
            if conv.preamble:
                prompt, conv.preamble = conv.preamble + "\n\n" + prompt, ""
            try:
                answer = to_mrkdwn(conv.agent.ask(prompt))
            except anthropic.APIStatusError as exc:
                answer = f":warning: Claude API error {exc.status_code}: {exc.message}"
            except anthropic.APIConnectionError:
                answer = ":warning: Cannot reach the Claude API right now."
            except LLMError as exc:
                answer = f":warning: {exc}"
            footer = f"\n_tools: {' '.join(conv.tool_log)}_" if conv.tool_log else ""
            self.client.chat_update(channel=channel, ts=conv.status_ts, text=answer + footer)

    def on_tool_result(
        self, conv: Conversation, name: str, args: dict[str, Any], result: dict[str, Any], is_error: bool
    ) -> None:
        conv.tool_log.append(f"{'✗' if is_error else '✓'}`{name}`")
        if conv.status_ts:
            self.client.chat_update(
                channel=conv.channel,
                ts=conv.status_ts,
                text=":brain: working on it… " + " ".join(conv.tool_log),
            )
        if name == "plot_timeseries" and not is_error:
            self.upload_plot(conv.channel, conv.thread_ts, result)
        if name == "start_experiment" and not is_error and result.get("ok"):
            # Progress updates for this plan go to the thread that started it.
            plan = self.tools.plans.get(result["plan"]["unit"])
            if plan:
                self._save_plan(plan, slack_channel=conv.channel, slack_thread_ts=conv.thread_ts)
                # The live card goes up straight away, so the plan can be adjusted from Slack from t = 0.
                self.submit(self.post_plan_card, conv.channel, conv.thread_ts, plan, self.progress_text(plan))

    def upload_plot(self, channel: str, thread_ts: str | None, result: dict[str, Any], note: str = "") -> None:
        caption = f"`{result['unit']}`{note}"
        if "pearson_r" in result:
            caption += f" · Pearson r = {result['pearson_r']}"
        try:
            self.client.files_upload_v2(
                channel=channel,
                thread_ts=thread_ts,
                file=result["png_path"],
                title=f"LARR-E plot · {result['unit']}",
                initial_comment=caption,
            )
        except SlackApiError as exc:
            log.warning("Plot upload failed: %s", exc.response.get("error"))

    # ------------------------------------------------------------ approvals

    def request_approval(self, conv: Conversation, tool: str, args: dict[str, Any]) -> bool:
        """Called from the agent's worker thread; blocks until someone clicks or it times out."""
        pending = PendingApproval(uuid.uuid4().hex[:12], tool, args, conv.requester, conv.channel, conv.thread_ts)
        with self.lock:
            self.approvals[pending.id] = pending
        dry = " _(dry run: nothing will move)_" if self.tools.dry_run else ""
        who = (
            "any approver"
            if self.cfg.slack.allow_self_approval
            else "a second person (two-person rule)"
        )
        summary = f":warning: *{ACTION_TITLES.get(tool, tool)}*{dry}\n`{tool}` {format_args(args)}"
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": summary}},
            *self._plan_preview_blocks(tool, args),
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"Requested by <@{conv.requester}> · needs {who}"}],
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "larre_approve",
                        "style": "primary",
                        "text": {"type": "plain_text", "text": "Approve"},
                        "value": pending.id,
                    },
                    {
                        "type": "button",
                        "action_id": "larre_deny",
                        "style": "danger",
                        "text": {"type": "plain_text", "text": "Deny"},
                        "value": pending.id,
                    },
                    *self._plan_adjust_buttons(tool, args),
                ],
            },
        ]
        pending.ts = self.client.chat_postMessage(
            channel=conv.channel, thread_ts=conv.thread_ts, text=f"Approval needed: {tool}", blocks=blocks
        )["ts"]

        decided = pending.event.wait(self.cfg.slack.approval_timeout_s)
        with self.lock:
            self.approvals.pop(pending.id, None)
        if not decided:
            self.client.chat_update(
                channel=conv.channel,
                ts=pending.ts,
                text=f":hourglass: *Expired*: `{tool}` {format_args(args)} (no decision in "
                f"{self.cfg.slack.approval_timeout_s:g}s; nothing was changed)",
                blocks=[],
            )
        self.audit(pending, "approved" if pending.approved else ("denied" if decided else "expired"))
        return pending.approved

    def _plan_adjust_buttons(self, tool: str, args: dict[str, Any]) -> list[dict[str, Any]]:
        if tool != "start_experiment":
            return []
        try:
            unit = self.tools.resolve_unit(args.get("unit"))
        except ToolError:
            return []
        button = self.adjust_button(unit)
        return [button] if button else []

    def _plan_preview_blocks(self, tool: str, args: dict[str, Any]) -> list[dict[str, Any]]:
        """For start_experiment, show the plan and the exact profile the approver is agreeing to."""
        if tool != "start_experiment":
            return []
        try:
            preview = self.tools.plan_experiment(**args)
        except (ToolError, TypeError) as exc:
            return [{"type": "section", "text": {"type": "mrkdwn", "text": f":x: Plan is invalid: {exc}"}}]
        plan = preview["plan"]
        text = f"*{plan['summary']}*\nPioreactor profile (it runs the schedule and stops the jobs at the end):\n```{preview['profile_yaml']}```"
        if preview["conflict"]:
            text += f"\n:warning: Already running: {preview['conflict']}"
        return [{"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}}]

    def decide(self, approval_id: str, user: str, approve: bool) -> str | None:
        """Apply a button click. Returns an error to show the clicker privately, or None on success."""
        with self.lock:
            pending = self.approvals.get(approval_id)
            if pending is None or pending.event.is_set():
                return "This request was already handled or has expired."
            approvers = self.cfg.slack.approvers
            if approvers and user not in approvers:
                return "You are not on the approver list for hardware actions."
            if approve and user == pending.requester and not self.cfg.slack.allow_self_approval:
                return "Two-person rule: someone other than the requester must approve this."
            pending.approved, pending.decided_by = approve, user
            pending.event.set()
        verdict = ":white_check_mark: *Approved*" if approve else ":no_entry: *Denied*"
        self.client.chat_update(
            channel=pending.channel,
            ts=pending.ts,
            text=f"{verdict} by <@{user}>: `{pending.tool}` {format_args(pending.args)}"
            f" (requested by <@{pending.requester}>)",
            blocks=[],
        )
        return None

    def audit(self, pending: PendingApproval, decision: str) -> None:
        record = {
            "at": utc_timestamp(),
            "source": "slack",
            "tool": pending.tool,
            "args": pending.args,
            "requester": pending.requester,
            "decision": decision,
            "decided_by": pending.decided_by,
            "channel": pending.channel,
            "dry_run": self.tools.dry_run,
        }
        with self.cfg.output.audit_path.open("a") as f:
            f.write(json.dumps(record) + "\n")

    # ------------------------------------------------------------ alerts

    def post_alert(self, alert: dict[str, Any]) -> None:
        channel = self.cfg.slack.alert_channel
        if not channel:
            log.warning("Alert received but LARRE_SLACK_ALERT_CHANNEL is not set")
            return
        emoji = ":rotating_light:" if alert["severity"] == "critical" else ":warning:"
        text = f"{emoji} *{alert['severity'].upper()}* · `{alert['unit']}` · {alert['metric']}\n{alert['message']}"
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": text}},
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"experiment `{alert['experiment']}` · value {alert['value']:.3f} · "
                        f"{alert['reading_timestamp']} · reply in thread to act",
                    }
                ],
            },
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "larre_plot_alert",
                        "text": {"type": "plain_text", "text": "📈 Plot last 15 min"},
                        "value": alert["id"],
                    }
                ],
            },
        ]
        ts = self.client.chat_postMessage(channel=channel, text=text, blocks=blocks)["ts"]
        with self.lock:
            self.alerts[alert["id"]] = alert
            self.alert_threads[alert["id"]] = (channel, ts)
        conv = self.conversation(channel, ts)
        conv.agent.default_unit = alert["unit"]
        conv.preamble = f"This thread is about an anomaly alert:\n{json.dumps(alert, indent=2)}"

    def post_triage(self, payload: dict[str, Any]) -> None:
        with self.lock:
            target = self.alert_threads.get(payload.get("alert_id", ""))
        note = f":brain: *Triage*\n{to_mrkdwn(payload['note'])}"
        if target is None:
            if self.cfg.slack.alert_channel:
                self.client.chat_postMessage(
                    channel=self.cfg.slack.alert_channel, text=f"`{payload['unit']}` {note}"
                )
            return
        channel, ts = target
        self.client.chat_postMessage(channel=channel, thread_ts=ts, text=note)
        conv = self.conversation(channel, ts)
        conv.preamble += f"\n\nAutomated triage of the alert:\n{payload['note']}"

    def plot_alert(self, alert_id: str, user: str) -> None:
        with self.lock:
            alert, target = self.alerts.get(alert_id), self.alert_threads.get(alert_id)
        if not alert or not target:
            return
        channel, ts = target
        metric = "od" if alert["metric"].startswith("od") else alert["metric"]
        metrics = [metric, "od" if metric == "temperature" else "temperature"]
        try:
            result = self.tools.plot_timeseries(metrics, alert["unit"], minutes=15)
        except ToolError as exc:
            self.client.chat_postMessage(channel=channel, thread_ts=ts, text=f":warning: Cannot plot: {exc}")
            return
        self.upload_plot(channel, ts, result, note=f" · requested by <@{user}>")

    # ------------------------------------------------------------ experiment progress

    def _save_plan(self, plan: Any, **fields: Any) -> None:
        """Set fields on `plan` and on the saved copy, re-read under a lock so that a change saved meanwhile
        (e.g. an approved setpoint) isn't overwritten by a stale copy. A newer plan for the unit is left alone."""
        for k, v in fields.items():
            setattr(plan, k, v)
        with self.plan_lock:
            saved = self.tools.plans.get(plan.unit)
            if saved and (saved.name, saved.started_at) == (plan.name, plan.started_at):
                for k, v in fields.items():
                    setattr(saved, k, v)
                self.tools.plans.put(saved)

    def _update_target(self, plan: Any) -> tuple[str, str | None] | None:
        if plan.slack_channel:
            return plan.slack_channel, plan.slack_thread_ts
        channel = self.cfg.plans.update_channel or self.cfg.slack.alert_channel
        return (channel, None) if channel else None

    def progress_text(self, plan: Any, final: bool = False) -> str:
        elapsed = min(plan.elapsed_h() or 0.0, plan.duration_h)
        if final:
            head = f":checkered_flag: *{plan.name}* finished after {plan.duration_h:g} h. Pioreactor stopped its jobs on schedule."
        else:
            head = f":test_tube: *{plan.name}*: {elapsed:.1f} h of {plan.duration_h:g} h ({plan.duration_h - elapsed:.1f} h left)"
        live = self.tools.get_live_status(plan.unit) if plan.unit in self.tools._live_state() else {}
        s = live.get(plan.unit, {})
        bits = []
        if "temperature" in s:
            tgt = f" (target {plan.target('temperature'):g})" if plan.temperature_c is not None else ""
            bits.append(f"temp {s['temperature']['value']:.1f} °C{tgt}")
        if "stirring" in s:
            tgt = f" (target {plan.target('stirring'):g})" if plan.rpm is not None else ""
            bits.append(f"stirring {s['stirring']['value']:.0f} rpm{tgt}")
        if s.get("od"):
            bits.append("OD " + ", ".join(f"{v['value']:.3f}" for _, v in sorted(s["od"].items())))
        if "growth_rate" in s:
            bits.append(f"growth {s['growth_rate']['value']:.3f} h⁻¹")
        return head + ("\n" + " · ".join(bits) if bits else "\n_(no live telemetry: is `larr-e listen` running?)_")

    def adjust_button(self, unit: str | None) -> dict[str, Any] | None:
        """A link to the /plan page for this unit, if the API's address and token are configured."""
        base, token = self.cfg.api.public_url, self.cfg.api.token
        if not (base and token and unit):
            return None
        query = urlencode({"k": token, "unit": unit})
        return {"type": "button", "action_id": "larre_adjust", "url": f"{base.rstrip('/')}/plan?{query}",
                "text": {"type": "plain_text", "text": "Adjust in LARR-E"}}

    def plan_chart(self, plan: Any, final: bool = False, proposed: dict[str, float] | None = None) -> tuple[str, str] | None:
        """Draw the plan chart (a replay GIF for the final message). Returns (path, caption) or None."""
        try:
            result = self.tools.replay_plan_gif(plan) if final else self.tools.plot_plan(plan, proposed)
        except ToolError:
            return None  # the plan sets no temperature or stirring speed
        off = [f"{m} off target {v:g} min" for m, v in result["off_target_min"].items() if v]
        return result.get("gif_path") or result["png_path"], "plan vs measured: " + (", ".join(off) or "on target throughout")

    def card_actions(self, plan: Any, final: bool = False) -> list[dict[str, Any]]:
        """The buttons under the chart: steppers for each setpoint the plan controls (they only propose),
        Apply / Cancel while a change is proposed, and the link to the /plan page."""
        if final or plan.status != "running":
            return []
        button = lambda action_id, label, **kw: {  # noqa: E731
            "type": "button", "action_id": action_id, "value": plan.unit,
            "text": {"type": "plain_text", "text": label}, **kw}
        steppers = [button(a, STEPPER_LABELS[a]) for a, (metric, _) in STEPPERS.items() if plan.target(metric) is not None]
        rows = [{"type": "actions", "block_id": "larre_steppers", "elements": steppers}] if steppers else []
        decide = []
        if plan.unit in self.proposals:
            decide = [button("larre_apply", "Apply", style="primary"), button("larre_cancel", "Cancel")]
        link = self.adjust_button(plan.unit)
        if decide or link:
            rows.append({"type": "actions", "block_id": "larre_decide", "elements": decide + ([link] if link else [])})
        return rows

    def proposed_values(self, unit: str) -> dict[str, float] | None:
        with self.lock:
            proposal = self.proposals.get(unit)
            return dict(proposal.values) if proposal else None

    def proposal_note(self, plan: Any) -> str:
        with self.lock:
            proposal = self.proposals.get(plan.unit)
            if not proposal:
                return ""
            values, proposers = dict(proposal.values), sorted(proposal.proposers)
        changes = ", ".join(f"{m} {v:g} {SETPOINT_UNITS[m]} (now {plan.target(m):g})" for m, v in values.items())
        who = ("anyone on the approver list" if self.cfg.slack.allow_self_approval
               else "someone other than " + ", ".join(f"<@{u}>" for u in proposers) + " (two-person rule)")
        dry = " _(dry run: nothing will move)_" if self.tools.dry_run else ""
        return f":pencil2: *Proposed change:* {changes}. Nothing has changed yet: *Apply* needs {who}.{dry}"

    def refresh_card(self, plan: Any, note: str = "") -> None:
        """Redraw the plan's live card (after a button click)."""
        target = self._update_target(plan)
        if target:
            self.post_plan_card(target[0], target[1], plan, self.progress_text(plan), note=note)

    def _running_plan(self, unit: str) -> tuple[Any, str | None]:
        plan = self.tools.plans.get(unit)
        if plan is None:
            return None, f"No plan for `{unit}`."
        if plan.status != "running":
            with self.lock:
                self.proposals.pop(unit, None)
            self.refresh_card(plan)  # take the buttons off
            return None, f"The plan on `{unit}` is {plan.status}; there is nothing to adjust."
        return plan, None

    def step(self, unit: str, user: str, action_id: str) -> str | None:
        """A stepper click: move the proposed setpoint one step and redraw. Changes nothing on the reactor.
        Returns an error to show the clicker privately, or None."""
        plan, error = self._running_plan(unit)
        if error:
            return error
        metric, delta = STEPPERS[action_id]
        if plan.target(metric) is None:
            return f"This plan doesn't control {metric}."
        lim = self.cfg.safety
        lo, hi = ((lim.min_temperature_c, lim.max_temperature_c) if metric == "temperature"
                  else (0.0, float(lim.max_stirring_rpm)))
        current = plan.target(metric)
        with self.lock:
            proposal = self.proposals.setdefault(unit, Proposal())
            new = min(hi, max(lo, proposal.values.get(metric, current) + delta))
            proposal.proposers.add(user)
            if new == current:
                proposal.values.pop(metric, None)
            else:
                proposal.values[metric] = new
            if not proposal.values:
                del self.proposals[unit]
        self.refresh_card(plan)
        return None

    def cancel(self, unit: str, user: str) -> str | None:
        with self.lock:
            dropped = self.proposals.pop(unit, None)
        plan = self.tools.plans.get(unit)
        if plan:
            self.refresh_card(plan, note=f"_<@{user}> cancelled the proposed change._" if dropped else "")
        return None

    def apply(self, unit: str, user: str) -> str | None:
        """Apply is the approval: same approver list and two-person rule as the Approve button. Sends each
        proposed setpoint to the reactor, then records it on the saved plan. Returns an error or None."""
        plan, error = self._running_plan(unit)
        if error:
            return error
        with self.lock:
            proposal = self.proposals.get(unit)
            if proposal is None:
                return "Nothing is proposed: use the − / + buttons first."
            approvers = self.cfg.slack.approvers
            if approvers and user not in approvers:
                return "You are not on the approver list for hardware actions."
            if user in proposal.proposers and not self.cfg.slack.allow_self_approval:
                return "Two-person rule: someone who didn't adjust this proposal must apply it."
            del self.proposals[unit]  # taken: a second click finds nothing to apply
        done, failed = [], {}
        for metric, value in proposal.values.items():
            tool, was = SETPOINT_TOOLS[metric], plan.target(metric)
            args = {"unit": unit, ("celsius" if metric == "temperature" else "rpm"): value}
            try:
                getattr(self.tools, tool)(**args)
            except ToolError as exc:
                failed[metric] = str(exc)
                continue
            self.audit(PendingApproval("card", tool, args, ",".join(sorted(proposal.proposers)),
                                       plan.slack_channel or "", plan.slack_thread_ts, decided_by=user), "approved")
            with self.plan_lock:
                saved = self.tools.plans.get(unit)
                if saved and (saved.name, saved.started_at) == (plan.name, plan.started_at):
                    saved.set_target(metric, value, by=user)
                    self.tools.plans.put(saved)
                    plan = saved
            done.append(f"{metric} {was:g} → {value:g} {SETPOINT_UNITS[metric]}")
        if failed:
            with self.lock:  # leave what didn't go through on the card, to retry or cancel
                self.proposals.setdefault(unit, Proposal(proposers=set(proposal.proposers))).values.update(
                    {m: proposal.values[m] for m in failed})
        channel, thread_ts = self._update_target(plan) or (None, None)
        if done and channel:
            by = ", ".join(f"<@{u}>" for u in sorted(proposal.proposers))
            dry = " _(dry run: nothing moved)_" if self.tools.dry_run else ""
            self.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=(
                f":white_check_mark: <@{user}> applied {', '.join(done)} on `{unit}` (proposed by {by}){dry}"))
        self.refresh_card(plan)
        if failed:
            return "Not applied: " + "; ".join(f"{m}: {e}" for m, e in failed.items())
        return None

    def _send_card(self, channel: str, thread_ts: str | None, plan: Any, text: str,
                   blocks: list[dict[str, Any]], final: bool) -> None:
        """Edit the plan's live card in place, or post it (and remember it) if there is none yet or it was
        deleted. The final message is always a new post, so that it notifies."""
        if not final and plan.slack_card_ts:
            try:
                self.client.chat_update(channel=channel, ts=plan.slack_card_ts, text=text, blocks=blocks)
                return
            except SlackApiError as exc:
                if exc.response.get("error") not in ("message_not_found", "cant_update_message", "edit_window_closed"):
                    raise
                log.info("Live card for %s is gone (%s); posting a new one", plan.unit, exc.response.get("error"))
        ts = self.client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text, blocks=blocks)["ts"]
        if not final:
            self._save_plan(plan, slack_card_ts=ts)

    def post_plan_card(self, channel: str, thread_ts: str | None, plan: Any, text: str, final: bool = False,
                       note: str = "") -> None:
        """The plan's live card: the update text, the plan chart inside it, and buttons under the chart.
        Progress updates and button clicks edit this one message in place. The chart is uploaded privately
        and shown through an image block, under a new block_id each time (Slack can keep showing the old
        image under a reused one). If Slack refuses the image (e.g. the upload is still processing), retry
        once, then show the text and share the chart in the thread."""
        proposed = None if final else self.proposed_values(plan.unit)
        chart = self.plan_chart(plan, final, proposed)
        actions = self.card_actions(plan, final)
        if chart:
            text = f"{text}\n_{chart[1]}_"
        note = "\n".join(n for n in (None if final else self.proposal_note(plan), note) if n)
        if note:
            text = f"{text}\n{note}"
        section = [{"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}}]
        if chart:
            title = f"LARR-E plan vs measured · {plan.unit}"
            try:
                file_id = self.client.files_upload_v2(file=chart[0], title=title)["file"]["id"]
                image = {"type": "image", "block_id": f"chart-{file_id}", "slack_file": {"id": file_id},
                         "alt_text": f"{plan.name}: {chart[1]}"}
                for attempt in range(2):
                    try:
                        self._send_card(channel, thread_ts, plan, text, section + [image] + actions, final)
                        return
                    except SlackApiError:
                        if attempt == 0:
                            time.sleep(self.file_ready_delay_s)
            except (SlackApiError, KeyError, TypeError) as exc:
                log.warning("Plan card with inline chart failed (%s); sharing the chart in the thread", exc)
        self._send_card(channel, thread_ts, plan, text, section + actions, final)
        if chart:
            try:
                self.client.files_upload_v2(channel=channel, thread_ts=thread_ts, file=chart[0],
                                            title=f"LARR-E plan vs measured · {plan.unit}")
            except SlackApiError as exc:
                log.warning("Plan chart upload failed: %s", exc.response.get("error"))

    def post_progress(self, plan: Any, final: bool = False) -> None:
        target = self._update_target(plan)
        if not target:
            log.warning("Progress update for %s due, but no Slack channel is known", plan.unit)
            return
        channel, thread_ts = target
        self.post_plan_card(channel, thread_ts, plan, self.progress_text(plan, final), final)
        minutes = max(1.0, min(plan.elapsed_h() or 0.0, plan.duration_h) * 60)
        for metrics in (["od", "growth_rate"], ["od", "temperature"]):
            try:
                result = self.tools.plot_timeseries(metrics, plan.unit, minutes=minutes)
            except ToolError:
                continue  # e.g. no growth rate yet early on; fall back to OD vs temperature
            self.upload_plot(channel, thread_ts, result, note=f" · {plan.name}")
            break

    def check_plans(self) -> None:
        """One pass: post due updates and the end-of-experiment message. Runs no LLM."""
        for plan in self.tools.plans.all():
            if plan.status == "finished" and not plan.final_posted:
                self.post_progress(plan, final=True)
                self._save_plan(plan, final_posted=True)
            elif plan.update_due():
                self.post_progress(plan)
                self._save_plan(plan, last_update_at=utc_timestamp())

    def start_progress_loop(self) -> None:
        def loop() -> None:
            while True:
                try:
                    self.check_plans()
                except Exception:
                    log.exception("Progress check failed")
                time.sleep(self.cfg.plans.check_every_s)

        threading.Thread(target=loop, name="larre-progress", daemon=True).start()

    @staticmethod
    def _duration(seconds: float) -> str:
        return f"{round(seconds)} s" if seconds < 90 else f"{round(seconds / 60)} min"

    def post_link_down(self, seconds: float) -> None:
        """The broker runs on the Pi, so the listener's alerts can't arrive: post this directly."""
        channel = self.cfg.slack.alert_channel
        if not channel:
            log.warning("Lost contact with the Pioreactor, but LARRE_SLACK_ALERT_CHANNEL is not set")
            return
        self.client.chat_postMessage(channel=channel, text=(
            f":electric_plug: *Lost contact with the Pioreactor* (`{self.cfg.mqtt.host}`) for "
            f"{self._duration(seconds)}. LARR-E can't see or control it right now. If the Pi still has power, "
            "its running jobs carry on by themselves. Check its power and Wi-Fi."))

    def post_link_up(self, seconds: float) -> None:
        channel = self.cfg.slack.alert_channel
        if not channel:
            return
        self.client.chat_postMessage(channel=channel, text=(
            f":white_check_mark: *Back in contact with the Pioreactor* after {self._duration(seconds)}. "
            "Readings from the gap weren't recorded."))

    def start_mqtt(self) -> None:
        m = self.cfg.mqtt
        client = build_client(self.cfg, role="slackbot")
        alert_topic, triage_topic = f"{m.alert_topic_prefix}/+", f"{m.triage_topic_prefix}/+"
        self.link = LinkWatch(m.link_grace_s, on_down=lambda s: self.submit(self.post_link_down, s),
                              on_up=lambda s: self.submit(self.post_link_up, s))
        self.link.start()

        def on_connect(client, userdata, flags, reason_code, properties):
            if not reason_code.is_failure:
                self.link.connected()
                client.subscribe([(alert_topic, 1), (triage_topic, 1)])
                log.info("Subscribed to %s and %s", alert_topic, triage_topic)

        def on_disconnect(client, userdata, flags, reason_code, properties):
            log.warning("Lost the MQTT connection to %s (%s); reconnecting", m.host, reason_code)
            self.link.disconnected()

        def on_message(client, userdata, msg):
            try:
                payload = json.loads(msg.payload)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return
            if msg.topic.startswith(m.alert_topic_prefix + "/"):
                self.submit(self.post_alert, payload)
            else:
                self.submit(self.post_triage, payload)

        client.on_connect, client.on_disconnect, client.on_message = on_connect, on_disconnect, on_message
        client.connect_async(m.host, m.port, m.keepalive)
        client.loop_start()

    # ------------------------------------------------------------ Slack handlers

    def _strip_mention(self, text: str) -> str:
        return text.replace(f"<@{self.bot_user_id}>", "").strip()

    def _register_handlers(self) -> None:
        app = self.app

        @app.event("app_mention")
        def on_mention(event: dict[str, Any]) -> None:
            text = self._strip_mention(event.get("text", ""))
            thread_ts = event.get("thread_ts") or event["ts"]
            if not text:
                self.client.chat_postMessage(channel=event["channel"], thread_ts=thread_ts, text=HELP_TEXT)
                return
            self.handle_prompt(event["channel"], thread_ts, event["user"], text)

        @app.event("message")
        def on_message(event: dict[str, Any]) -> None:
            if event.get("subtype") or event.get("bot_id") or "user" not in event:
                return  # edits, joins, and bot messages (our own included)
            text = event.get("text", "")
            if f"<@{self.bot_user_id}>" in text:
                return  # app_mention handles it
            channel = event["channel"]
            if event.get("channel_type") == "im":
                self.handle_prompt(channel, event.get("thread_ts"), event["user"], text)
            elif event.get("thread_ts") and self.has_conversation(channel, event["thread_ts"]):
                self.handle_prompt(channel, event["thread_ts"], event["user"], text)

        @app.command("/larr-e")
        def on_command(ack: Callable, command: dict[str, Any], respond: Callable) -> None:
            text = command.get("text", "").strip()
            if text.lower() in {"", "help"}:
                ack(HELP_TEXT)
                return
            if text.lower() == "status":
                ack()
                respond(text=format_status(self.tools.get_live_status()), response_type="in_channel")
                return
            if text.lower() in {"units", "cluster"}:
                ack()
                rows = self.tools.list_units()["units"]
                lines = [
                    f"• `{u['unit']}` {':large_green_circle:' if u['streaming'] else ':white_circle:'} "
                    f"{u['experiment'] or '—'} · {u['plan'] or 'no plan'} · _{u['leader']}_"
                    for u in rows
                ] or ["No units known yet."]
                respond(text="*Cluster*\n" + "\n".join(lines), response_type="in_channel")
                return
            if text.lower() in {"experiment", "plan"}:
                ack()
                plans = [p for p in self.tools.plans.all() if p.status == "running"]
                msg = "\n\n".join(self.progress_text(p) for p in plans) or "No experiment is running."
                respond(text=msg, response_type="in_channel")
                return
            if text.lower() == "alerts":
                ack()
                alerts = self.tools.list_recent_alerts(8)["alerts"]
                lines = [f"• `{a['unit']}` {a['metric']}: {a['message']}" for a in alerts] or ["No alerts yet."]
                respond(text="*Recent alerts*\n" + "\n".join(lines), response_type="in_channel")
                return
            ack()
            try:
                parent = self.client.chat_postMessage(
                    channel=command["channel_id"], text=f"<@{command['user_id']}> asked LARR-E: _{text}_"
                )
            except SlackApiError as exc:
                respond(
                    text=f"I can't post here ({exc.response.get('error')}). Invite me with `/invite @LARR-E`.",
                    response_type="ephemeral",
                )
                return
            self.handle_prompt(parent["channel"], parent["ts"], command["user_id"], text)

        def on_decision(approve: bool) -> Callable[..., None]:
            def handler(ack: Callable, body: dict[str, Any]) -> None:
                ack()
                user = body["user"]["id"]
                error = self.decide(body["actions"][0]["value"], user, approve)
                if error:
                    self.client.chat_postEphemeral(channel=body["channel"]["id"], user=user, text=error)

            return handler

        app.action("larre_approve")(on_decision(True))
        app.action("larre_deny")(on_decision(False))

        @app.action("larre_adjust")
        def on_adjust(ack: Callable, body: dict[str, Any]) -> None:
            ack()  # a link button: Slack opens the URL; the click still needs acknowledging

        def on_card(fn: Callable[..., str | None], pass_action: bool = False) -> Callable[..., None]:
            def handler(ack: Callable, body: dict[str, Any]) -> None:
                ack()
                user, action = body["user"]["id"], body["actions"][0]

                def run_it() -> None:
                    args = (action["value"], user) + ((action["action_id"],) if pass_action else ())
                    error = fn(*args)
                    if error:
                        self.client.chat_postEphemeral(channel=body["channel"]["id"], user=user, text=error)

                self.submit(run_it)

            return handler

        app.action(re.compile(r"^larre_step_"))(on_card(self.step, pass_action=True))
        app.action("larre_apply")(on_card(self.apply))
        app.action("larre_cancel")(on_card(self.cancel))

        @app.action("larre_plot_alert")
        def on_plot(ack: Callable, body: dict[str, Any]) -> None:
            ack()
            self.submit(self.plot_alert, body["actions"][0]["value"], body["user"]["id"])


def run(cfg: Settings = default_settings) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if not (cfg.slack.bot_token and cfg.slack.app_token):
        sys.exit("Set SLACK_BOT_TOKEN (xoxb-…) and SLACK_APP_TOKEN (xapp-…). See README section 4.")
    cfg.ensure_dirs()
    bot = LarreBot(cfg)
    bot.start_mqtt()
    bot.start_progress_loop()
    log.info("LARR-E bot online as %s (dry_run=%s)", bot.bot_user_id, bot.tools.dry_run)
    SocketModeHandler(bot.app, cfg.slack.app_token).start()


if __name__ == "__main__":
    run()
