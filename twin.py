"""Digital twin of a Pioreactor cluster: virtual units that look like real ones to LARR-E.

Each virtual unit runs a small physical model (thermostat, stirring, logistic growth that
depends on temperature and aeration, dilution by the pumps). The twin speaks the same
interfaces LARR-E uses on real hardware, so nothing in LARR-E knows the difference:

* MQTT telemetry on pioreactor/<unit>/<exp>/<job>/<attr>, same payload shapes as real jobs
* MQTT control:   .../<job>/<attr>/set  and  .../<job>/$state/set -> disconnected
* REST on the leader paths LARR-E calls (subset of Pioreactor's web API, including 202 task polling):
    POST /api/workers/<unit>/jobs/run/job_name/<job>/experiments/<exp>
    POST /api/workers/<unit>/jobs/stop/job_name/<job>/experiments/<exp>
    GET  /api/workers/<unit>/jobs/running
    GET  /api/workers/<unit>/experiment
    POST /api/experiment_profiles            GET /api/experiment_profiles
    GET  /api/experiments/<exp>/experiment_profiles/running
    GET  /unit_api/task_results/<task_id>
* Twin-only controls for rehearsals and stage demos:
    GET  /twin/state
    POST /twin/fault   {"unit": "twin1", "fault": "heater_fail" | "stir_stall" | "bubble" |
                        "contamination" | "vial_removed" | "clear"}

Why not Pioreactor's own TESTING mode? Its mock hardware (core/pioreactor/utils/mock.py) is
for unit tests: OD growth is switched off (max_gr = 0), temperature is a fixed sine wave around
25 °C that ignores the heater, and stirring always reports the initial RPM. It can't show a
setpoint being reached or a fault being caught.

Time: schedules (experiment profiles, job timing) run in real time, so LARR-E's plan clock and
the twin agree. Biology is sped up by --bio-speed (default 10: a 10-minute demo shows about 2 h
of growth, roughly a 7-fold OD rise). Much faster, and healthy exponential growth outruns the
listener's rolling-median OD spike rule, which then reports it as a spike. Growth rate is
reported per real hour, consistent with the OD curve LARR-E plots.

    python twin.py --units twin1,twin2 --experiment larre-demo --mqtt-host 127.0.0.1 --port 8790
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import random
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import paho.mqtt.client as mqtt

log = logging.getLogger("larre.twin")

AMBIENT_C = 22.0
JOBS = {"stirring", "temperature_automation", "od_reading", "growth_rate_calculating"}
PUMPS = {"add_media": 1.0, "remove_waste": -1.0, "add_alt_media": 1.0}
PUMP_ML_PER_S = 0.5
# Thermostat, from Pioreactor 26.11 on a 20 ml v1.0 (pio01's model) and pio01's readings on 17 Oct.
# See docs/twin-fidelity.md. --temp-speed divides the times for stage demos.
TEMP_INFERENCE_S = 225.0  # INFERENCE_EVERY_N_SECONDS: one temperature reading per cycle
HEAT_TAU_S = 720.0  # pio01's first heat-up fits a first-order warm-up with a 12.3 min time constant
COOL_TAU_S = 600.0  # not measured on pio01
MAX_TARGET_TEMP_C = 63.0  # thermostat clamp: max_temp_to_reduce_heating on a 20 ml v1.0
FAULTS = {"heater_fail", "stir_stall", "bubble", "contamination", "vial_removed"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_t(value: Any) -> float:
    """Profile time -> seconds. Bare numbers are hours, as in Pioreactor profiles."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value) * 3600
    m = re.fullmatch(r"\s*([0-9]*\.?[0-9]+)\s*([smhd]?)\s*", str(value))
    if not m:
        raise ValueError(f"cannot parse time {value!r}")
    n, u = float(m.group(1)), m.group(2) or "h"
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[u]


# ---------------------------------------------------------------------------- physics


