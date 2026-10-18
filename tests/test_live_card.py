"""Option A: one live plan card per plan in Slack, with stepper buttons that propose a new setpoint,
draw it on the chart, and change it only after Apply (the approval). No Slack, hardware or Claude API needed."""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from experiment import ExperimentPlan, PlanStore  # noqa: E402
from test_experiment_plans import make_settings  # noqa: E402
from test_slack_plan import MIN, running_plan, write_history  # noqa: E402

T0 = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)


def plan_at_t0(**kw) -> ExperimentPlan:
    return ExperimentPlan(name="overnight", unit="pio1", experiment="demo", duration_h=18, temperature_c=30, rpm=300,
                          status="running", started_at=T0.isoformat(), **kw)


# ------------------------------------------------------------------ piece 1: setpoint history on the plan


def test_a_setpoint_change_is_recorded_and_becomes_the_current_target():
    plan = plan_at_t0()
    plan.set_target("temperature", 31, by="U2", now=T0 + timedelta(hours=1))
    assert plan.setpoints == [{"at": "2026-10-17T13:00:00Z", "metric": "temperature", "value": 31.0, "by": "U2"}]
    assert plan.target("temperature") == 31
    assert plan.target("stirring") == 300  # untouched
    assert plan.temperature_c == 30  # what was asked for at the start stays on the plan (and in the profile)


def test_target_at_a_time_follows_the_history():
    plan = plan_at_t0()
    plan.set_target("stirring", 350, now=T0 + timedelta(hours=1))
    plan.set_target("stirring", 400, now=T0 + timedelta(hours=2))
    assert plan.target("stirring", seconds_in=30 * MIN) == 300
    assert plan.target("stirring", seconds_in=90 * MIN) == 350
    assert plan.target("stirring", seconds_in=150 * MIN) == 400
    assert plan.target_steps("stirring") == [(0.0, 300.0), (60.0, 350.0), (120.0, 400.0)]
    assert plan.target_steps("temperature") == [(0.0, 30.0)]


def test_alert_triage_expects_the_current_setpoint():
    plan = plan_at_t0()
    plan.set_target("temperature", 33, now=T0 + timedelta(hours=1))
    plan.set_target("stirring", 250, now=T0 + timedelta(hours=1))
    state = plan.expected_state(T0 + timedelta(hours=2))
    assert state["target_temperature_c"] == 33 and state["target_rpm"] == 250


def test_off_target_uses_the_setpoint_in_force_and_forgives_warm_up_after_a_change():
    plan = plan_at_t0()
    plan.set_target("temperature", 33, now=T0 + timedelta(hours=1))
    assert plan.off_target("temperature", 50 * MIN, 33.0)  # before the change, 30 was the target
    assert not plan.off_target("temperature", 50 * MIN, 30.2)
    assert not plan.off_target("temperature", 65 * MIN, 30.2)  # heater still warming to the new target
    assert plan.off_target("temperature", 75 * MIN, 30.2)
    assert not plan.off_target("temperature", 75 * MIN, 32.8)


def test_setpoint_history_survives_the_store(tmp_path):
    store = PlanStore(tmp_path / "plans.json")
    plan = plan_at_t0()
    plan.duration_h = 100_000  # still running whenever this test runs
    plan.set_target("stirring", 350, by="U2", now=T0 + timedelta(hours=1))
    store.put(plan)
    again = store.get("pio1")
    assert again.target("stirring") == 350 and again.setpoints == plan.setpoints


# ------------------------------------------------------------------ piece 2: the chart draws steps and a proposal


def drawn(tmp_path, plan, proposed=None):
    """Draw the plan chart and return {metric: {gid: line}} for the lines tagged 'target' and 'proposed'."""
    from matplotlib.figure import Figure

    from tools import PioreactorTools

    tools = PioreactorTools(make_settings(tmp_path), render_terminal=False)
    start, series = tools._plan_series(plan)
    fig = Figure(figsize=(10, 6))
    tools._draw_plan(fig, plan, series, (time.time() - start) / 60, proposed=proposed)
    out = {}
    for ax, metric in zip(fig.axes, series):
        out[metric] = {line.get_gid(): line for line in ax.get_lines() if line.get_gid()}
    return out


