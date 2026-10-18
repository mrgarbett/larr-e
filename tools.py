"""Hardware and data toolset that the LLM routing layer calls.

Actuators use the same interfaces as the Pioreactor web UI:

* change a running job's setting:  MQTT pioreactor/<unit>/<exp>/<job>/<attr>/set
* stop a job:                      MQTT pioreactor/<unit>/<exp>/<job>/$state/set  -> "disconnected"
* start a job (e.g. a pump):       REST POST /api/workers/<unit>/jobs/run/job_name/<job>/experiments/<exp>
                                   (Pioreactor has no MQTT subscriber for starting jobs)

Reads come from the leader's SQLite database (opened read-only) and from the
live state snapshot that listener.py keeps up to date.
"""

from __future__ import annotations

import json
import re
import shlex
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import paho.mqtt.publish as mqtt_publish

from config import Settings, settings as default_settings
from experiment import ExperimentPlan, PlanStore
from leader_api import LeaderAPI, LeaderAPIError


class ToolError(Exception):
    """A tool failed in a way the LLM should see and can recover from."""


def utc_timestamp() -> str:
    """Timestamp in the format Pioreactor uses in payloads and SQLite rows."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# metric name -> (table, value column, extra WHERE clause)
METRIC_TABLES: dict[str, tuple[str, str, str]] = {
    "od": ("od_readings", "od_reading", ""),
    "od_filtered": ("od_readings_filtered", "normalized_od_reading", ""),
    "temperature": ("temperature_readings", "temperature_c", ""),
    "growth_rate": ("growth_rates", "rate", ""),
    "stirring": ("stirring_rates", "measured_rpm", ""),
}

UNIT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}")

# audit_log kinds: which logged tool calls count as which kind of hardware action.
AUDIT_KINDS: dict[str, Any] = {
    "all": lambda tool, args: True,
    "pump": lambda tool, args: tool == "dose",
    "stirring": lambda tool, args: tool == "set_stirring_rpm"
    or (tool in ("start_job", "stop_job") and args.get("job") == "stirring"),
    "temperature": lambda tool, args: tool == "set_target_temperature"
    or (tool in ("start_job", "stop_job") and args.get("job") == "temperature_automation"),
    "experiment": lambda tool, args: tool in ("start_experiment", "stop_experiment"),
}

# The leader's live time-series API (/api/experiments/<exp>/time_series/<source>) covers these
# metrics; stirring isn't served there, so it comes from the listener's recent history instead.
LEADER_SERIES = {
    "od": "od_readings",
    "od_filtered": "od_readings_filtered",
    "temperature": "temperature_readings",
    "growth_rate": "growth_rates",
}

METRIC_LABELS = {
    "od": "OD (raw)",
    "od_filtered": "OD (normalized)",
    "temperature": "Temperature (°C)",
    "growth_rate": "Growth rate (h⁻¹)",
    "stirring": "Stirring (RPM)",
}

PUMP_JOBS = {"media": "add_media", "waste": "remove_waste", "alt_media": "add_alt_media"}

STARTABLE_JOBS = {"stirring", "od_reading", "growth_rate_calculating", "temperature_automation"}
STOPPABLE_JOBS = STARTABLE_JOBS | {"dosing_automation", "led_automation", *PUMP_JOBS.values()}


@dataclass
class Series:
    metric: str
    unit: str
    experiment: str
    timestamps: list[datetime]
    values: list[float]

    def summary(self) -> dict[str, Any]:
        if not self.values:
            return {"metric": self.metric, "points": 0}
        first, last = self.timestamps[0], self.timestamps[-1]
        return {
            "metric": self.metric,
            "points": len(self.values),
            "start": first.isoformat(),
            "end": last.isoformat(),
            "min": round(min(self.values), 4),
            "max": round(max(self.values), 4),
            "mean": round(sum(self.values) / len(self.values), 4),
            "first": round(self.values[0], 4),
            "last": round(self.values[-1], 4),
        }


class PioreactorTools:
    def __init__(
        self, cfg: Settings = default_settings, dry_run: bool | None = None, render_terminal: bool = True
    ):
        self.cfg = cfg
        self.dry_run = cfg.pioreactor.dry_run if dry_run is None else dry_run
        self.render_terminal = render_terminal
        self._leaders: dict[str, LeaderAPI] = {}
        self.plans = PlanStore(cfg.output.plans_path)
        cfg.ensure_dirs()

    # ------------------------------------------------------------------ context

    def resolve_unit(self, unit: str | None) -> str:
        unit = unit or self.cfg.pioreactor.default_unit
        if unit:
            # Unit names are Pi hostnames. Checked here, before any approval card: the audit log once
            # showed an approved pump run for "p i o 01", a model mangling "pio01".
            if not UNIT_NAME.fullmatch(unit):
                raise ToolError(f"'{unit}' isn't a valid unit name (letters, digits, - and _ only). "
                                f"Did you mean '{re.sub(r'[^A-Za-z0-9_-]', '', unit)}'?")
            return unit
        units = sorted(self._live_state().keys())
        if len(units) == 1:
            return units[0]
        raise ToolError(
            f"No unit given and no single default available (live units: {units or 'none'}). "
            "Specify a unit name."
        )

    def resolve_experiment(self, unit: str | None = None) -> str:
        # In a mixed cluster (real Pi + twin) units run different experiments, so what a unit is
        # actually streaming wins over the global setting.
        state = self._live_state()
        if unit and unit in state and state[unit].get("experiment"):
            return state[unit]["experiment"]
        if unit and not self.dry_run:
            assigned = self.leader_for(unit).assigned_experiment(unit)
            if assigned:
                return assigned
        if self.cfg.pioreactor.experiment:
            return self.cfg.pioreactor.experiment
        try:
            with self._connect_ro() as conn:
                row = None
                if unit:
                    row = conn.execute(
                        "SELECT experiment FROM od_readings WHERE pioreactor_unit = ? ORDER BY timestamp DESC LIMIT 1",
                        (unit,),
                    ).fetchone()
                row = row or conn.execute(
                    "SELECT experiment FROM experiments ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
        except (ToolError, sqlite3.DatabaseError):
            row = None
        if row:
            return row[0]
        raise ToolError("Could not determine the active experiment; set LARRE_EXPERIMENT.")

    def leader_for(self, unit: str) -> LeaderAPI:
        """The REST API that controls this unit: its own (twin) leader if mapped, else the main leader."""
        url = self.cfg.pioreactor.leaders.get(unit, self.cfg.pioreactor.leader_url)
        if url not in self._leaders:
            self._leaders[url] = LeaderAPI(url)
        return self._leaders[url]

    def leader_unit_for(self, unit: str) -> str:
        if unit in self.cfg.pioreactor.leaders:
            return unit
        return self.cfg.pioreactor.leader_unit or unit

    def list_units(self) -> dict[str, Any]:
        """Every unit LARR-E knows: configured leaders, the default unit, and anything streaming telemetry."""
        live = self._live_state()
        names = set(live) | set(self.cfg.pioreactor.leaders)
        if self.cfg.pioreactor.default_unit:
            names.add(self.cfg.pioreactor.default_unit)
        units = []
        for u in sorted(names):
            plan = self.plans.get(u)
            units.append({
                "unit": u,
                "leader": self.cfg.pioreactor.leaders.get(u, self.cfg.pioreactor.leader_url),
                "streaming": u in live,
                "last_seen": live.get(u, {}).get("last_seen"),
                "experiment": live.get(u, {}).get("experiment"),
                "plan": plan.summary() + f" [{plan.status}]" if plan else None,
            })
        return {"units": units}

    # ------------------------------------------------------------------ MQTT

    def _publish(self, topic: str, payload: str, qos: int = 2) -> dict[str, Any]:
        record = {"topic": topic, "payload": payload, "dry_run": self.dry_run}
        if self.dry_run:
            return record
        m = self.cfg.mqtt
        auth = {"username": m.username, "password": m.password} if m.username else None
        try:
            mqtt_publish.single(
                topic,
                payload=payload,
                qos=qos,
                hostname=m.host,
                port=m.port,
                auth=auth,
                keepalive=m.keepalive,
                client_id=f"{m.client_id_prefix}-cmd-{int(time.time() * 1000)}",
            )
        except OSError as exc:
            raise ToolError(f"MQTT publish to {m.host}:{m.port} failed: {exc}") from exc
        return record

    def _set_attribute(self, unit: str, job: str, attr: str, value: Any) -> dict[str, Any]:
        exp = self.resolve_experiment(unit)
        return self._publish(f"pioreactor/{unit}/{exp}/{job}/{attr}/set", str(value))

    def _change_or_start(self, unit: str, job: str, attr: str, value: Any, start_options: dict[str, Any]) -> dict[str, Any]:
        """Change a running job's setting; if the job isn't running, start it with that setting.

        A setting published over MQTT to a job that isn't running reaches no one, yet looks like
        success: on 17 Oct an approved "stir at 200 rpm" changed nothing because stirring was off.
        """
        if self.dry_run or self.cfg.pioreactor.pump_transport not in ("rest", "http"):
            return self._set_attribute(unit, job, attr, value)
        try:
            running = self.leader_for(unit).running_jobs(unit)
        except LeaderAPIError as exc:
            result = self._set_attribute(unit, job, attr, value)
            return {**result, "note": f"Sent the change, but couldn't check whether {job} is running ({exc}). "
                                      f"If it isn't running, nothing changed: start it instead."}
        if job in running:
            return self._set_attribute(unit, job, attr, value)
        return {**self._run_job_rest(unit, job, start_options),
                "note": f"{job} wasn't running, so it was started with {attr}={value}."}

    def _run_job(self, unit: str, job: str, options: dict[str, Any]) -> dict[str, Any]:
        transport = self.cfg.pioreactor.pump_transport
        if transport == "pios":
            return self._run_job_pios(unit, job, options)
        if transport in ("rest", "http"):  # "http" is the same transport under another name
            return self._run_job_rest(unit, job, options)
        raise ToolError(
            f"LARRE_PUMP_TRANSPORT={transport!r} can't start jobs; use 'rest' (or 'pios' on the leader). "
            "Pioreactor ignores the mqtt run/<job> topic, so a publish there would start nothing."
        )

    def _run_job_pios(self, unit: str, job: str, options: dict[str, Any]) -> dict[str, Any]:
        cmd = ["pios", "run", job, "--units", unit, "-y"]
        for key, value in options.items():
            cmd.append(f"--{key.replace('_', '-')}={value}")
        record = {"command": shlex.join(cmd), "dry_run": self.dry_run}
        if self.dry_run:
            return record
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise ToolError(f"`{record['command']}` failed: {proc.stderr.strip() or proc.stdout.strip()}")
        return record

    def _run_job_rest(self, unit: str, job: str, options: dict[str, Any]) -> dict[str, Any]:
        exp = self.resolve_experiment(unit)
        record: dict[str, Any] = {"transport": "rest", "job": job, "experiment": exp, "dry_run": self.dry_run}
        if self.dry_run:
            return record
        try:
            response = self.leader_for(unit).run_job(unit, job, exp, options)
        except LeaderAPIError as exc:
            raise ToolError(str(exc)) from exc
        # The leader accepts the request; only the per-unit result says whether the unit did it.
        per_unit = response if isinstance(response, dict) else {}
        refusals = [f"{u}: {r.get('error') or r}" for u, r in per_unit.items() if isinstance(r, dict) and r.get("ok") is False]
        if refusals:
            raise ToolError(f"Pioreactor did not start {job}: {'; '.join(refusals)}"[:300])
        record["leader_response"] = response
        return record

    # ------------------------------------------------------------------ actuators
    # Each actuator validates against SafetyLimits first. check_only=True stops after
    # validation, so an out-of-limit request is rejected before anyone is asked to confirm it.

    def dose(
        self,
        pump: str,
        unit: str | None = None,
        ml: float | None = None,
        duration_s: float | None = None,
        check_only: bool = False,
    ) -> dict[str, Any]:
        if pump not in PUMP_JOBS:
            raise ToolError(f"pump must be one of {sorted(PUMP_JOBS)}")
        if (ml is None) == (duration_s is None):
            raise ToolError("Give exactly one of ml or duration_s.")
        limits = self.cfg.safety
        if ml is not None and not limits.pumps_calibrated:
            raise ToolError("The pumps are not calibrated, so ml can't be measured. Use duration_s (seconds) instead.")
        if ml is not None:
            if not 0 < ml <= limits.max_dose_ml:
                raise ToolError(f"ml must be in (0, {limits.max_dose_ml}] (safety limit).")
            options = {"ml": round(float(ml), 3)}
        else:
            if not 0 < duration_s <= limits.max_dose_duration_s:
                raise ToolError(f"duration_s must be in (0, {limits.max_dose_duration_s}] (safety limit).")
            options = {"duration": round(float(duration_s), 2)}
        unit = self.resolve_unit(unit)
        if check_only:  # validated; the agent asks for confirmation before the real call
            return {"ok": True}
        result = self._run_job(unit, PUMP_JOBS[pump], options)
        return {"ok": True, "action": PUMP_JOBS[pump], "unit": unit, **options, **result}

    def set_target_temperature(self, celsius: float, unit: str | None = None, check_only: bool = False) -> dict[str, Any]:
        limits = self.cfg.safety
        if not limits.min_temperature_c <= celsius <= limits.max_temperature_c:
            raise ToolError(
                f"Target must be within {limits.min_temperature_c}-{limits.max_temperature_c} °C."
            )
        unit = self.resolve_unit(unit)
        if check_only:  # validated; the agent asks for confirmation before the real call
            return {"ok": True}
        target = round(celsius, 2)
        result = self._change_or_start(unit, "temperature_automation", "target_temperature", target,
                                       {"automation_name": "thermostat", "target_temperature": target})
        return {
            "ok": True,
            "unit": unit,
            "target_temperature": celsius,
            "note": "Takes effect only if the thermostat temperature automation is running.",
            **result,
        }

    def set_stirring_rpm(self, rpm: float, unit: str | None = None, check_only: bool = False) -> dict[str, Any]:
        if not 0 <= rpm <= self.cfg.safety.max_stirring_rpm:
            raise ToolError(f"rpm must be within 0-{self.cfg.safety.max_stirring_rpm}.")
        unit = self.resolve_unit(unit)
        if check_only:  # validated; the agent asks for confirmation before the real call
            return {"ok": True}
        result = self._change_or_start(unit, "stirring", "target_rpm", round(rpm), {"target_rpm": round(rpm)})
        return {"ok": True, "unit": unit, "target_rpm": round(rpm), **result}

    def start_job(
        self, job: str, unit: str | None = None, options: dict[str, Any] | None = None, check_only: bool = False
    ) -> dict[str, Any]:
        if job not in STARTABLE_JOBS:
            raise ToolError(f"job must be one of {sorted(STARTABLE_JOBS)}")
        options = dict(options or {})
        if job == "temperature_automation":
            options.setdefault("automation_name", "thermostat")
            if "target_temperature" in options:
                t = float(options["target_temperature"])
                lim = self.cfg.safety
                if not lim.min_temperature_c <= t <= lim.max_temperature_c:
                    raise ToolError(f"target_temperature must be within {lim.min_temperature_c}-{lim.max_temperature_c} °C.")
        if job == "stirring" and "target_rpm" in options:
            if not 0 <= float(options["target_rpm"]) <= self.cfg.safety.max_stirring_rpm:
                raise ToolError(f"target_rpm must be within 0-{self.cfg.safety.max_stirring_rpm}.")
        unit = self.resolve_unit(unit)
        if check_only:  # validated; the agent asks for confirmation before the real call
            return {"ok": True}
        result = self._run_job(unit, job, options)
        return {"ok": True, "unit": unit, "job": job, "options": options, **result}

    def stop_job(self, job: str, unit: str | None = None, check_only: bool = False) -> dict[str, Any]:
        if job not in STOPPABLE_JOBS:
            raise ToolError(f"job must be one of {sorted(STOPPABLE_JOBS)}")
        unit = self.resolve_unit(unit)
        if check_only:  # validated; the agent asks for confirmation before the real call
            return {"ok": True}
        result = self._set_attribute(unit, job, "$state", "disconnected")
        return {"ok": True, "unit": unit, "job": job, "state": "disconnected", **result}

    # ------------------------------------------------------------------ experiment plans

    def _build_plan(
        self,
        name: str,
        duration_h: float,
        temperature_c: float | None = None,
        rpm: float | None = None,
        od_reading: bool = True,
        growth_rate: bool | None = None,
        update_every_h: float | None = None,
        unit: str | None = None,
        notes: str = "",
        requested_by: str = "",
    ) -> ExperimentPlan:
        unit = self.resolve_unit(unit)
        plan = ExperimentPlan(
            name=name,
            unit=unit,
            experiment=self.resolve_experiment(unit),
            duration_h=float(duration_h),
            temperature_c=None if temperature_c is None else float(temperature_c),
            rpm=None if rpm is None else float(rpm),
            od_reading=bool(od_reading),
            growth_rate=bool(od_reading) if growth_rate is None else bool(growth_rate),
            update_every_h=self.cfg.plans.default_update_every_h if update_every_h is None else float(update_every_h),
            notes=notes,
            requested_by=requested_by,
        )
        if plan.update_every_h and plan.update_every_h > plan.duration_h:
            # A run shorter than one update interval gets a single update at the end, or none if very short.
            plan.update_every_h = plan.duration_h if plan.duration_h >= 0.05 else None
        try:
            plan.validate(self.cfg)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return plan

    def plan_experiment(self, **kwargs: Any) -> dict[str, Any]:
        """Read-only preview: the plan and the Pioreactor profile it becomes. Changes nothing."""
        plan = self._build_plan(**kwargs)
        current = self.plans.get(plan.unit)
        return {
            "plan": plan.public(),
            "profile_yaml": plan.to_profile_yaml(),
            "conflict": current.summary() if current and current.status == "running" else None,
        }

    def start_experiment(self, check_only: bool = False, **kwargs: Any) -> dict[str, Any]:
        """Upload the profile to the leader and execute it. Pioreactor then owns the schedule."""
        plan = self._build_plan(**kwargs)
        current = self.plans.get(plan.unit)
        if current and current.status == "running":
            raise ToolError(
                f"{plan.unit} already runs '{current.summary()}'. Stop it with stop_experiment first: "
                "two profiles driving the same jobs interfere with each other."
            )
        if check_only:  # validated; the agent asks for confirmation before the real call
            return {"ok": True, "plan": plan.public()}
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        plan.profile_filename = f"{plan.profile_name()}-{stamp}.yaml"
        yaml_body = plan.to_profile_yaml()
        leader, leader_unit = self.leader_for(plan.unit), self.leader_unit_for(plan.unit)
        full_path = f"{self.cfg.pioreactor.profile_dir.rstrip('/')}/{plan.profile_filename}"
        record: dict[str, Any] = {"dry_run": self.dry_run, "profile_path_on_leader": full_path}
        if not self.dry_run:
            try:
                leader.upload_profile(plan.profile_filename, yaml_body)
            except LeaderAPIError as exc:
                raise ToolError(f"The leader rejected the profile: {exc}") from exc
            try:
                record["leader_response"] = leader.run_profile(leader_unit, full_path, plan.experiment)
            except LeaderAPIError as exc:
                plan.status = "failed"
                self.plans.put(plan)
                raise ToolError(f"Profile uploaded but did not start: {exc}") from exc
        plan.status = "running"
        plan.started_at = utc_timestamp()
        self.plans.put(plan)
        return {"ok": True, "plan": plan.public(), "profile_yaml": yaml_body, **record}

    def get_experiment_plan(self, unit: str | None = None) -> dict[str, Any]:
        if unit:
            plan = self.plans.get(self.resolve_unit(unit))
            return {"plan": plan.public() if plan else None}
        return {"plans": [p.public() for p in self.plans.all()]}

    def stop_experiment(self, unit: str | None = None, check_only: bool = False) -> dict[str, Any]:
        """Stop the running profile and the jobs it started, before its scheduled end."""
        unit = self.resolve_unit(unit)
        plan = self.plans.get(unit)
        if not plan or plan.status != "running":
            raise ToolError(f"No running experiment plan on {unit}.")
        if check_only:  # validated; the agent asks for confirmation before the real call
            return {"ok": True}
        stopped, errors = [], []
        if not self.dry_run:
            leader = self.leader_for(unit)
            # Stop only this plan's profile. Stopping job_name=experiment_profile would stop every
            # profile in the experiment, including other units' plans.
            try:
                job_id = leader.find_profile_run(plan.experiment, plan.profile_filename or "")
                if job_id is not None:
                    leader.stop_job_id(job_id)
                    stopped.append(f"experiment_profile#{job_id}")
                else:
                    errors.append("profile run not found on the leader (already finished, or started elsewhere)")
            except LeaderAPIError as exc:
                errors.append(f"experiment_profile: {exc}")
            # Killing a profile stops the jobs it started; stopping them explicitly as well is harmless.
            for target, job in [(unit, job) for job in plan.jobs()]:
                try:
                    leader.stop_job(target, job, plan.experiment)
                    stopped.append(job)
                except LeaderAPIError as exc:
                    errors.append(f"{job}: {exc}")
        plan.status, plan.ended_at = "stopped", utc_timestamp()
        self.plans.put(plan)
        return {"ok": not errors, "unit": unit, "stopped": stopped, "errors": errors, "dry_run": self.dry_run}

    def active_plan_context(self, unit: str | None) -> dict[str, Any] | None:
        """Expected state for the agent and triage; None when there is no plan."""
        try:
            plan = self.plans.get(self.resolve_unit(unit))
        except ToolError:
            return None
        if not plan:
            return None
        return {"summary": plan.summary(), "status": plan.status, **plan.expected_state()}

    # ------------------------------------------------------------------ SQLite

    def _connect_ro(self) -> sqlite3.Connection:
        path = Path(self.cfg.pioreactor.db_path)
        if not path.exists():
            raise ToolError(f"Database not found at {path}. Set LARRE_DB_PATH.")
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row

        # Defense in depth on top of mode=ro: refuse anything but reads.
        allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}

        def authorizer(action, *_):
            return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY

        conn.set_authorizer(authorizer)
        deadline = time.monotonic() + self.cfg.safety.query_timeout_s
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
        return conn

    def query_database(self, sql: str) -> dict[str, Any]:
        stripped = sql.strip().rstrip(";")
        if ";" in stripped:
            raise ToolError("Only a single statement is allowed.")
        if not stripped.lower().startswith(("select", "with")):
            raise ToolError("Only SELECT / WITH queries are allowed.")
        max_rows = self.cfg.safety.max_query_rows
        try:
            with self._connect_ro() as conn:
                cur = conn.execute(stripped)
                rows = cur.fetchmany(max_rows + 1)
                columns = [d[0] for d in cur.description or []]
        except sqlite3.DatabaseError as exc:
            raise ToolError(f"SQL error: {exc}") from exc
        truncated = len(rows) > max_rows
        return {
            "columns": columns,
            "rows": [list(r) for r in rows[:max_rows]],
            "row_count": min(len(rows), max_rows),
            "truncated": truncated,
        }

    def get_series(
        self, metric: str, unit: str, minutes: float, experiment: str | None = None
    ) -> tuple[Series, str]:
        """Fetch a time series covering the most recent `minutes` of data, from the first source
        that has it: the leader's live API, then a local copy of its database, then the
        listener's recent history.

        For the database, the window is anchored on the newest stored reading rather than the
        wall clock: Raspberry Pis have no RTC, so clocks can disagree across machines.
        """
        if metric not in METRIC_TABLES:
            raise ToolError(f"metric must be one of {sorted(METRIC_TABLES)}")
        experiment = experiment or self.resolve_experiment(unit)
        attempts: list[str] = []
        if self.cfg.pioreactor.pump_transport in ("rest", "http") and metric in LEADER_SERIES:
            try:
                series = self._leader_series(metric, unit, minutes, experiment)
                if series.values:
                    return series, series.timestamps[-1].isoformat()
                attempts.append("leader API: no readings in that window")
            except ToolError as exc:
                attempts.append(str(exc))
        if Path(self.cfg.pioreactor.db_path).exists():
            series, latest = self._db_series(metric, unit, minutes, experiment)
            if series.values:
                return series, latest
            attempts.append("database copy: no readings")
        series = self._live_series(metric, unit, minutes, experiment)
        if series.values:
            return series, series.timestamps[-1].isoformat()
        attempts.append("listener: no recent readings (is the job running and `larr-e listen` up?)")
        return Series(metric, unit, experiment, [], []), "no data (" + "; ".join(attempts) + ")"

    def _leader_series(self, metric: str, unit: str, minutes: float, experiment: str) -> Series:
        path = (f"/api/experiments/{quote(experiment)}/time_series/{LEADER_SERIES[metric]}"
                f"?lookback={minutes / 60:.4f}")  # lookback is in hours
        try:
            data = self.leader_for(unit).call("GET", path)
        except LeaderAPIError as exc:
            raise ToolError(str(exc)) from exc
        data = data if isinstance(data, dict) else {}
        names, points = data.get("series", []), data.get("data", [])
        rows = points[names.index(unit)] if unit in names and names.index(unit) < len(points) else []
        rows = sorted(rows, key=lambda p: p["x"])
        return Series(metric, unit, experiment, [parse_timestamp(p["x"]) for p in rows], [float(p["y"]) for p in rows])

    def _live_series(self, metric: str, unit: str, minutes: float, experiment: str) -> Series:
        s = self._live_state().get(unit, {})
        values, stamps = s.get("history", {}).get(metric, []), s.get("history_t", {}).get(metric, [])
        pairs = list(zip(stamps, values))
        if pairs:
            cutoff = pairs[-1][0] - minutes * 60  # anchored on the newest reading, like the database path
            pairs = [(t, v) for t, v in pairs if t >= cutoff]
        return Series(metric, unit, experiment,
                      [datetime.fromtimestamp(t, timezone.utc) for t, _ in pairs], [float(v) for _, v in pairs])

    def _db_series(self, metric: str, unit: str, minutes: float, experiment: str) -> tuple[Series, str]:
        table, column, extra = METRIC_TABLES[metric]
        try:
            with self._connect_ro() as conn:
                latest = conn.execute(
                    f"SELECT MAX(timestamp) FROM {table} WHERE experiment = ? AND pioreactor_unit = ? {extra}",
                    (experiment, unit),
                ).fetchone()[0]
                if latest is None:
                    return Series(metric, unit, experiment, [], []), "no data"
                cutoff = parse_timestamp(latest) - timedelta(minutes=minutes)
                rows = conn.execute(
                    f"""SELECT timestamp, {column} FROM {table}
                        WHERE experiment = ? AND pioreactor_unit = ? AND timestamp >= ? {extra}
                        ORDER BY timestamp""",
                    (experiment, unit, cutoff.strftime("%Y-%m-%dT%H:%M:%S")),
                ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise ToolError(f"SQL error reading {table}: {exc}") from exc
        series = Series(
            metric,
            unit,
            experiment,
            [parse_timestamp(r[0]) for r in rows],
            [float(r[1]) for r in rows],
        )
        return series, latest

    def get_recent_timeseries(
        self, metric: str, unit: str | None = None, minutes: float = 10, max_points: int = 60
    ) -> dict[str, Any]:
        unit = self.resolve_unit(unit)
        series, anchor = self.get_series(metric, unit, minutes)
        step = max(1, len(series.values) // max_points)
        return {
            "unit": unit,
            "experiment": series.experiment,
            "window_end": anchor,
            "summary": series.summary(),
            "points": [
                [t.isoformat(timespec="seconds"), round(v, 4)]
                for t, v in list(zip(series.timestamps, series.values))[::step]
            ],
        }

    def plot_timeseries(
        self, metrics: list[str], unit: str | None = None, minutes: float = 10
    ) -> dict[str, Any]:
        """Draw 1-2 metrics in the terminal and save a PNG with twin y-axes."""
        # The Figure API (no pyplot) is safe to use from the Slack bot's worker threads.
        import numpy as np
        import plotext as tplt
        from matplotlib.figure import Figure

        if not 1 <= len(metrics) <= 2:
            raise ToolError("Plot one or two metrics at a time.")
        unit = self.resolve_unit(unit)
        series = [self.get_series(m, unit, minutes)[0] for m in metrics]
        if any(not s.values for s in series):
            empty = [s.metric for s in series if not s.values]
            raise ToolError(f"No data for {empty} on {unit}.")

        t0 = min(s.timestamps[0] for s in series)
        xs = [[(t - t0).total_seconds() / 60 for t in s.timestamps] for s in series]

        if self.render_terminal:
            # Terminal plot, so the judges see it on the shared screen immediately.
            tplt.clear_figure()
            tplt.theme("pro")
            tplt.plotsize(100, 25)
            tplt.title(f"{unit} · last {minutes:g} min")
            tplt.xlabel("minutes")
            for i, (s, x) in enumerate(zip(series, xs)):
                tplt.plot(x, s.values, label=METRIC_LABELS[s.metric], yside="left" if i == 0 else "right")
            tplt.show()

        # PNG for Slack / the README.
        fig = Figure(figsize=(10, 4.5), dpi=120)
        ax = fig.subplots()
        colors = ["#2563eb", "#dc2626"]
        ax.plot(xs[0], series[0].values, color=colors[0], label=METRIC_LABELS[series[0].metric])
        ax.set_ylabel(METRIC_LABELS[series[0].metric], color=colors[0])
        ax.set_xlabel(f"minutes since {t0.strftime('%H:%M:%S')} UTC")
        if len(series) == 2:
            ax2 = ax.twinx()
            ax2.plot(xs[1], series[1].values, color=colors[1], label=METRIC_LABELS[series[1].metric])
            ax2.set_ylabel(METRIC_LABELS[series[1].metric], color=colors[1])
        ax.set_title(f"LARR-E · {unit} · {series[0].experiment}")
        ax.grid(alpha=0.3)
        fig.tight_layout()
        out = self.cfg.output.plots_dir / f"{unit}_{'_vs_'.join(metrics)}_{int(time.time())}.png"
        fig.savefig(out)

        result: dict[str, Any] = {
            "unit": unit,
            "png_path": str(out),
            "rendered_in_terminal": self.render_terminal,
            "summaries": [s.summary() for s in series],
        }
        if len(series) == 2:
            # Align the second series onto the first's timestamps before correlating.
            x0 = np.array([t.timestamp() for t in series[0].timestamps])
            x1 = np.array([t.timestamp() for t in series[1].timestamps])
            y1_on_x0 = np.interp(x0, x1, series[1].values)
            y0 = np.array(series[0].values)
            if len(y0) > 2 and y0.std() > 0 and y1_on_x0.std() > 0:
                result["pearson_r"] = round(float(np.corrcoef(y0, y1_on_x0)[0, 1]), 3)
        return result

    # Colours for plan charts: dark, high-contrast, readable on a projector and in Slack's dark mode.
    PLAN_STYLE = {"bg": "#0b1220", "panel": "#111a2e", "grid": "#22304d", "text": "#e6edf7", "muted": "#8a9bb8",
                  "target": "#2dd4bf", "measured": {"temperature": "#f59e0b", "stirring": "#60a5fa"},
                  "off": "#ef4444", "proposed": "#e879f9"}

    def _plan_series(self, plan: ExperimentPlan) -> tuple[float, dict[str, tuple[list[float], list[float]]]]:
        """Plan start (epoch s) and, per setpoint metric, (minutes since start, values)."""
        start = parse_timestamp(plan.started_at).timestamp() if plan.started_at else time.time()
        elapsed_min = max(1.0, min((time.time() - start) / 60, plan.duration_h * 60))
        out = {}
        for metric in ("temperature", "stirring"):
            if plan.target(metric) is None:
                continue
            s, _ = self.get_series(metric, plan.unit, elapsed_min + 1)
            out[metric] = ([(t.timestamp() - start) / 60 for t in s.timestamps], list(s.values))
        if not out:
            raise ToolError("The plan sets no temperature or stirring speed to compare against.")
        return start, out

    def _draw_plan(self, fig: Any, plan: ExperimentPlan, series: dict[str, tuple[list[float], list[float]]],
                   upto_min: float, proposed: dict[str, float] | None = None) -> dict[str, float]:
        """Draw the plan chart with readings up to `upto_min`. Returns minutes off target per metric.
        `proposed` (metric -> value) adds a second dashed line from now to the end: a change not yet applied."""
        from matplotlib.patches import Rectangle

        st = self.PLAN_STYLE
        fig.clear()
        fig.patch.set_facecolor(st["bg"])
        end_min = plan.duration_h * 60
        metrics = list(series)
        axes = fig.subplots(len(metrics), 1, sharex=True, squeeze=False)[:, 0]
        fig.subplots_adjust(left=0.08, right=0.97, top=0.80, bottom=0.12, hspace=0.18)
        off_target_min: dict[str, float] = {}
        headline = []
        for ax, metric in zip(axes, metrics):
            t_all, v_all = series[metric]
            pts = [(t, v) for t, v in zip(t_all, v_all) if t <= upto_min]
            ts, vs = [p[0] for p in pts], [p[1] for p in pts]
            target = plan.target(metric)
            color = st["measured"][metric]
            ax.set_facecolor(st["panel"])
            for side in ax.spines.values():
                side.set_visible(False)
            ax.tick_params(colors=st["muted"], labelsize=9)
            ax.grid(color=st["grid"], lw=0.8)
            # off-target stretches, each labelled with its length
            off, run_start = 0.0, None
            for i, (t, v) in enumerate(pts):
                nxt = ts[i + 1] if i + 1 < len(ts) else t + 10 / 60
                bad = plan.off_target(metric, t * 60, v)
                if bad:
                    ax.axvspan(t, nxt, color=st["off"], alpha=0.22, lw=0)
                    off += nxt - t
                    run_start = t if run_start is None else run_start
                if run_start is not None and (not bad or i + 1 == len(pts)):
                    ax.annotate(f"off target\n{(nxt if bad else t) - run_start:.1f} min",
                                xy=(run_start, plan.target(metric, run_start * 60)),
                                xytext=(4, -26), textcoords="offset points", color=st["off"], fontsize=8, weight="bold")
                    run_start = None
            off_target_min[metric] = round(off, 1)
            # target: a glowing dashed line across the whole plan, stepping at each approved change
            steps = plan.target_steps(metric)
            sx, sy = [t for t, _ in steps] + [end_min], [v for _, v in steps] + [steps[-1][1]]
            for lw, alpha in ((9, 0.06), (5, 0.12)):
                ax.plot(sx, sy, color=st["target"], lw=lw, alpha=alpha, drawstyle="steps-post")
            ax.plot(sx, sy, color=st["target"], ls=(0, (6, 4)), lw=2, drawstyle="steps-post", gid="target")
            ax.text(end_min, target, f" target {target:g}", color=st["target"], fontsize=8, va="bottom", ha="right")
            if proposed and proposed.get(metric) is not None:
                new, now_min = proposed[metric], min(max(upto_min, 0.0), end_min)
                ax.plot([now_min, end_min], [new, new], color=st["proposed"], ls="--", lw=2.2, gid="proposed")
                ax.text(end_min, new, f" proposed {new:g}", color=st["proposed"], fontsize=8, weight="bold",
                        va="bottom" if new >= target else "top", ha="right")
            if vs:
                lo = min(min(vs), target)
                ax.fill_between(ts, vs, lo - abs(lo) * 0.05, color=color, alpha=0.18, lw=0)
                for lw, alpha in ((7, 0.10), (4, 0.18)):
                    ax.plot(ts, vs, color=color, lw=lw, alpha=alpha, solid_capstyle="round")
                ax.plot(ts, vs, color=color, lw=2)
                ax.plot(ts[-1:], vs[-1:], "o", color=color, ms=7, mec=st["bg"], mew=1.5)
                unit = "°C" if metric == "temperature" else "rpm"
                headline.append((f"{vs[-1]:.1f} {unit}" if metric == "temperature" else f"{vs[-1]:.0f} {unit}", color))
            ax.axvline(min(upto_min, end_min), color=st["muted"], ls=":", lw=1)
            ax.set_ylabel(METRIC_LABELS[metric], color=st["muted"], fontsize=9)
        axes[-1].set_xlim(-0.02 * end_min, end_min * 1.02)
        axes[-1].set_xlabel("minutes since the plan started", color=st["muted"], fontsize=9)
        # headline: plan name, big live numbers, progress bar
        fig.text(0.08, 0.93, f"{plan.name} · {plan.unit}", color=st["text"], fontsize=15, weight="bold")
        x = 0.08
        for label, color in headline:
            fig.text(x, 0.855, label, color=color, fontsize=13, weight="bold")
            x += 0.16
        done = max(0.0, min(1.0, upto_min / end_min)) if end_min else 0.0
        fig.text(0.97, 0.93, f"{min(upto_min, end_min):.0f} of {end_min:.0f} min", color=st["muted"], fontsize=10, ha="right")
        fig.patches.extend([
            Rectangle((0.60, 0.865), 0.37, 0.018, transform=fig.transFigure, color=st["grid"]),
            Rectangle((0.60, 0.865), 0.37 * done, 0.018, transform=fig.transFigure, color=st["target"]),
        ])
        return off_target_min

    def plot_plan(self, plan: ExperimentPlan, proposed: dict[str, float] | None = None) -> dict[str, Any]:
        """PNG of the plan against what happened: one panel per setpoint (temperature, stirring) with
        the target as a dashed line and off-target stretches shaded. The /plan page draws the same.
        `proposed` draws a setpoint change that is waiting for approval as a second dashed line."""
        from matplotlib.figure import Figure

        start, series = self._plan_series(plan)
        fig = Figure(figsize=(10, 2.3 * len(series) + 1.4), dpi=120)
        off_target_min = self._draw_plan(fig, plan, series, (time.time() - start) / 60, proposed)
        out = self.cfg.output.plots_dir / f"{plan.unit}_plan_{time.time_ns()}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(out, facecolor=fig.get_facecolor())
        return {"unit": plan.unit, "png_path": str(out), "off_target_min": off_target_min}

    def replay_plan_gif(self, plan: ExperimentPlan, frames: int = 36, seconds: float = 5.0) -> dict[str, Any]:
        """Animated GIF of the whole run: the measured lines draw themselves along the target line,
        then the last frame holds. For the end-of-experiment message."""
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure
        from PIL import Image

        start, series = self._plan_series(plan)
        upto_end = min((time.time() - start) / 60, plan.duration_h * 60)
        fig = Figure(figsize=(10, 2.3 * len(series) + 1.4), dpi=80)
        canvas = FigureCanvasAgg(fig)
        images, off_target_min = [], {}
        for i in range(frames):
            off_target_min = self._draw_plan(fig, plan, series, upto_end * (i + 1) / frames)
            canvas.draw()
            images.append(Image.frombuffer("RGBA", canvas.get_width_height(), canvas.buffer_rgba()).convert(
                "P", palette=Image.Palette.ADAPTIVE, colors=128))
        out = self.cfg.output.plots_dir / f"{plan.unit}_replay_{int(time.time())}.gif"
        out.parent.mkdir(parents=True, exist_ok=True)
        step_ms = int(seconds * 1000 / frames)
        images[0].save(out, save_all=True, append_images=images[1:], loop=0,
                       duration=[step_ms] * (frames - 1) + [3000], optimize=True)
        return {"unit": plan.unit, "gif_path": str(out), "off_target_min": off_target_min}

    # ------------------------------------------------------------------ live state

    def _live_state(self) -> dict[str, Any]:
        path = self.cfg.output.state_path
        try:
            return json.loads(path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _with_ages(self, state: dict[str, Any]) -> dict[str, Any]:
        """Add age_s and stale to every reading, so an old value can't pass for a live one."""
        now = datetime.now(timezone.utc)
        limit = self.cfg.output.stale_after_s

        def mark(entry: dict[str, Any]) -> None:
            try:
                age = (now - parse_timestamp(entry["timestamp"])).total_seconds()
            except (KeyError, TypeError, ValueError):
                entry["stale"] = True
                return
            entry["age_s"] = round(age)
            entry["stale"] = age > limit

        for unit_state in state.values():
            if not isinstance(unit_state, dict):
                continue
            for key, entry in unit_state.items():
                if key == "od" and isinstance(entry, dict):
                    for channel in entry.values():
                        mark(channel)
                elif isinstance(entry, dict) and "timestamp" in entry:
                    mark(entry)
        return state

    def get_live_status(self, unit: str | None = None, include_history: bool = False) -> dict[str, Any]:
        state = self._live_state()
        if not include_history:  # charts only; keep it out of the agent's context
            state = {u: {k: v for k, v in s.items() if k not in ("history", "history_t")} for u, s in state.items()}
        state = self._with_ages(state)
        now = datetime.now(timezone.utc)
        for s in state.values():  # say how old the data is, so a stopped job isn't reported as live
            try:
                age = round((now - parse_timestamp(s["last_seen"])).total_seconds())
            except (KeyError, TypeError, ValueError):
                continue
            s["seconds_since_last_reading"] = age
            if age > self.cfg.output.stale_after_s:  # spelled out in the data: weaker models skip instructions more than facts
                s["stale"] = True
                s["note"] = (f"STALE: the newest reading is {age // 60} min old. The jobs that publish these "
                             "readings are probably stopped; these values are NOT current.")
        if not state:
            return {"available": False, "note": "listener.py is not running or has seen no telemetry yet."}
        if unit:
            if unit not in state:
                raise ToolError(f"No live telemetry for {unit}. Known units: {sorted(state)}")
            return {"available": True, unit: state[unit]}
        return {"available": True, **state}

    def audit_log(self, hours: float = 1, kind: str = "all", limit: int = 50) -> dict[str, Any]:
        """Hardware actions from LARR-E's own audit log, newest last.

        Front ends (Slack, API, CLI) log each decision: who asked, who approved. The agent logs each
        action's result. So this answers "what did LARR-E do" without the Pi's database.
        """
        if kind not in AUDIT_KINDS:
            raise ToolError(f"kind must be one of {sorted(AUDIT_KINDS)}")
        if not 0 < hours <= 24 * 30:
            raise ToolError("hours must be between 0 and 720")
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        path = self.cfg.output.audit_path
        entries: list[dict[str, Any]] = []
        for line in (path.read_text().splitlines() if path.exists() else []):
            try:
                record = json.loads(line)
                when = parse_timestamp(record["at"])
            except (ValueError, KeyError, TypeError):
                continue
            if when >= cutoff and AUDIT_KINDS[kind](record.get("tool", ""), record.get("args") or {}):
                entries.append(record)
        entries = entries[-limit:]
        return {
            "hours": hours, "kind": kind, "count": len(entries), "entries": entries,
            "note": "Entries with 'decision' are approvals from Slack, the API or the terminal; entries "
                    "with 'result' are what happened when LARR-E carried an action out. Pumps here are "
                    "uncalibrated: durations are recorded, not volumes.",
        }

    def list_recent_alerts(self, limit: int = 10) -> dict[str, Any]:
        path = self.cfg.output.alerts_path
        if not path.exists():
            return {"alerts": []}
        lines = path.read_text().strip().splitlines()[-limit:]
        return {"alerts": [json.loads(line) for line in lines if line]}
