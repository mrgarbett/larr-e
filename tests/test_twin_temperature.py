"""The twin's thermostat against Pioreactor 26.11 and pio01's own readings (docs/twin-fidelity.md, gap 1).

Known-right answers:
- Cadence: 26.11's TemperatureAutomationJob infers a temperature every 225 s on a 20 ml v1.0
  (INFERENCE_EVERY_N_SECONDS), and pio01's readings on 17 Oct arrived 224-225 s apart.
- Warm-up: pio01's first heat-up on 17 Oct (runtime/cluster.sqlite, 14:44:52 UTC onwards). A
  first-order fit gives a setpoint of about 37 °C and a time constant of 12.3 min.
- Clamp: 26.11's thermostat clamps targets to max_temp_to_reduce_heating, 63 °C on a 20 ml v1.0.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import twin as twin_mod

# (seconds since the first reading, °C) from pio01, 17 Oct, 14:44:52 UTC
PIO01_WARMUP = [(0.0, 24.81), (234.4, 28.17), (458.8, 30.60), (683.8, 32.45), (908.8, 34.26), (1583.8, 36.26)]


def make_twin(**kw) -> tuple[twin_mod.Twin, list[tuple[float, str, object]]]:
    tw = twin_mod.Twin(["twin1"], "demo", "127.0.0.1", 1883, **kw)
    sent: list[tuple[float, str, object]] = []
    clock = {"now": 0.0}
    tw.publish = lambda topic, payload, retain=False: sent.append((clock["now"], topic, payload))  # type: ignore[method-assign]
    tw._clock = clock  # type: ignore[attr-defined]
    return tw, sent


def run(tw: twin_mod.Twin, until_s: float, step_s: float = 1.0) -> None:
    clock = tw._clock  # type: ignore[attr-defined]
    t = clock["now"]
    while t <= until_s + 1e-9:
        clock["now"] = t
        tw.tick(t)
        t += step_s


def temperature_times(sent) -> list[float]:
    return [t for t, topic, _ in sent if topic.endswith("/temperature_automation/temperature")]


def test_temperature_is_published_when_the_thermostat_starts_then_every_225_s():
    tw, sent = make_twin()
    run(tw, 0)
    tw.start_job("twin1", "temperature_automation", {"target_temperature": 37})
    run(tw, 700)
    assert temperature_times(sent) == pytest.approx([0, 225, 450, 675], abs=1.01)


def test_warm_up_follows_pio01_first_heat_up():
    tw, sent = make_twin()
    tw.reactors["twin1"].temperature_c = PIO01_WARMUP[0][1]
    tw.start_job("twin1", "temperature_automation", {"target_temperature": 37})
    for t_s, measured in PIO01_WARMUP:
        run(tw, t_s)
        assert tw.reactors["twin1"].temperature_c == pytest.approx(measured, abs=1.0), f"at {t_s:.0f} s"


def test_target_above_63_c_is_clamped_like_a_20ml_v1_thermostat():
    tw, sent = make_twin()
    tw.start_job("twin1", "temperature_automation", {"target_temperature": 80})
    assert tw.reactors["twin1"].jobs["temperature_automation"]["target_temperature"] == 63.0

    msg = SimpleNamespace(topic="pioreactor/twin1/demo/temperature_automation/target_temperature/set", payload=b"70")
    tw._on_message(None, None, msg)
    assert tw.reactors["twin1"].jobs["temperature_automation"]["target_temperature"] == 63.0
    echoed = [p for _, topic, p in sent if topic.endswith("/temperature_automation/target_temperature")]
    assert float(echoed[-1]) == 63.0


def test_temp_speed_runs_cadence_and_warm_up_faster_for_stage_demos():
    tw, sent = make_twin(temp_speed=10)
    tw.reactors["twin1"].temperature_c = PIO01_WARMUP[0][1]
    run(tw, 0)
    tw.start_job("twin1", "temperature_automation", {"target_temperature": 37})
    run(tw, 70, step_s=0.5)
    assert temperature_times(sent) == pytest.approx([0, 22.5, 45, 67.5], abs=0.51)
    # 1583.8 s of pio01 warm-up in a tenth of the time
    run(tw, 158.4, step_s=0.5)
    assert tw.reactors["twin1"].temperature_c == pytest.approx(36.26, abs=1.0)


def test_temperature_payload_matches_pioreactor_struct():
    tw, sent = make_twin()
    tw.start_job("twin1", "temperature_automation", {"target_temperature": 37})
    run(tw, 0)
    payload = [p for _, topic, p in sent if topic.endswith("/temperature_automation/temperature")][0]
    body = payload if isinstance(payload, dict) else json.loads(payload)
    assert set(body) == {"temperature", "timestamp"}