def test_target_line_steps_at_each_approved_change(tmp_path):
    start = time.time() - 20 * MIN
    write_history(tmp_path, start)
    plan = running_plan(start)
    plan.set_target("stirring", 350, now=datetime.fromtimestamp(start + 15 * MIN, timezone.utc))
    lines = drawn(tmp_path, plan)
    stir = lines["stirring"]["target"]
    assert list(stir.get_ydata())[0] == 300 and list(stir.get_ydata())[-1] == 350
    assert stir.get_drawstyle() == "steps-post"
    assert "proposed" not in lines["stirring"]


def test_a_proposal_is_a_second_dashed_line_from_now_to_the_end(tmp_path):
    start = time.time() - 20 * MIN
    write_history(tmp_path, start)
    plan = running_plan(start)
    lines = drawn(tmp_path, plan, proposed={"temperature": 31})
    prop = lines["temperature"]["proposed"]
    assert set(prop.get_ydata()) == {31}
    xs = list(prop.get_xdata())
    assert 19 <= xs[0] <= 21 and xs[-1] == plan.duration_h * 60  # from now to the planned end
    assert prop.get_linestyle() == "--"
    assert prop.get_color() != lines["temperature"]["target"].get_color()
    assert "proposed" not in lines["stirring"]


def test_plot_plan_passes_the_proposal_through(tmp_path):
    from tools import PioreactorTools

    start = time.time() - 20 * MIN
    write_history(tmp_path, start)
    tools = PioreactorTools(make_settings(tmp_path), render_terminal=False)
    out = tools.plot_plan(running_plan(start), proposed={"stirring": 350})
    assert Path(out["png_path"]).stat().st_size > 1000


# ------------------------------------------------------------------ piece 3: one live card, edited in place

from test_slack_plan import CardSlack, bot_with  # noqa: E402