@dataclass
class VirtualReactor:
    unit: str
    experiment: str
    bio_speed: float = 10.0
    temperature_c: float = AMBIENT_C
    measured_rpm: float = 0.0
    od: float = 0.05
    volume_ml: float = 14.0
    carrying_capacity: float = 2.0
    mu_max_per_h: float = 1.2  # E. coli-like at the optimum
    heat_tau_s: float = HEAT_TAU_S  # thermostat time constant towards the setpoint
    cool_tau_s: float = COOL_TAU_S  # passive cooling towards ambient
    next_temp_at: float | None = None  # when the next temperature is published; None = at the next tick
    jobs: dict[str, dict[str, Any]] = field(default_factory=dict)  # running job -> settings
    faults: set[str] = field(default_factory=set)
    _ln_od_history: list[tuple[float, float]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    # cardinal temperature model (Rosso et al. 1993) with E. coli-like cardinal values
    T_MIN, T_OPT, T_MAX = 8.0, 37.0, 46.0

    def temp_factor(self) -> float:
        t, lo, opt, hi = self.temperature_c, self.T_MIN, self.T_OPT, self.T_MAX
        if not lo < t < hi:
            return 0.0
        num = (t - hi) * (t - lo) ** 2
        den = (opt - lo) * ((opt - lo) * (t - opt) - (opt - hi) * (opt + lo - 2 * t))
        return max(0.0, num / den)

    def aeration_factor(self) -> float:
        return min(1.0, 0.35 + self.measured_rpm / 400.0)

    def mu_per_sim_h(self) -> float:
        mu = self.mu_max_per_h * self.temp_factor() * self.aeration_factor()
        if "contamination" in self.faults:
            mu += 1.5  # a faster-growing contaminant takes over
        return mu

    def step(self, dt: float) -> None:
        """Advance dt real seconds."""
        with self.lock:
            # temperature: the Pioreactor heater can only heat, never cool below ambient
            temp = self.jobs.get("temperature_automation")
            heating = temp is not None and "heater_fail" not in self.faults
            target = max(AMBIENT_C, float(temp.get("target_temperature", AMBIENT_C))) if heating else AMBIENT_C
            tau = self.heat_tau_s if heating else self.cool_tau_s
            self.temperature_c += (target - self.temperature_c) * (1 - math.exp(-dt / tau))

            # stirring
            stir = self.jobs.get("stirring")
            target_rpm = float(stir.get("target_rpm", 500)) if stir and "stir_stall" not in self.faults else 0.0
            self.measured_rpm += (target_rpm - self.measured_rpm) * (1 - math.exp(-dt / 3.0))

            # growth (logistic), sped up by bio_speed
            if "vial_removed" not in self.faults:
                sim_h = dt * self.bio_speed / 3600
                r = self.mu_per_sim_h()
                k = self.carrying_capacity
                self.od = k / (1 + (k / self.od - 1) * math.exp(-r * sim_h))

    def dose(self, pump: str, ml: float) -> float:
        with self.lock:
            if PUMPS[pump] > 0:
                self.od *= self.volume_ml / (self.volume_ml + ml)
                self.volume_ml += ml
            else:
                self.volume_ml = max(14.0, self.volume_ml - ml)  # efflux tube holds the level
            return self.volume_ml

    def observed_od(self) -> float:
        if "vial_removed" in self.faults:
            return max(0.0, random.gauss(0.002, 0.001))
        od = self.od * (1 + random.gauss(0, 0.004))
        if "bubble" in self.faults:
            self.faults.discard("bubble")  # one-shot artefact
            od *= 3.0
        return max(0.0, od)

    def growth_rate_real_h(self, observed_od: float) -> float:
        """Slope of ln(OD) over the last ~2 minutes, per real hour (like growth_rate_calculating)."""
        t = time.monotonic()
        self._ln_od_history.append((t, math.log(max(observed_od, 1e-4))))
        self._ln_od_history = [p for p in self._ln_od_history if t - p[0] <= 120]
        if len(self._ln_od_history) < 3:
            return 0.0
        xs, ys = zip(*self._ln_od_history)
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        var = sum((x - mx) ** 2 for x in xs)
        return 0.0 if var == 0 else sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / var * 3600


# ---------------------------------------------------------------------------- tasks and profiles


@dataclass
class ProfileRun:
    job_id: int
    unit: str  # unit the profile was launched on (the "leader")
    experiment: str
    filename: str
    name: str
    dry_run: bool
    started_at: str = field(default_factory=now_iso)
    timers: list[threading.Timer] = field(default_factory=list)
    started_jobs: set[tuple[str, str]] = field(default_factory=set)
    done: threading.Event = field(default_factory=threading.Event)


class Twin:
    def __init__(self, units: list[str], experiment: str, mqtt_host: str, mqtt_port: int,
                 mqtt_auth: tuple[str, str] | None = None, bio_speed: float = 10.0,
                 od_interval_s: float = 5.0, slow_interval_s: float | None = None, profile_dir: Path | None = None,
                 temp_speed: float = 1.0):
        self.experiment = experiment
        self.reactors = {u: VirtualReactor(u, experiment, bio_speed, heat_tau_s=HEAT_TAU_S / temp_speed,
                                           cool_tau_s=COOL_TAU_S / temp_speed) for u in units}
        self.od_interval_s = od_interval_s
        # slow_interval_s, if given, sets both the temperature and stirring cadence (the end-to-end test uses it).
        self.temp_interval_s = slow_interval_s if slow_interval_s is not None else TEMP_INFERENCE_S / temp_speed
        self.stir_interval_s = slow_interval_s if slow_interval_s is not None else 10.0
        self._next_od = self._next_stir = 0.0
        self._last_tick: float | None = None
        self.tasks: dict[str, dict[str, Any]] = {}
        self.profiles: dict[str, str] = {}
        self.runs: dict[int, ProfileRun] = {}
        self._job_ids = 100
        self.profile_dir = profile_dir
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                                  client_id=f"larre-twin-{uuid.uuid4().hex[:6]}")
        if mqtt_auth:
            self.client.username_pw_set(*mqtt_auth)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.mqtt_host, self.mqtt_port = mqtt_host, mqtt_port

    # ------------------------------------------------------------ MQTT

    def topic(self, unit: str, job: str, attr: str) -> str:
        return f"pioreactor/{unit}/{self.experiment}/{job}/{attr}"

    def publish(self, topic: str, payload: Any, retain: bool = False) -> None:
        body = payload if isinstance(payload, str) else json.dumps(payload)
        self.client.publish(topic, body, qos=0, retain=retain)

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.error("MQTT connect failed: %s", reason_code)
            return
        for u in self.reactors:
            client.subscribe(f"pioreactor/{u}/+/+/+/set")
        log.info("MQTT connected to %s:%s", self.mqtt_host, self.mqtt_port)

    def _on_message(self, client, userdata, msg):
        parts = msg.topic.split("/")
        if len(parts) != 6:
            return
        _, unit, exp, job, attr, _set = parts
        reactor = self.reactors.get(unit)
        if reactor is None or exp != self.experiment:
            return  # real Pioreactor jobs ignore commands for another experiment
        value = msg.payload.decode(errors="replace")
        if attr == "$state":
            if value == "disconnected":
                self.stop_job(unit, job)
            return
        with reactor.lock:
            settings = reactor.jobs.get(job)
            if settings is None:
                return  # setting a job that isn't running does nothing, as on real hardware
            try:
                settings[attr] = float(value)
            except ValueError:
                settings[attr] = value
            if job == "temperature_automation" and attr == "target_temperature":
                settings[attr] = clamp_target_temperature(settings[attr])
                value = str(settings[attr])
        self.publish(self.topic(unit, job, attr), value, retain=True)
        log.info("%s: %s.%s = %s", unit, job, attr, value)

    # ------------------------------------------------------------ jobs

    def start_job(self, unit: str, job: str, options: dict[str, Any]) -> dict[str, Any]:
        reactor = self.reactors[unit]
        opts = {k.replace("-", "_"): v for k, v in (options or {}).items()}
        if job in PUMPS:
            ml = float(opts["ml"]) if "ml" in opts else float(opts.get("duration", 0)) * PUMP_ML_PER_S
            if ml <= 0:
                raise ValueError("pump needs ml or duration")
            vol = reactor.dose(job, ml)
            self.publish(self.topic(unit, "dosing_events", "dosing_event"),
                         {"event": job, "volume_change": ml, "source_of_event": "twin", "timestamp": now_iso()})
            log.info("%s: %s %.2f ml (volume %.1f ml)", unit, job, ml, vol)
            return {"job": job, "ml": ml}
        if job not in JOBS:
            raise LookupError(f"Job {job} is not available on this twin (known: {sorted(JOBS | set(PUMPS))}).")
        with reactor.lock:
            if job in reactor.jobs:
                raise RuntimeError(f"{job} is already running on {unit}.")  # like Pioreactor's 'job already running'
            settings: dict[str, Any] = {}
            if job == "stirring":
                settings["target_rpm"] = float(opts.get("target_rpm", 500))
            if job == "temperature_automation":
                settings["automation_name"] = opts.get("automation_name", "thermostat")
                settings["target_temperature"] = clamp_target_temperature(float(opts.get("target_temperature", 30)))
                reactor.next_temp_at = None  # like Pioreactor: a reading as the thermostat starts
            reactor.jobs[job] = settings
        self.publish(self.topic(unit, job, "$state"), "ready", retain=True)
        for k, v in settings.items():
            self.publish(self.topic(unit, job, k), str(v), retain=True)
        log.info("%s: started %s %s", unit, job, settings)
        return {"job": job, "settings": settings}

    def stop_job(self, unit: str, job: str) -> bool:
        reactor = self.reactors[unit]
        with reactor.lock:
            was = reactor.jobs.pop(job, None) is not None
        if was:
            self.publish(self.topic(unit, job, "$state"), "disconnected", retain=True)
            log.info("%s: stopped %s", unit, job)
        return was

    # ------------------------------------------------------------ profiles

    def upload_profile(self, filename: str, body: str) -> None:
        import yaml

        name = Path(filename).name
        if not re.fullmatch(r"[A-Za-z0-9._-]+\.ya?ml", name):
            raise ValueError("Validation error: must be a valid unix filename ending in .yaml")
        try:
            doc = yaml.safe_load(body)
        except yaml.YAMLError as exc:
            raise ValueError(f"Validation error. {exc}") from exc
        if not isinstance(doc, dict) or doc.get("version") != "1.0":
            raise ValueError('Validation error. version must be the string "1.0"')
        if "experiment_profile_name" not in doc:
            raise ValueError("Validation error. experiment_profile_name is required")
        for _unit, _job, action in self._actions(doc):
            if action.get("type") not in {"start", "stop", "update", "pause", "resume", "log"}:
                raise ValueError(f"Validation error. action type {action.get('type')!r} is not simulated by the twin")
            parse_t(action.get("t", action.get("hours_elapsed", 0)))
        with self.lock:
            if name in self.profiles:
                raise ValueError("A profile already exists with that filename. Choose another.")
            self.profiles[name] = body
        if self.profile_dir:
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            (self.profile_dir / name).write_text(body)

    def _actions(self, doc: dict[str, Any]):
        units = list(self.reactors)
        for job, spec in ((doc.get("common") or {}).get("jobs") or {}).items():
            for unit in units:
                for a in spec.get("actions", []):
                    yield unit, job, a
        for unit, block in (doc.get("pioreactors") or {}).items():
            for job, spec in ((block or {}).get("jobs") or {}).items():
                for a in spec.get("actions", []):
                    yield unit, job, a

    def run_profile(self, leader_unit: str, path: str, experiment: str, dry_run: bool) -> dict[str, Any]:
        import yaml

        name = Path(path).name
        body = self.profiles.get(name)
        if body is None:
            raise FileNotFoundError(f"[Errno 2] No such file or directory: '{path}'")
        doc = yaml.safe_load(body)
        self._job_ids += 1
        run = ProfileRun(self._job_ids, leader_unit, experiment, name, doc["experiment_profile_name"], dry_run)
        last_t = 0.0
        for unit, job, action in self._actions(doc):
            if unit not in self.reactors:
                log.warning("profile %s targets unknown unit %s; skipping", name, unit)
                continue
            t = parse_t(action.get("t", action.get("hours_elapsed", 0)))
            last_t = max(last_t, t)
            timer = threading.Timer(t, self._do_action, args=(run, unit, job, action))
            timer.daemon = True
            run.timers.append(timer)
        finisher = threading.Timer(last_t + 0.5, self._finish_run, args=(run,))
        finisher.daemon = True
        run.timers.append(finisher)
        with self.lock:
            self.runs[run.job_id] = run
        for timer in run.timers:
            timer.start()
        log.info("profile %s started on %s (%d actions, ends after %.0f s)%s",
                 run.name, leader_unit, len(run.timers) - 1, last_t, " [DRY RUN]" if dry_run else "")
        return {"job_id": run.job_id, "experiment_profile_name": run.name}

    def _do_action(self, run: ProfileRun, unit: str, job: str, action: dict[str, Any]) -> None:
        kind = action.get("type")
        if run.dry_run or kind == "log":
            log.info("profile %s: %s %s on %s (dry run or log)", run.name, kind, job, unit)
            return
        try:
            if kind in {"start", "resume"}:
                self.start_job(unit, job, action.get("options") or {})
                run.started_jobs.add((unit, job))
            elif kind in {"stop", "pause"}:
                self.stop_job(unit, job)
            elif kind == "update":
                for k, v in (action.get("options") or {}).items():
                    self.publish(f"pioreactor/{unit}/{run.experiment}/{job}/{k}/set", str(v))
        except Exception as exc:  # a failing action is logged, the profile carries on (as Pioreactor does)
            log.warning("profile %s: %s %s on %s failed: %s", run.name, kind, job, unit, exc)

    def _finish_run(self, run: ProfileRun) -> None:
        run.done.set()
        with self.lock:
            self.runs.pop(run.job_id, None)
        log.info("profile %s finished", run.name)

    def kill_profiles(self, experiment: str | None = None, job_id: int | None = None) -> int:
        """Like `pio kill --job-name experiment_profile` (whole experiment, as on real Pioreactor) or
        `pio kill --job-id N`: cancel pending actions and stop the jobs the profile started."""
        with self.lock:
            runs = [r for r in self.runs.values()
                    if (job_id is not None and r.job_id == job_id) or (job_id is None and r.experiment == experiment)]
            for r in runs:
                self.runs.pop(r.job_id, None)
        for r in runs:
            for timer in r.timers:
                timer.cancel()
            for unit, job in r.started_jobs:
                self.stop_job(unit, job)
            r.done.set()
        return len(runs)

    # ------------------------------------------------------------ simulation loop

    def run_loop(self, tick_s: float = 1.0) -> None:
        while not self.stop_event.is_set():
            self.tick(time.monotonic())
            self.stop_event.wait(tick_s)

    def tick(self, now: float) -> None:
        """Advance the physics to `now` (seconds, any monotonic clock) and publish whatever is due."""
        dt = 0.0 if self._last_tick is None else now - self._last_tick
        self._last_tick = now
        for r in self.reactors.values():
            r.step(dt)
        if now >= self._next_od:
            self._next_od = now + self.od_interval_s
            for r in self.reactors.values():
                if "od_reading" in r.jobs:
                    od = r.observed_od()
                    ts = now_iso()
                    self.publish(self.topic(r.unit, "od_reading", "od1"),
                                 {"od": round(od, 6), "angle": "90", "channel": "1", "timestamp": ts})
                    if "growth_rate_calculating" in r.jobs:
                        gr = r.growth_rate_real_h(od)
                        self.publish(self.topic(r.unit, "growth_rate_calculating", "growth_rate"),
                                     {"growth_rate": round(gr, 5), "timestamp": ts})
        # Temperature: one reading as the thermostat starts, then one per inference cycle (per unit).
        for r in self.reactors.values():
            if "temperature_automation" in r.jobs and (r.next_temp_at is None or now >= r.next_temp_at - 1e-9):
                r.next_temp_at = now + self.temp_interval_s
                t = r.temperature_c + random.gauss(0, 0.03)
                self.publish(self.topic(r.unit, "temperature_automation", "temperature"),
                             {"temperature": round(t, 3), "timestamp": now_iso()})
        if now >= self._next_stir:
            self._next_stir = now + self.stir_interval_s
            for r in self.reactors.values():
                if "stirring" in r.jobs:
                    self.publish(self.topic(r.unit, "stirring", "measured_rpm"),
                                 {"measured_rpm": round(max(0.0, r.measured_rpm + random.gauss(0, 2)), 1),
                                  "timestamp": now_iso()})

    def start(self) -> None:
        self.client.connect_async(self.mqtt_host, self.mqtt_port, 30)
        self.client.loop_start()
        threading.Thread(target=self.run_loop, name="twin-sim", daemon=True).start()

    def stop(self) -> None:
        self.stop_event.set()
        for r in list(self.runs.values()):
            for timer in r.timers:
                timer.cancel()
        self.client.loop_stop()
        self.client.disconnect()

    # ------------------------------------------------------------ tasks (202 + polling, like Pioreactor)

    def task(self, fn) -> dict[str, Any]:
        task_id = uuid.uuid4().hex
        try:
            self.tasks[task_id] = {"task_id": task_id, "status": "succeeded", "result": fn()}
        except Exception as exc:
            self.tasks[task_id] = {"task_id": task_id, "status": "failed", "error": str(exc)}
        return {"unit": "twin", "task_id": task_id, "result_url_path": f"/unit_api/task_results/{task_id}",
                "status": "accepted"}

    def state(self) -> dict[str, Any]:
        out = {}
        for u, r in self.reactors.items():
            with r.lock:
                out[u] = {
                    "experiment": r.experiment, "jobs": {k: dict(v) for k, v in r.jobs.items()},
                    "temperature_c": round(r.temperature_c, 2), "measured_rpm": round(r.measured_rpm, 1),
                    "od": round(r.od, 4), "volume_ml": round(r.volume_ml, 2),
                    "mu_per_sim_h": round(r.mu_per_sim_h(), 3), "faults": sorted(r.faults),
                }
        return {"bio_speed": next(iter(self.reactors.values())).bio_speed, "units": out,
                "profiles_running": [vars(r) | {"timers": len(r.timers), "started_jobs": sorted(r.started_jobs),
                                                "done": r.done.is_set()} for r in self.runs.values()]}


