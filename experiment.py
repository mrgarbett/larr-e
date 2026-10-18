"""Experiment plans: what the scientist asked for, as a Pioreactor experiment profile.

A scientist says "run the vial at 37 °C and 180 rpm for 18 hours, update me every 2 hours".
LARR-E turns that into an ExperimentPlan, shows the profile YAML for approval, then hands
the schedule to Pioreactor (`pio run experiment_profile execute ...`). Pioreactor owns the
timing and the stop at the end; LARR-E only keeps the plan on disk so that

* it survives a restart of the laptop, the bot, or the agent,
* anomaly triage knows what is intentional (stirring stopping at t = 18 h is the plan,
  not a failure),
* the Slack bot knows when the next progress update is due.

Profile format: Pioreactor experiment profiles, version "1.0"
(https://docs.pioreactor.com/user-guide/experiment-profiles-schema).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from config import Settings


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "experiment"


def _yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return f"{value:g}" if isinstance(value, float) else str(value)
    return json.dumps(str(value), ensure_ascii=False)  # a JSON string is a valid double-quoted YAML scalar


@dataclass
class ExperimentPlan:
    name: str
    unit: str
    experiment: str
    duration_h: float
    temperature_c: float | None = None
    rpm: float | None = None
    od_reading: bool = True
    growth_rate: bool = True
    update_every_h: float | None = None
    requested_by: str = ""
    notes: str = ""
    status: str = "draft"  # draft -> running -> finished | stopped | failed
    created_at: str = field(default_factory=lambda: _iso(_now()))
    started_at: str | None = None
    ended_at: str | None = None
    last_update_at: str | None = None
    profile_filename: str | None = None
    # Where progress updates go (set by the Slack bot when the plan starts from a thread).
    slack_channel: str | None = None
    slack_thread_ts: str | None = None
    final_posted: bool = False
    # The one live Slack card for this plan, edited in place on each update.
    slack_card_ts: str | None = None
    # Setpoint changes approved while the plan runs: [{"at", "metric", "value", "by"}], oldest first.
    # temperature_c / rpm stay what was asked for at the start (and what the profile started with).
    setpoints: list[dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------ validation

    def validate(self, cfg: Settings) -> None:
        lim = cfg.safety
        if not 0 < self.duration_h <= cfg.plans.max_duration_h:
            raise ValueError(f"duration_h must be in (0, {cfg.plans.max_duration_h}] hours.")
        if self.temperature_c is not None and not lim.min_temperature_c <= self.temperature_c <= lim.max_temperature_c:
            raise ValueError(f"temperature_c must be within {lim.min_temperature_c}-{lim.max_temperature_c} °C.")
        if self.rpm is not None and not 0 < self.rpm <= lim.max_stirring_rpm:
            raise ValueError(f"rpm must be within (0, {lim.max_stirring_rpm}].")
        if self.update_every_h is not None and not 0.05 <= self.update_every_h <= self.duration_h:
            raise ValueError("update_every_h must be at least 0.05 h (3 min) and no longer than the experiment.")
        if self.growth_rate and not self.od_reading:
            raise ValueError("growth_rate needs od_reading.")
        if self.temperature_c is None and self.rpm is None and not self.od_reading:
            raise ValueError("The plan does nothing: give a temperature, a stirring speed, or OD reading.")

    # ------------------------------------------------------------ profile

    def jobs(self) -> dict[str, dict[str, Any]]:
        """job name -> start options, in start order."""
        jobs: dict[str, dict[str, Any]] = {}
        if self.rpm is not None:
            jobs["stirring"] = {"target_rpm": round(self.rpm)}
        if self.temperature_c is not None:
            jobs["temperature_automation"] = {
                "automation_name": "thermostat",
                "target_temperature": round(self.temperature_c, 2),
            }
        if self.od_reading:
            jobs["od_reading"] = {}
        if self.growth_rate:
            jobs["growth_rate_calculating"] = {}
        return jobs

    # growth_rate_calculating reads OD from od_reading, so give od_reading a head start.
    START_TIMES = {"growth_rate_calculating": "30s"}

    def profile_name(self) -> str:
        return f"larre-{_slug(self.name)}"

    def to_profile_yaml(self) -> str:
        """A Pioreactor experiment profile that starts every job at t=0 and stops it at the end."""
        end = f"{self.duration_h:g}h"
        desc = self.notes or self.summary()
        lines = [
            'version: "1.0"',
            f"experiment_profile_name: {_yaml_scalar(self.profile_name())}",
            "metadata:",
            '  author: "LARR-E"',
            f"  description: {_yaml_scalar(desc)}",
            "pioreactors:",
            f"  {self.unit}:",
            "    jobs:",
        ]
        for job, options in self.jobs().items():
            lines += [f"      {job}:", "        actions:", "          - type: start", f"            t: {self.START_TIMES.get(job, '0s')}"]
            if options:
                lines.append("            options:")
                lines += [f"              {k}: {_yaml_scalar(v)}" for k, v in options.items()]
            lines += ["          - type: stop", f"            t: {end}"]
        return "\n".join(lines) + "\n"

    def summary(self) -> str:
        parts = []
        if self.temperature_c is not None:
            parts.append(f"{self.temperature_c:g} °C")
        if self.rpm is not None:
            parts.append(f"{self.rpm:g} rpm")
        if self.od_reading:
            parts.append("OD + growth rate" if self.growth_rate else "OD")
        upd = f", updates every {self.update_every_h:g} h" if self.update_every_h else ""
        return f"{self.name}: {', '.join(parts)} for {self.duration_h:g} h on {self.unit}{upd}"

    # ------------------------------------------------------------ timing

    def elapsed_h(self, now: datetime | None = None) -> float | None:
        if not self.started_at:
            return None
        return ((now or _now()) - _parse(self.started_at)).total_seconds() / 3600

    def ends_at(self) -> datetime | None:
        return _parse(self.started_at) + timedelta(hours=self.duration_h) if self.started_at else None

    def update_due(self, now: datetime | None = None) -> bool:
        if self.status != "running" or not self.update_every_h:
            return False
        now = now or _now()
        last = _parse(self.last_update_at or self.started_at)  # type: ignore[arg-type]
        return now - last >= timedelta(hours=self.update_every_h)

    def refresh_status(self, now: datetime | None = None) -> bool:
        """Mark the plan finished once its duration has passed. Returns True if it changed."""
        now = now or _now()
        end = self.ends_at()
        if self.status == "running" and end and now >= end:
            self.status, self.ended_at = "finished", _iso(end)
            return True
        return False

    def expected_state(self, now: datetime | None = None) -> dict[str, Any]:
        """What the reactor *should* be doing right now, for anomaly triage."""
        running = self.status == "running" and not (self.ends_at() and (now or _now()) >= self.ends_at())
        return {
            "plan_status": self.status,
            "jobs_should_be_running": sorted(self.jobs()) if running else [],
            "target_temperature_c": self.target("temperature") if running else None,
            "target_rpm": self.target("stirring") if running else None,
            "elapsed_h": round(self.elapsed_h(now) or 0.0, 2),
            "ends_at": _iso(self.ends_at()) if self.ends_at() else None,
        }

    # "Off target" for charts (the /plan page in web/plan.html uses the same numbers). The heater
    # takes minutes to warm a vial and the stirrer a few seconds to spin up, so the start is forgiven.
    TEMP_TOLERANCE_C = 1.0
    RPM_TOLERANCE_FRAC = 0.15
    WARM_UP_S = 600
    SPIN_UP_S = 30

    def set_target(self, metric: str, value: float, by: str = "", now: datetime | None = None) -> None:
        """Record an approved setpoint change. The caller has already sent it to the reactor."""
        if self.target(metric) is None:
            raise ValueError(f"The plan doesn't control {metric}.")
        self.setpoints.append({"at": _iso(now or _now()), "metric": metric, "value": float(value), "by": by})

    def target_steps(self, metric: str) -> list[tuple[float, float]]:
        """The setpoint as a step line: (minutes since the plan started, value) at the start and each change."""
        initial = {"temperature": self.temperature_c, "stirring": self.rpm}.get(metric)
        if initial is None:
            return []
        start = _parse(self.started_at) if self.started_at else None
        steps = [(0.0, float(initial))]
        for sp in self.setpoints:
            if sp["metric"] == metric:
                at = (_parse(sp["at"]) - start).total_seconds() / 60 if start else 0.0
                steps.append((round(at, 4), float(sp["value"])))
        return steps

    def target(self, metric: str, seconds_in: float | None = None) -> float | None:
        """The setpoint in force `seconds_in` after the start, or the current one."""
        steps = self.target_steps(metric)
        if not steps:
            return None
        if seconds_in is None:
            return steps[-1][1]
        return [v for t, v in steps if t * 60 <= seconds_in or t == 0][-1]

    def off_target(self, metric: str, seconds_in: float, value: float) -> bool:
        """Whether a reading taken `seconds_in` after the plan started is off the setpoint in force then.
        The warm-up / spin-up allowance restarts at each setpoint change."""
        target = self.target(metric, seconds_in)
        if target is None or not 0 <= seconds_in <= self.duration_h * 3600:
            return False
        since = seconds_in - max(t * 60 for t, _ in self.target_steps(metric) if t * 60 <= seconds_in or t == 0)
        if metric == "temperature":
            return since > self.WARM_UP_S and abs(value - target) > self.TEMP_TOLERANCE_C
        return since > self.SPIN_UP_S and abs(value - target) > target * self.RPM_TOLERANCE_FRAC

    def public(self) -> dict[str, Any]:
        d = asdict(self)
        d["summary"] = self.summary()
        if self.status == "running":
            d["expected_state"] = self.expected_state()
        return d


class PlanStore:
    """One active plan per unit, kept in a JSON file in the runtime directory."""

    def __init__(self, path: Path):
        self.path = path

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save(self, data: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.path)  # atomic, so a crash never leaves half a file

    def get(self, unit: str) -> ExperimentPlan | None:
        raw = self._load().get(unit)
        if not raw:
            return None
        plan = ExperimentPlan(**raw)
        if plan.refresh_status():
            self.put(plan)
        return plan

    def all(self) -> list[ExperimentPlan]:
        return [p for p in (self.get(u) for u in self._load()) if p]

    def put(self, plan: ExperimentPlan) -> None:
        data = self._load()
        data[plan.unit] = asdict(plan)
        self._save(data)