class LiveSlack(CardSlack):
    """CardSlack plus chat_update and ephemeral messages; `lost_ts` makes updates to those messages fail."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.updates, self.ephemeral, self.lost_ts = [], [], set()

    def chat_update(self, **kw):
        if kw["ts"] in self.lost_ts:
            from slack_sdk.errors import SlackApiError
            raise SlackApiError("message_not_found", {"ok": False, "error": "message_not_found"})
        self.updates.append(kw)
        return {"ts": kw["ts"]}

    def chat_postEphemeral(self, **kw):
        self.ephemeral.append(kw)


def live_bot(tmp_path, cfg=None, minutes_ago=20, **plan_kw):
    start = time.time() - minutes_ago * MIN
    write_history(tmp_path, start)
    bot = bot_with(cfg or make_settings(tmp_path), LiveSlack())
    plan = running_plan(start, slack_channel="C1", slack_thread_ts="1.1", **plan_kw)
    bot.tools.plans.put(plan)
    return bot


def image_block(msg):
    (img,) = [b for b in msg["blocks"] if b["type"] == "image"]
    return img


def test_the_second_update_edits_the_first_card_in_place(tmp_path):
    bot = live_bot(tmp_path)
    bot.post_progress(bot.tools.plans.get("pio1"))
    card_ts = bot.tools.plans.get("pio1").slack_card_ts
    assert card_ts == bot.client.posts[0].get("ts", "1") and len(bot.client.posts) == 1
    bot.post_progress(bot.tools.plans.get("pio1"))
    assert len(bot.client.posts) == 1  # no second card
    (update,) = bot.client.updates
    assert update["channel"] == "C1" and update["ts"] == card_ts
    first, second = image_block(bot.client.posts[0]), image_block(update)
    assert first["block_id"] != second["block_id"]  # Slack keeps showing the old image under a reused block_id
    assert second["slack_file"]["id"] != first["slack_file"]["id"]


def test_a_deleted_card_is_replaced_by_a_new_one(tmp_path):
    bot = live_bot(tmp_path)
    bot.post_progress(bot.tools.plans.get("pio1"))
    bot.client.lost_ts.add(bot.tools.plans.get("pio1").slack_card_ts)
    bot.post_progress(bot.tools.plans.get("pio1"))
    assert len(bot.client.posts) == 2
    assert bot.tools.plans.get("pio1").slack_card_ts == "2"


def test_the_card_appears_as_soon_as_the_plan_starts(tmp_path):
    from slack_bot import Conversation

    bot = live_bot(tmp_path, minutes_ago=0)
    conv = Conversation(channel="C9", thread_ts="9.9", requester="U1")
    bot.submit = lambda fn, *a: fn(*a)
    bot.on_tool_result(conv, "start_experiment", {}, {"ok": True, "plan": {"unit": "pio1"}}, False)
    assert bot.client.posts and bot.client.posts[0]["channel"] == "C9" and bot.client.posts[0]["thread_ts"] == "9.9"
    assert bot.tools.plans.get("pio1").slack_card_ts == "1"


def test_card_text_shows_the_current_setpoint(tmp_path):
    import json

    bot = live_bot(tmp_path)
    state_path = bot.cfg.output.state_path
    state = json.loads(state_path.read_text())
    state["pio1"]["temperature"] = {"value": 30.1, "timestamp": datetime.now(timezone.utc).isoformat()}
    state_path.write_text(json.dumps(state))
    plan = bot.tools.plans.get("pio1")
    plan.set_target("temperature", 33)
    assert "target 33" in bot.progress_text(plan)


def test_a_progress_update_does_not_lose_a_change_made_while_it_was_drawing(tmp_path):
    bot = live_bot(tmp_path, update_every_h=0.1)
    plan = bot.tools.plans.get("pio1")
    plan.last_update_at = datetime.fromtimestamp(time.time() - 3600, timezone.utc).isoformat()
    bot.tools.plans.put(plan)
    real = bot.post_progress

    def slow_post(p, final=False):
        changed = bot.tools.plans.get("pio1")  # someone applies a change meanwhile
        changed.set_target("stirring", 350)
        bot.tools.plans.put(changed)
        real(p, final)

    bot.post_progress = slow_post
    bot.check_plans()
    after = bot.tools.plans.get("pio1")
    assert after.target("stirring") == 350 and after.last_update_at and after.slack_card_ts


# ------------------------------------------------------------------ piece 4: steppers propose, Apply approves

import dataclasses  # noqa: E402
import json  # noqa: E402

from tools import ToolError  # noqa: E402


def action_ids(msg):
    return [e.get("action_id") for b in msg.get("blocks") or [] if b["type"] == "actions" for e in b["elements"]]


def last_card(bot):
    return bot.client.updates[-1] if bot.client.updates else bot.client.posts[-1]


def card_bot(tmp_path, self_approval=True, **plan_kw):
    cfg = make_settings(tmp_path)
    cfg = dataclasses.replace(cfg, slack=dataclasses.replace(cfg.slack, allow_self_approval=self_approval, approvers=frozenset()))
    bot = live_bot(tmp_path, cfg, **plan_kw)
    bot.calls = []
    bot.tools.set_target_temperature = lambda celsius, unit=None: bot.calls.append(("temperature", celsius, unit)) or {"ok": True}
    bot.tools.set_stirring_rpm = lambda rpm, unit=None: bot.calls.append(("stirring", rpm, unit)) or {"ok": True}
    bot.post_progress(bot.tools.plans.get("pio1"))  # the live card
    return bot


def test_the_card_has_steppers_for_what_the_plan_controls(tmp_path):
    bot = card_bot(tmp_path)
    ids = action_ids(bot.client.posts[0])
    assert {"larre_step_temp_down", "larre_step_temp_up", "larre_step_rpm_down", "larre_step_rpm_up"} <= set(ids)
    assert "larre_apply" not in ids and "larre_cancel" not in ids  # nothing proposed yet


def test_no_temperature_steppers_when_the_plan_leaves_temperature_alone(tmp_path):
    bot = card_bot(tmp_path)
    plan = bot.tools.plans.get("pio1")
    plan.temperature_c = None
    bot.tools.plans.put(plan)
    bot.post_progress(plan)
    ids = action_ids(last_card(bot))
    assert "larre_step_rpm_up" in ids and "larre_step_temp_up" not in ids


def test_a_stepper_proposes_and_redraws_but_does_not_act(tmp_path):
    bot = card_bot(tmp_path)
    assert bot.step("pio1", "U1", "larre_step_temp_up") is None
    assert bot.step("pio1", "U1", "larre_step_temp_up") is None
    assert bot.calls == []  # nothing sent to the reactor
    assert bot.tools.plans.get("pio1").target("temperature") == 30
    card = last_card(bot)
    assert card["ts"] == bot.tools.plans.get("pio1").slack_card_ts  # same card, edited
    assert "32" in card["text"] and "proposed" in card["text"].lower()
    assert {"larre_apply", "larre_cancel"} <= set(action_ids(card))
    assert bot.proposed_values("pio1") == {"temperature": 32}


def test_steppers_stop_at_the_safety_limits(tmp_path):
    bot = card_bot(tmp_path)
    lim = bot.cfg.safety
    for _ in range(int(lim.max_stirring_rpm / 50) + 3):
        bot.step("pio1", "U1", "larre_step_rpm_up")
    assert bot.proposed_values("pio1")["stirring"] == lim.max_stirring_rpm


def test_stepping_back_to_the_current_setpoint_withdraws_the_proposal(tmp_path):
    bot = card_bot(tmp_path)
    bot.step("pio1", "U1", "larre_step_rpm_up")
    bot.step("pio1", "U1", "larre_step_rpm_down")
    assert bot.proposed_values("pio1") is None
    assert "larre_apply" not in action_ids(last_card(bot))


def test_cancel_drops_the_proposal(tmp_path):
    bot = card_bot(tmp_path)
    bot.step("pio1", "U1", "larre_step_rpm_up")
    assert bot.cancel("pio1", "U1") is None
    assert bot.proposed_values("pio1") is None and bot.calls == []
    assert "larre_apply" not in action_ids(last_card(bot))


def test_apply_changes_the_setpoint_and_records_it_on_the_plan(tmp_path):
    bot = card_bot(tmp_path)
    bot.step("pio1", "U1", "larre_step_rpm_up")
    bot.step("pio1", "U1", "larre_step_temp_down")
    assert bot.apply("pio1", "U1") is None
    assert sorted(bot.calls) == [("stirring", 350, "pio1"), ("temperature", 29, "pio1")]
    plan = bot.tools.plans.get("pio1")
    assert plan.target("stirring") == 350 and plan.target("temperature") == 29
    assert {sp["by"] for sp in plan.setpoints} == {"U1"}
    assert plan.expected_state()["target_rpm"] == 350
    assert bot.proposed_values("pio1") is None
    card = last_card(bot)
    assert "larre_apply" not in action_ids(card) and "larre_step_rpm_up" in action_ids(card)
    said = [p["text"] for p in bot.client.posts[1:]]
    assert any("350" in t and "<@U1>" in t for t in said)  # a thread note: who changed what
    audit = [json.loads(line) for line in bot.cfg.output.audit_path.read_text().splitlines()]
    assert audit[-1]["decision"] == "approved" and audit[-1]["decided_by"] == "U1"


def test_two_person_rule_applies_to_apply(tmp_path):
    bot = card_bot(tmp_path, self_approval=False)
    bot.step("pio1", "U1", "larre_step_rpm_up")
    error = bot.apply("pio1", "U1")
    assert error and "Two-person" in error and bot.calls == []
    assert bot.proposed_values("pio1") == {"stirring": 350}  # still waiting for a second person
    assert bot.apply("pio1", "U2") is None
    assert bot.calls == [("stirring", 350, "pio1")]


def test_anyone_who_adjusted_the_proposal_counts_as_its_requester(tmp_path):
    bot = card_bot(tmp_path, self_approval=False)
    bot.step("pio1", "U1", "larre_step_rpm_up")
    bot.step("pio1", "U2", "larre_step_rpm_up")
    assert bot.apply("pio1", "U2") and bot.apply("pio1", "U1") and bot.calls == []
    assert bot.apply("pio1", "U3") is None


def test_approver_list_applies_to_apply(tmp_path):
    bot = card_bot(tmp_path)
    bot.cfg = dataclasses.replace(bot.cfg, slack=dataclasses.replace(bot.cfg.slack, approvers=frozenset({"U9"})))
    bot.step("pio1", "U1", "larre_step_rpm_up")
    assert bot.apply("pio1", "U1") and bot.calls == []
    assert bot.apply("pio1", "U9") is None


def test_a_refused_change_is_not_recorded(tmp_path):
    bot = card_bot(tmp_path)

    def refuse(rpm, unit=None):
        raise ToolError("MQTT publish failed")

    bot.tools.set_stirring_rpm = refuse
    bot.step("pio1", "U1", "larre_step_rpm_up")
    error = bot.apply("pio1", "U1")
    assert error and "MQTT publish failed" in error
    assert bot.tools.plans.get("pio1").setpoints == []


def test_buttons_on_a_finished_plan_do_nothing(tmp_path):
    bot = card_bot(tmp_path)
    plan = bot.tools.plans.get("pio1")
    plan.status = "stopped"
    bot.tools.plans.put(plan)
    assert bot.step("pio1", "U1", "larre_step_rpm_up")
    assert bot.proposed_values("pio1") is None
    assert "larre_step_rpm_up" not in action_ids(last_card(bot))  # the card loses its buttons


def test_apply_with_nothing_proposed_says_so(tmp_path):
    bot = card_bot(tmp_path)
    assert bot.apply("pio1", "U1") and bot.calls == []


def test_button_clicks_reach_step_apply_and_cancel(tmp_path):
    import re

    import pytest

    pytest.importorskip("slack_bolt")
    from slack_bot import LarreBot

    handlers = {}

    class App:
        client = LiveSlack()

        def event(self, *_):
            return lambda f: f

        command = event

        def action(self, key):
            return lambda f: handlers.setdefault(key, f)

    bot = LarreBot(make_settings(tmp_path), app=App(), bot_user_id="UBOT")
    bot.submit = lambda fn, *a: fn(*a)
    seen = []
    bot.step = lambda unit, user, action_id: seen.append(("step", unit, user, action_id))
    bot.apply = lambda unit, user: seen.append(("apply", unit, user)) or "nope"

    def click(action_id):
        (handler,) = [f for k, f in handlers.items()
                      if (k.match(action_id) if isinstance(k, re.Pattern) else k == action_id)]
        body = {"user": {"id": "U1"}, "channel": {"id": "C1"}, "actions": [{"action_id": action_id, "value": "pio1"}]}
        handler(ack=lambda *a, **k: None, body=body)

    # handlers were bound before the fakes above, so re-register them against the fakes
    handlers.clear()
    bot._register_handlers()
    click("larre_step_rpm_up")
    click("larre_apply")
    assert seen == [("step", "pio1", "U1", "larre_step_rpm_up"), ("apply", "pio1", "U1")]
    assert bot.client.ephemeral[-1]["text"] == "nope"  # an error goes to the clicker privately