# ---------------------------------------------------------------------------- HTTP


def make_handler(twin: Twin):
    run_re = re.compile(r"^/api/(?:workers|units)/([^/]+)/jobs/(run|stop)/job_name/([^/]+)/experiments/([^/]+)$")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.debug("http %s", fmt % args)

        def _reply(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> Any:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            path = unquote(self.path.split("?")[0])
            if path.startswith("/unit_api/task_results/"):
                t = twin.tasks.get(path.rsplit("/", 1)[1])
                return self._reply(200, t) if t else self._reply(202, {"status": "pending"})
            if path == "/twin/state":
                return self._reply(200, twin.state())
            if path == "/api/experiment_profiles":
                return self._reply(200, [{"file": n} for n in twin.profiles])
            m = re.fullmatch(r"/api/experiments/([^/]+)/experiment_profiles/running", path)
            if m:
                return self._reply(200, [{"job_name": "experiment_profile", "job_id": r.job_id,
                                          "experiment": r.experiment,
                                          "settings": {"experiment_profile_name": r.name, "start_time_utc": r.started_at,
                                                       "profile_filename": r.filename}}
                                         for r in list(twin.runs.values()) if r.experiment == m.group(1)])
            m = re.fullmatch(r"/api/(?:workers|units)/([^/]+)/experiment", path)
            if m:
                if m.group(1) not in twin.reactors:
                    return self._reply(404, {"error": f"Worker {m.group(1)} not found."})
                return self._reply(200, {"pioreactor_unit": m.group(1), "is_active": 1, "experiment": twin.experiment,
                                         "model_name": "digital_twin", "model_version": "1.0"})
            m = re.fullmatch(r"/api/(?:workers|units)/([^/]+)/jobs/running", path)
            if m and m.group(1) in twin.reactors:
                return self._reply(200, [{"job_name": j, "settings": s} for j, s in twin.reactors[m.group(1)].jobs.items()])
            if path in {"/", "/api/health"}:
                return self._reply(200, {"twin": True, "units": list(twin.reactors)})
            self._reply(404, {"error": f"Not found: {path}"})

        def do_POST(self):
            path = unquote(self.path.split("?")[0])
            try:
                body = self._body()
            except json.JSONDecodeError:
                return self._reply(400, {"error": "Invalid JSON body"})
            if path == "/api/experiment_profiles":
                try:
                    twin.upload_profile(str(body.get("filename", "")), str(body.get("body", "")))
                except ValueError as exc:
                    return self._reply(400, {"error": str(exc)})
                return self._reply(200, {"status": "success"})
            if path == "/unit_api/jobs/stop":
                if body.get("job_id") is not None:
                    jid = int(body["job_id"])
                    return self._reply(202, twin.task(lambda: {"killed": twin.kill_profiles(job_id=jid)}))
                return self._reply(400, {"error": "the twin supports stopping by job_id only on /unit_api/jobs/stop"})
            if path == "/twin/fault":
                unit, fault = body.get("unit"), body.get("fault")
                r = twin.reactors.get(unit)
                if r is None or (fault not in FAULTS and fault != "clear"):
                    return self._reply(400, {"error": f"unit must be one of {list(twin.reactors)}, fault one of {sorted(FAULTS)} or 'clear'"})
                with r.lock:
                    r.faults.clear() if fault == "clear" else r.faults.add(fault)
                log.warning("%s: fault %s", unit, fault)
                return self._reply(200, {"unit": unit, "faults": sorted(r.faults)})
            m = run_re.match(path)
            if not m:
                return self._reply(404, {"error": f"Not found: {path}"})
            unit, verb, job, exp = m.groups()
            if unit not in twin.reactors or exp != twin.experiment:
                return self._reply(404, {"error": f"Worker(s) {unit} not found, not active, or not assigned to experiment {exp}."})
            if verb == "stop":
                if job == "experiment_profile":
                    return self._reply(202, twin.task(lambda: {"killed": twin.kill_profiles(exp)}))
                return self._reply(202, twin.task(lambda: per_unit(unit, {"stopped": twin.stop_job(unit, job)})))
            args, options = body.get("args") or [], body.get("options") or {}
            if job == "experiment_profile":
                if len(args) < 3 or args[0] != "execute":
                    return self._reply(400, {"error": "expected args ['execute', <path>, <experiment>]"})
                return self._reply(202, twin.task(lambda: twin.run_profile(unit, args[1], args[2], "dry-run" in options)))
            return self._reply(202, twin.task(lambda: per_unit(unit, twin.start_job(unit, job, options))))

    return Handler


def clamp_target_temperature(value: Any) -> Any:
    """Pioreactor's thermostat clamps its target to 0..max_temp_to_reduce_heating (63 °C on a 20 ml v1.0)."""
    return min(max(float(value), 0.0), MAX_TARGET_TEMP_C) if isinstance(value, (int, float)) else value


def per_unit(unit: str, value: Any) -> dict[str, Any]:
    """Pioreactor 26.11 reduces a job run/stop across units to {unit: {"ok", "unit", "value"}} (web/tasks.py)."""
    return {unit: {"ok": True, "unit": unit, "value": value}}


def serve(twin: Twin, host: str, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(twin))
    threading.Thread(target=server.serve_forever, name="twin-http", daemon=True).start()
    return server


def main() -> None:
    from config import settings

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--units", default="twin1", help="comma-separated virtual unit names")
    p.add_argument("--experiment", default=settings.pioreactor.experiment or "larre-demo")
    p.add_argument("--mqtt-host", default=settings.mqtt.host)
    p.add_argument("--mqtt-port", type=int, default=settings.mqtt.port)
    p.add_argument("--host", default="0.0.0.0", help="HTTP bind address")
    p.add_argument("--port", type=int, default=8790, help="HTTP port (the twin's 'leader URL')")
    p.add_argument("--bio-speed", type=float, default=10.0, help="growth speed-up; schedules stay real-time")
    p.add_argument("--od-interval", type=float, default=5.0)
    p.add_argument("--temp-speed", type=float, default=1.0,
                   help="thermostat speed-up: 1 = a real 20 ml Pioreactor (a reading every 225 s, about 25 min "
                        "to reach 37 °C); 10 for a stage demo")
    p.add_argument("--profile-dir", type=Path, help="also save uploaded profiles here")
    a = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    m = settings.mqtt
    twin = Twin([u.strip() for u in a.units.split(",") if u.strip()], a.experiment, a.mqtt_host, a.mqtt_port,
                (m.username, m.password or "") if m.username else None, a.bio_speed, a.od_interval,
                profile_dir=a.profile_dir, temp_speed=a.temp_speed)
    twin.start()
    server = serve(twin, a.host, a.port)
    log.info("twin up: units=%s experiment=%s http://%s:%d mqtt=%s:%d bio_speed=%g temp_speed=%g",
             list(twin.reactors), a.experiment, a.host, a.port, a.mqtt_host, a.mqtt_port, a.bio_speed, a.temp_speed)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        twin.stop()


if __name__ == "__main__":
    main()
