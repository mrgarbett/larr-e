"""End-to-end: MQTT broker + digital twin (two units) + LARR-E listener/recorder + LARR-E tools.

No hardware, Slack or Claude API. Needs an MQTT broker binary: `amqtt` (pip install amqtt) or
`mosquitto`. Skipped if neither is installed.  Runs in about 30 s.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import Settings  # noqa: E402
from tools import PioreactorTools  # noqa: E402
import twin as twin_mod  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(cond, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = cond()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture(scope="module")
def broker(tmp_path_factory):
    port = free_port()
    d = tmp_path_factory.mktemp("broker")
    if shutil.which("mosquitto"):
        (d / "m.conf").write_text(f"listener {port} 127.0.0.1\nallow_anonymous true\n")
        cmd = ["mosquitto", "-c", str(d / "m.conf")]
    elif shutil.which("amqtt"):
        (d / "b.yaml").write_text(
            f"listeners:\n  default:\n    type: tcp\n    bind: 127.0.0.1:{port}\n"
            "plugins:\n  amqtt.plugins.authentication.AnonymousAuthPlugin:\n    allow_anonymous: true\n"
        )
        cmd = ["amqtt", "-c", str(d / "b.yaml")]
    else:
        pytest.skip("no MQTT broker (mosquitto or amqtt) installed")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    wait_for(lambda: socket.socket().connect_ex(("127.0.0.1", port)) == 0, 10, "broker")
    yield port
    proc.terminate()


@pytest.fixture(scope="module")
def cluster(broker, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("cluster")
    tw = twin_mod.Twin(["twin1", "twin2"], "demo", "127.0.0.1", broker, bio_speed=20,
                       od_interval_s=0.5, slow_interval_s=0.5)
    for r in tw.reactors.values():
        r.heat_tau_s = 3.0  # fast thermostat so the test sees it reach the setpoint
    tw.start()
    http_port = free_port()
    server = twin_mod.serve(tw, "127.0.0.1", http_port)
    url = f"http://127.0.0.1:{http_port}"

    base = Settings()
    cfg = dataclasses.replace(
        base,
        mqtt=dataclasses.replace(base.mqtt, host="127.0.0.1", port=broker, username=None, password=None),
        pioreactor=dataclasses.replace(
            base.pioreactor, default_unit=None, experiment=None, pump_transport="rest", dry_run=False,
            leaders={"twin1": url, "twin2": url}, record_db=True, db_path=tmp / "cluster.sqlite",
        ),
        anomaly=dataclasses.replace(base.anomaly, min_samples=4, cooldown_s=1),
        output=dataclasses.replace(
            base.output, runtime_dir=tmp, state_path=tmp / "live_state.json", alerts_path=tmp / "alerts.jsonl",
            audit_path=tmp / "audit.jsonl", plans_path=tmp / "plans.json", plots_dir=tmp / "plots",
            auto_triage=False, slack_webhook_url=None,
        ),
    )
    import listener

    threading.Thread(target=listener.run, args=(cfg, False), daemon=True).start()
    yield tw, cfg, url
    server.shutdown()
    tw.stop()


def test_twin_cluster_end_to_end(cluster):
    tw, cfg, _ = cluster
    tools = PioreactorTools(cfg, render_terminal=False)

    # two plans on two units of the same twin leader (same experiment)
    out1 = tools.start_experiment(name="main", duration_h=0.05, temperature_c=37, rpm=180, unit="twin1")
    out2 = tools.start_experiment(name="other", duration_h=0.05, temperature_c=30, rpm=300, unit="twin2")
    assert out1["ok"] and out2["ok"]

    # the profile, run by the twin's leader, starts the jobs; telemetry flows to the listener
    wait_for(lambda: {"stirring", "temperature_automation", "od_reading"} <= set(tw.reactors["twin1"].jobs), 5, "jobs on twin1")
    state = wait_for(lambda: (lambda s: s if {"twin1", "twin2"} <= set(s) else None)(tools._live_state()), 10, "live state")
    assert state["twin1"]["experiment"] == "demo"

    # the thermostat reaches the setpoint and stirring the target
    wait_for(lambda: abs(tools._live_state()["twin1"].get("temperature", {}).get("value", 0) - 37) < 0.5, 20, "37 °C")
    wait_for(lambda: abs(tools._live_state()["twin1"].get("stirring", {}).get("value", 0) - 180) < 10, 20, "180 rpm")

    # stopping twin2's plan must not stop twin1's (same experiment, same leader)
    stopped = tools.stop_experiment(unit="twin2")
    assert stopped["ok"], stopped
    time.sleep(1)
    assert tw.reactors["twin2"].jobs == {}
    assert "stirring" in tw.reactors["twin1"].jobs, "stopping one plan stopped another unit's plan"

    # the recorder stored history, so LARR-E's data tools work for twin units
    ts = wait_for(lambda: (lambda r: r if r["summary"]["points"] >= 3 else None)(
        tools.get_recent_timeseries("od", unit="twin1", minutes=5)), 10, "recorded OD")
    assert ts["experiment"] == "demo"

    # a short plan ends on schedule: the Pioreactor-style stop actions run on the twin
    tools.start_experiment(name="short", duration_h=6 / 3600, rpm=200, od_reading=False, unit="twin2")
    wait_for(lambda: "stirring" in tw.reactors["twin2"].jobs, 5, "short plan start")
    wait_for(lambda: tw.reactors["twin2"].jobs == {}, 15, "scheduled stop at t=6s")
    assert "stirring" in tw.reactors["twin1"].jobs

    # cluster view
    units = {u["unit"]: u for u in tools.list_units()["units"]}
    assert set(units) >= {"twin1", "twin2"} and units["twin1"]["plan"].endswith("[running]")
    assert tools.stop_experiment(unit="twin1")["ok"]


def test_twin_fault_raises_alert(cluster):
    tw, cfg, _ = cluster
    tools = PioreactorTools(cfg, render_terminal=False)
    tw.reactors["twin1"].jobs.pop("stirring", None)
    tools.start_job("stirring", unit="twin1", options={"target_rpm": 500})
    time.sleep(17)  # refill the 30-reading window at 500 rpm (the previous test left 180 rpm, below the 200 rpm floor)
    tw.reactors["twin1"].faults.add("stir_stall")
    alerts_path = cfg.output.alerts_path

    def stall_alert():
        if not alerts_path.exists():
            return None
        alerts = [json.loads(line) for line in alerts_path.read_text().splitlines() if line]
        return [a for a in alerts if a["unit"] == "twin1" and a["metric"] == "stirring"]

    alerts = wait_for(stall_alert, 15, "stirring stall alert")
    assert alerts[0]["severity"] == "critical"
    tw.reactors["twin1"].faults.clear()


def test_twin_rejects_like_pioreactor(cluster):
    tw, cfg, url = cluster
    from leader_api import LeaderAPI, LeaderAPIError

    api = LeaderAPI(url)
    with pytest.raises(LeaderAPIError, match="not assigned to experiment"):
        api.run_job("twin1", "stirring", "other-experiment")
    with pytest.raises(LeaderAPIError, match="version"):
        api.upload_profile("bad.yaml", "version: 1.0\nexperiment_profile_name: x\n")
    api.upload_profile("dup.yaml", 'version: "1.0"\nexperiment_profile_name: x\n')
    with pytest.raises(LeaderAPIError, match="already exists"):
        api.upload_profile("dup.yaml", 'version: "1.0"\nexperiment_profile_name: x\n')


def test_twin_job_result_uses_pioreactor_envelope(cluster):
    """Pioreactor 26.11 reduces a job run/stop to {unit: {"ok", "unit", "value"}} (web/tasks.py, fanout_success).
    Clients that check `ok` per unit (upstream's tools.py does) must see the same shape from the twin."""
    tw, cfg, url = cluster
    from leader_api import LeaderAPI

    api = LeaderAPI(url)
    api.stop_job("twin2", "od_reading", "demo")
    result = api.run_job("twin2", "od_reading", "demo")
    assert result["twin2"]["ok"] is True and result["twin2"]["unit"] == "twin2"
    result = api.stop_job("twin2", "od_reading", "demo")
    assert result["twin2"]["ok"] is True


def test_slack_card_apply_changes_the_twin(cluster):
    """The live Slack card against the twin: a stepper only proposes; Apply moves the (simulated) stirrer."""
    tw, cfg, _ = cluster
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_live_card import LiveSlack, action_ids
    from test_slack_plan import bot_with
    from slack_bot import Conversation

    bot = bot_with(cfg, LiveSlack())
    bot.submit = lambda fn, *a: fn(*a)
    for job in list(tw.reactors["twin1"].jobs):  # earlier tests leave jobs running; the profile would refuse to start them
        tw.stop_job("twin1", job)
    out = bot.tools.start_experiment(name="card", duration_h=0.5, temperature_c=30, rpm=300, unit="twin1")
    assert out["ok"], out
    bot.on_tool_result(Conversation(channel="C1", thread_ts="1.1", requester="U1"), "start_experiment", {}, out, False)
    assert bot.tools.plans.get("twin1").slack_card_ts == "1"
    wait_for(lambda: abs(tools_rpm(bot, "twin1") - 300) < 15, 20, "300 rpm")

    assert bot.step("twin1", "U1", "larre_step_rpm_up") is None
    time.sleep(2)
    assert abs(tools_rpm(bot, "twin1") - 300) < 15  # proposing moved nothing
    assert "larre_apply" in action_ids(bot.client.updates[-1])

    assert bot.apply("twin1", "U2") is None
    wait_for(lambda: abs(tools_rpm(bot, "twin1") - 350) < 15, 20, "350 rpm after Apply")
    plan = bot.tools.plans.get("twin1")
    assert plan.target("stirring") == 350 and plan.expected_state()["target_rpm"] == 350
    card = bot.client.updates[-1]
    assert card["ts"] == "1" and "larre_apply" not in action_ids(card)
    assert bot.tools.stop_experiment(unit="twin1")["ok"]


def tools_rpm(bot, unit):
    return bot.tools._live_state().get(unit, {}).get("stirring", {}).get("value", 0)
