# How close is the twin to a real Pioreactor?

Checked on 17 Oct against two sources. The first is the Pioreactor 26.11.0 source (tag `26.11.0`
on github.com/Pioreactor/pioreactor: `core/pioreactor/background_jobs/`, `automations/temperature/`,
`structs.py`, `models.py` and `packaging/shared-assets/pioreactor/config.example.ini`). The second
is what `pio01` (pioreactor_20ml **v1.0**) actually sent on 17 Oct, which the laptop's listener
recorded in `runtime/cluster.sqlite`: 357 stirring readings, 24 temperatures and 42 OD readings.

Ranked by how much each gap could make LARR-E look better on the twin than it would on the real reactor.

## 1. Temperature: far too fast, far too often (fixed 18 Oct)

**Fixed:** readings now come every 225 s (one as the thermostat starts), the warm-up time constant
is 12 min (from pio01's first heat-up), and targets are clamped at 63 °C. `--temp-speed 10` runs the
thermostat 10× faster for a stage demo. Tests: `tests/test_twin_temperature.py`. Not modelled: the
PID's own dynamics, and the heater pausing during each reading. Passive cooling (10 min) is not
measured on pio01. pio01's third heat-up fitted a slower 23 min time constant, so real warm-ups can
take longer than the twin's.

The comparison before the fix:

| | Real (26.11, 20 ml v1.0) | Twin today |
|---|---|---|
| How often a temperature is published | Every **225 s**. The heater switches off for 29 × 5 s = 145 s of each cycle to infer the temperature | Every 10 s |
| Warm-up | pio01: 24.8 → 36.3 °C in 26 min; 26.1 → 42.9 °C in 30 min. That's a time constant of roughly **10–15 min** | Time constant 90 s, so the setpoint is reached in about 5 min |
| Highest target allowed | Clamped to 63 °C (`max_temp_to_reduce_heating`) | No clamp |
| Controller | PID on the heater duty cycle (Kp 2.6, Kd 4.6), updated once per reading. The duty cycle is published as `heater_duty_cycle` | First-order lag straight to the setpoint |

What this hides: on the twin the plan chart reaches its target almost at once, and triage never sees
a long warm-up. The 20-minute warm-up allowance (`LARRE_TEMP_WARMUP_S=1200`) has only ever been
tested against the fast twin. On pio01, 37 °C takes about 25 min, so it would still be "warming up"
when the allowance runs out. Note too that pio01's second run reached 42.9 °C, above the listener's
42 °C alert ceiling.

## 2. OD: wrong channel, too little noise

| | Real | Twin today |
|---|---|---|
| Channel | `od2`: photodiode 2 at 90°. Channel 1 is the reference (`REF`) and isn't published as OD | `od1` with `angle: "90"`, `channel: "1"` |
| Payload | `{timestamp, angle, od, channel, ir_led_intensity, calibrated: 0}`, plus an `ods` message carrying all channels | No `ir_led_intensity`, `calibrated` or `ods` |
| How often | Every 5 s (`samples_per_second=0.2`) | Every 5 s ✓ |
| Noise | pio01, nearly blank vial: mean 0.057, standard deviation 0.010 (**18 %** of the mean). Max 0.080 | 0.4 % |

What this hides: with real noise, single readings reach about 40 % above the median. That's close to
the 50 % OD-spike rule (`LARRE_OD_SPIKE_REL=0.5`), so false spike alerts are likely on real hardware
and impossible on the twin.

## 3. Stirring: real stirrers stall and recover

| | Real | Twin today |
|---|---|---|
| How often | Every **23 s** (`duration_between_updates_seconds`), with extra readings while the stirrer starts or its setting changes | Every 10 s |
| Steady state | pio01 at 500 RPM: mean 572, standard deviation 55 | Standard deviation 2 |
| Stalls | 77 of 357 pio01 readings were 0 RPM, and one reading was 1714. On 0 RPM, Pioreactor logs "Stirring RPM is 0 - attempting to restart it automatically" and kicks the fan: 0 % → 100 % for 0.75 s each, then back to about its previous duty cycle | `stir_stall` stays at 0 until cleared |
| Settings it publishes | `target_rpm`, `measured_rpm`, `duty_cycle` | `target_rpm`, `measured_rpm` |

What this gives the pitch: a fault that stalls, gets kicked and recovers, the way pio01's does,
lets the twin show the real catch on stage.

## 4. Growth rate: the real one has a warm-up

The real job runs an extended Kalman filter, a smoothing method that estimates growth rate from noisy
OD. It publishes nothing until it has 10 OD samples (`samples_for_od_statistics=10`, about 50 s). It
publishes `growth_rate` and `od_filtered`. The twin starts after 3 samples, uses a 2-minute
regression and has no `od_filtered`.

## 5. Small differences

- Job `$state`: real jobs go `init` → `ready`. The twin publishes `ready` directly.
- Real temperature automations also publish `automation_name`, `heater_duty_cycle` and `latest_event`.
- pio01 has no pump. The twin's pumps are only for the twin-only dosing story, so keep them.

## Order to fix (one at a time, test first)

1. Temperature: publish every 225 s, warm up on a 10–15 min time constant, clamp the target at 63 °C. Make the speed configurable, so a stage demo can still run fast.
2. OD on `od2` with the real payload and realistic noise. Then check the OD-spike rule against it.
3. Stirring every 23 s, realistic RPM noise, and a `stir_flaky` fault that stalls, kicks and recovers.
4. A growth-rate warm-up of 10 samples, and `od_filtered`.
