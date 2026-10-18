# LARR-E 🧫💬

**Talk to your bioreactors.** LARR-E replaces siloed SCADA alarm panels with a collaborative
AI agent that you drive from the terminal or from Slack. It listens to a [Pioreactor](https://pioreactor.com) cluster's open MQTT
telemetry, flags anomalies in real time, triages them with Claude, and lets anyone on the team
act on the hardware in plain English, with hard safety limits and a human confirmation step.

```
 ┌────────────── Pioreactor cluster ──────────────┐
 │ worker1 … workerN ──MQTT──► leader (Mosquitto) │◄───────── commands ──────────┐
 │                     leader SQLite (history)    │                              │
 └──────────┬─────────────────────────┬───────────┘                              │
            │ telemetry               │ read-only SQL                            │
            ▼                         ▼                                          │
   listener.py ──alerts + triage──► slack_bot.py ◄──► Slack (threads, Approve)   │
   (rolling-median    │                   │                                      │
    anomaly rules)    │                   ▼                                      │
            ▲         └──triage──► agent.py: Claude tool-use loop ──► tools.py ──┘
            │                      (CLI: exec / query / chat)   (pump, temp, RPM, SQL, plots)
   inject_anomaly.py (stage demo only)
```

| File | Role |
|---|---|
| `config.py` | Every setting (broker, DB path, safety limits, detector thresholds, model), overridable by env var |
| `tools.py` | Hardware and data toolset: pumps, temperature, stirring, start/stop jobs, read-only SQL, time series, plots |
| `agent.py` | `larr-e` CLI plus the Claude tool-routing loop (`exec`, `query`, `chat`, `status`, `alerts`, `listen`, `slack`) |
| `listener.py` | MQTT anomaly monitor: OD spike/drop, temperature drop/out-of-band, growth crash, stirring stall |
| `slack_bot.py` | Slack front end: mentions, DMs, `/larr-e`, threaded conversations, Approve/Deny buttons, alert posts with plots |
| `slack_manifest.yaml` | Paste-in manifest that creates the Slack app with the right scopes and Socket Mode |
| `api_server.py` | Local HTTP API (`larr-e api`): status, alerts, data, agent jobs and approvals, behind a token |
| `web/dashboard.html` | Dashboard for small, old browsers, including the Nintendo 3DS browser. Served by the API |
| `pocket3ds/` | Native Nintendo 3DS app (PocketJS) and the bridge that pairs it with the API. See [pocket3ds/README.md](pocket3ds/README.md) |
| `experiment.py` | Experiment plans: request → Pioreactor experiment profile YAML, persisted plan, expected state, update timing |
| `leader_api.py` | Small client for the leader's REST API: start/stop jobs, upload and run profiles |
| `twin.py` | Digital twin: virtual Pioreactors with a growth/thermal model, the same REST and MQTT interfaces, and injectable faults (`larr-e twin`) |
| `schema.py` | The Pioreactor SQLite tables LARR-E reads, shared by the recorder and the demo injector |
| `inject_anomaly.py` | Demo helper that publishes Pioreactor-format fake telemetry to trigger alerts on cue |

---

## 1. How it talks to the Pioreactor

LARR-E uses the same MQTT conventions as the Pioreactor web UI. No firmware changes are needed.

| Purpose | Topic | Payload |
|---|---|---|
| OD reading | `pioreactor/<unit>/<exp>/od_reading/od1` | `{"od": 0.81, "angle": "90", "channel": "1", "timestamp": "…Z"}` |
| Growth rate | `pioreactor/<unit>/<exp>/growth_rate_calculating/growth_rate` | `{"growth_rate": 0.34, "timestamp": "…Z"}` |
| Temperature | `pioreactor/<unit>/<exp>/temperature_automation/temperature` | `{"temperature": 30.1, "timestamp": "…Z"}` |
| Stirring | `pioreactor/<unit>/<exp>/stirring/measured_rpm` | `{"measured_rpm": 498, "timestamp": "…Z"}` |
| Change a setting | `pioreactor/<unit>/<exp>/<job>/<attr>/set` | `32` |
| Stop a job | `pioreactor/<unit>/<exp>/<job>/$state/set` | `disconnected` |
| Run a pump/job | REST, not MQTT: `POST /api/workers/<unit>/jobs/run/job_name/add_media/experiments/<exp>` | `{"options": {"ml": 1.0}, "args": []}` |
| LARR-E alerts | `larre/alerts/<unit>` | alert JSON (kept outside `pioreactor/` so it is never stored) |
| LARR-E triage | `larre/triage/<unit>` | `{"alert_id": "…", "unit": "…", "note": "…"}` |

History comes from the leader's SQLite database
(`/home/pioreactor/.pioreactor/storage/pioreactor.sqlite`): `od_readings`, `temperature_readings`,
`growth_rates`, `stirring_rates`, `dosing_events`, `experiments`. It is opened read-only, and an
SQLite authorizer rejects any statement that is not a read.

> **Starting jobs uses REST, not MQTT.** Changing a setting (`…/set`) and stopping a job
> (`$state/set`) over MQTT work. Starting one over MQTT doesn't: Pioreactor 26.11 has no subscriber
> for `pioreactor/<unit>/<exp>/run/<job>`, and on a real Pi a publish there started nothing. LARR-E
> therefore starts jobs through the leader's web API (`LARRE_PUMP_TRANSPORT=rest`, the default:
> `POST /api/workers/<unit>/jobs/run/job_name/<job>/experiments/<exp>`, the same call the Pioreactor
> UI and MCP server make), or `pios` when running on the leader. Any other value is refused.

---

## 2. Installation

### Requirements
- Python 3.11+
- A Pioreactor cluster (leader plus at least one worker) on the same network, **or** any MQTT broker for rehearsal
- An Anthropic API key (`export ANTHROPIC_API_KEY=…`)
- Calibrated pumps if you want to dose by **ml** (otherwise dose by duration, e.g. "run the media pump for 2 seconds")

### Install
With [uv](https://docs.astral.sh/uv/) (versions pinned in `uv.lock`):
```bash
git clone <your repo> larr-e && cd larr-e
uv sync                          # creates .venv; add --group dev for the tests
uv run larr-e status             # or `source .venv/bin/activate` and just `larr-e ...`
uv run --group dev pytest        # tests (no hardware, Slack or API key)
```
Without uv (e.g. on the Pi):
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
alias larr-e="python $(pwd)/agent.py"
```

### Configure (env vars; full list in `config.py`)
Put settings, and above all secrets, in a `.env` file: `cp .env.example .env`, then fill it in.
It's gitignored, and LARR-E loads it at startup. Anything already set in the environment wins.
`.claude/settings.json` stops Claude Code from reading `.env`, so keys don't end up in a model's
context; you edit the file yourself. Use a dedicated Anthropic key for LARR-E with a spend limit,
and revoke it after the event. The same variables can also be exported in the shell:
```bash
export ANTHROPIC_API_KEY=sk-ant-...
export LARRE_MQTT_HOST=pioreactor.local      # the leader's hostname or IP
export LARRE_MQTT_USERNAME=pioreactor        # only if your broker needs auth:
export LARRE_MQTT_PASSWORD=raspberry         #   see [mqtt] in the leader's ~/.pioreactor/config.ini
export LARRE_DEFAULT_UNIT=worker1
export LARRE_EXPERIMENT=larre-demo        # optional; otherwise taken from live telemetry or the DB
export LARRE_DB_PATH=/home/pioreactor/.pioreactor/storage/pioreactor.sqlite
# Slack: see section 4. (LARRE_SLACK_WEBHOOK_URL is a no-bot fallback; don't set it while the bot runs, or alerts post twice.)
export LARRE_EFFORT=low                      # snappier answers on stage (default: medium)
export LARRE_API_TOKEN=pick-a-long-random-string   # for `larr-e api`, the 3DS bridge and dashboard
export LARRE_PUBLIC_URL=http://<laptop-ip>:8787        # optional: "Adjust in LARR-E" buttons in Slack open /plan (private channel only: the link carries the token)
export LARRE_PUMP_TRANSPORT=rest             # default; start jobs via the leader's web API (see section 1)
export LARRE_LEADER_URL=http://pioreactor.local   # the leader's web UI address
export LARRE_LEADER_UNIT=pioreactor          # leader's unit name; defaults to the target unit (one-Pi cluster)
export LARRE_UPDATE_EVERY_H=2                # default spacing of Slack progress updates
```

**LLM backend.** The default is Claude (`ANTHROPIC_API_KEY`). To use open models through
[Hugging Face Inference Providers](https://huggingface.co/docs/inference-providers/en/index)
and your HF credits instead:
```bash
export LARRE_LLM=hf
export HF_TOKEN=hf_...                                 # fine-grained, "Make calls to Inference Providers"
export LARRE_HF_MODEL=openai/gpt-oss-120b:fastest      # default; ":cheapest" or ":groq" etc. pick the provider
```
Or use **Google Gemini's free tier** (key from [aistudio.google.com](https://aistudio.google.com/apikey),
no card needed; check your model limits at aistudio.google.com/rate-limit):
```bash
export LARRE_LLM=gemini
export GEMINI_API_KEY=...
export LARRE_GEMINI_MODEL=gemini-3.8-flash             # default
```
Tools, safety limits, confirmations and approvals behave the same on both backends. They live in
Python, not in the model. Open models are less reliable at multi-step tool use, so rehearse with the
backend you'll present with.

Run the agent **on the leader** to get direct DB access. To run it from a laptop, copy the DB over
first (`scp pioreactor@pioreactor.local:.pioreactor/storage/pioreactor.sqlite ./runtime/`) and point
`LARRE_DB_PATH` at the copy.

### Digital twin and mixed clusters

`twin.py` runs virtual Pioreactors on a laptop. Each one has a thermostat that heats toward its
setpoint (it can't cool below ambient), stirring that ramps to its target, logistic growth that
depends on temperature and stirring, and pumps that dilute. The twin answers on the same REST
paths and MQTT topics as a real leader, so LARR-E drives it with the same code, profiles
included. Biology runs 10× faster by default (`--bio-speed`), but schedules run in real time.

Pioreactor's own test mode is not a twin. Its mock hardware keeps OD flat, fakes temperature as a
sine wave around 25 °C whatever the heater does, and always reports the initial RPM.

```bash
larr-e twin --units twin1,twin2 --experiment larre-demo --mqtt-host 127.0.0.1 --port 8790   # terminal 1
export LARRE_MQTT_HOST=127.0.0.1 LARRE_PUMP_TRANSPORT=rest LARRE_RECORD_DB=1 \
       LARRE_DB_PATH=./runtime/cluster.sqlite \
       LARRE_LEADERS="twin1=http://127.0.0.1:8790,twin2=http://127.0.0.1:8790"
larr-e listen                                                # terminal 2: alerts + records history
larr-e exec "run twin1 at 37 degrees and 180 rpm for 10 minutes, update me every 3 minutes"
curl -X POST localhost:8790/twin/fault -d '{"unit":"twin1","fault":"stir_stall"}'     # trip an alert
curl localhost:8790/twin/state                               # ground truth, for checking LARR-E
```

Faults: `heater_fail`, `stir_stall`, `bubble` (one OD spike), `contamination` (growth jumps),
`vial_removed` (OD to zero), and `clear` to remove them all.

**Adding the real Pi.** Point MQTT at the Pi's broker (`LARRE_MQTT_HOST=pioreactor.local`, plus
credentials) and start the twin with the same `--mqtt-host`, so both report in one place. Leave
the Pi's unit out of `LARRE_LEADERS`; it uses `LARRE_LEADER_URL`. Leave `LARRE_EXPERIMENT` unset:
LARR-E asks each unit's leader which experiment the unit is assigned to. `/larr-e units` lists
every unit, real and twin. Caveat: the Pi's `mqtt_to_db_streaming` may also store the twin's
readings in the Pi's database, so give the twin its own experiment name.

**Profiles and shared experiments.** On Pioreactor, stopping `experiment_profile` through the
leader stops every profile in the experiment. `stop_experiment` therefore finds its own run by
`profile_filename` and stops it by `job_id`, leaving other units' plans running. The twin copies
Pioreactor's behaviour so the tests would catch a regression.

### Rehearse with no hardware
```bash
docker run -d --name mqtt -p 1883:1883 eclipse-mosquitto:2 mosquitto -c /mosquitto-no-auth.conf
export LARRE_MQTT_HOST=127.0.0.1 LARRE_DEFAULT_UNIT=worker1 LARRE_EXPERIMENT=demo \
       LARRE_DB_PATH=./runtime/demo.sqlite LARRE_DRY_RUN=1
python inject_anomaly.py --seed-history 30 --write-db ./runtime/demo.sqlite --scenario none
larr-e listen                                  # terminal 1
python inject_anomaly.py --scenario od_spike     # terminal 2
larr-e query "plot the last 10 minutes of OD against temperature"
```

---

## 3. Usage

**Start everything with one command.** `./up.sh` opens one tmux window with four panes (listener, API, 3DS
bridge, Slack bot), loads `real.env` and `~/.config/larr-e/secrets.env` in each, and restarts cleanly if it's
already running. Use `./up.sh rehearsal` for the local test setup, and `./down.sh` to stop. In tmux,
click a pane to focus it, `Ctrl-b z` zooms it, and `Ctrl-b d` detaches while everything keeps running
(`tmux attach -t larre` to come back).

```bash
larr-e status                                       # live table, no LLM call
larr-e query "plot the last 10 minutes of OD against temperature"
larr-e query "how much media have we added to worker1 today?"
larr-e exec  "pump 1ml of media into worker1"       # asks you to confirm before acting
larr-e exec  "set worker1 to 32 degrees" --dry-run  # prints the MQTT message, sends nothing
larr-e exec  "slow stirring to 400 rpm" --yes       # for an upstream bot that already confirmed
larr-e chat                                         # multi-turn session
larr-e alerts -n 5
larr-e listen [--no-triage]
larr-e slack                                        # Slack bot front end (section 4)
larr-e api                                          # HTTP API + 3DS/phone dashboard (section 5)
```

**Whole experiments**

```bash
larr-e exec "run the vial at 37 degrees and 180 rpm for 18 hours, update me every 2 hours"
larr-e query "how is the experiment going?"
larr-e exec "stop the experiment"
```

LARR-E doesn't drive a long experiment one command at a time. It turns the request into an
`ExperimentPlan` (`experiment.py`) and from that a Pioreactor
[experiment profile](https://docs.pioreactor.com/user-guide/experiment-profiles-schema) that
starts stirring, the thermostat, OD reading and growth rate at t = 0 and stops them at t = 18 h.
The leader validates the profile on upload, then runs it. **The Pioreactor owns the schedule**, so
the experiment ends correctly even if the laptop, the bot or the Claude API goes away.

LARR-E keeps the plan in `runtime/experiment_plans.json` (one per unit, survives restarts) and uses it to:
- show the approver the exact profile YAML on the Approve/Deny card,
- tell triage and the agent what is *supposed* to be happening (`expected_state`), so a job
  stopping at the planned end isn't reported as a fault,
- post progress updates (status line + plot, no LLM call) to the Slack thread that started the
  experiment every `update_every_h`, and a final message at the end. `/larr-e experiment` shows it on demand.

`plan_experiment` previews without touching anything. `start_experiment` and `stop_experiment` need approval.
LARR-E refuses to start a second plan on a unit that already has one running. Pioreactor's own
profile dry-run executes in real time, so we don't use it as a pre-flight check.

Tests: `pytest tests/` (no hardware, Slack or API key). `tests/test_twin_cluster.py` runs a real
MQTT broker, a two-unit twin, the listener and the tools end to end (about 50 s; needs
`mosquitto` or `pip install amqtt`). To also check the generated profile
against Pioreactor's own schema validator, clone `Pioreactor/pioreactor` and run with
`PYTHONPATH=pioreactor/core GLOBAL_CONFIG=pioreactor/config.dev.ini TESTING=1`.

**Safety model**
- `query` mode never offers hardware tools to the model.
- In `exec` mode every hardware call stops at a confirmation panel unless you pass `--yes`.
  In Slack it stops at an Approve/Deny card. You can limit who may approve, and optionally require
  a second person, and every decision is logged to `runtime/audit.jsonl`.
- Hard limits in Python, whatever the model asks for: ≤ 5 ml or ≤ 30 s per dose, setpoint 20–45 °C,
  0–1500 RPM, and an allow-list of jobs. Change them in `config.py`.
- SQL is read-only three ways: `mode=ro`, a single SELECT/WITH statement, and an SQLite authorizer.
- Claude Opus 5.5 runs with the API's server-side refusal fallback turned on
  (`LARRE_USE_FALLBACKS=0` disables it).

**Detector rules** (rolling median over the last 30 readings per unit and metric; 60 s cooldown per alert type)

| Metric | Fires when | Severity |
|---|---|---|
| OD (od1/od2) | jumps > max(+50 %, +0.1) above median | critical |
| OD | falls > max(−50 %, −0.1) below median | warning |
| Temperature | > 2 °C below median, or outside 22–42 °C | critical |
| Growth rate | < −0.1 h⁻¹ | warning |
| Stirring | < 30 % of a median ≥ 200 RPM | critical |

---

## 4. Slack front end

`slack_bot.py` puts the same agent in Slack. It connects over **Socket Mode** (an outbound
WebSocket), so it works behind NAT and on hackathon Wi-Fi with no public URL.

### Set up (about 5 minutes)
1. Go to <https://api.slack.com/apps>, then **Create New App → From a manifest**. Choose your
   workspace and paste `slack_manifest.yaml`.
2. **Basic Information → App-Level Tokens → Generate**, with scope `connections:write`. This is `SLACK_APP_TOKEN` (`xapp-…`).
3. **Install to Workspace**, then copy the **Bot User OAuth Token**. This is `SLACK_BOT_TOKEN` (`xoxb-…`).
4. Create a `#bioreactor-alerts` channel, run `/invite @LARR-E` in it, and copy its channel ID
   (channel name → *About* → bottom of the panel, `C…`).
5. On the leader (so it can read the SQLite DB):
   ```bash
   export SLACK_BOT_TOKEN=xoxb-... SLACK_APP_TOKEN=xapp-...
   export LARRE_SLACK_ALERT_CHANNEL=C0123456789
   export LARRE_SLACK_APPROVERS=U0AAA,U0BBB          # optional: only these users may approve
   export LARRE_SLACK_ALLOW_SELF_APPROVAL=0          # optional: two-person rule
   larr-e listen    # terminal 1: detects anomalies and publishes alerts and triage over MQTT
   larr-e slack     # terminal 2: posts them to Slack and answers people
   ```

### What people can do in Slack
| Do this | What happens |
|---|---|
| `@LARR-E plot the last 10 minutes of OD against temperature` | Answer in a thread, with the PNG uploaded |
| Reply in that thread: `now add 1 ml of media` | Same conversation continues, no @mention needed |
| DM the bot | A private session with the same tools |
| `/larr-e status` · `/larr-e alerts` | Instant answers with no LLM call |
| `/larr-e pump 1ml of media into worker1` | Posts the request in the channel, then an **Approve / Deny** card |
| An alert lands in `#bioreactor-alerts` | **📈 Plot last 15 min** button; the triage note arrives in the thread; reply there to act, and the agent already knows the alert |

**Hardware approvals.** The agent pauses at each hardware action and posts a card with the exact
tool and arguments, marked *dry run* when `LARRE_DRY_RUN=1`. The first valid click decides. After
`LARRE_SLACK_APPROVAL_TIMEOUT_S` (default 180 s) with no click, the request expires and nothing
moves. Clicks from people outside `LARRE_SLACK_APPROVERS`, or the requester's own click under the
two-person rule, are refused with a private message.

**Limits.** Conversation memory lives in the bot process and is lost on restart. Messages in
threads the bot didn't start are ignored unless they @mention it. `/larr-e` only works in
channels the bot has been invited to.

---

## 5. Nintendo 3DS front ends

There are two ways to use LARR-E from a 3DS. Both run on top of `larr-e api`:

```bash
export LARRE_API_TOKEN=pick-a-long-random-string
larr-e api        # prints http://<this-computer's-LAN-IP>:8787/?k=<token>
```

On Fedora, open the port for your LAN: `sudo firewall-cmd --add-port=8787/tcp` (the rule lasts
until reboot).

**The browser dashboard needs no homebrew.** Open the printed URL in the 3DS's built-in browser,
or on a phone. You get:
- live status, the newest alert and an OD sparkline
- four request buttons
- an Approve/Deny card whenever an agent request wants to move hardware

It uses ES5 JavaScript and a 320 px layout, so the old 3DS browser can run it.

**The native app is the quirky one.** A PocketJS app shows the dashboard on the top screen and
turns the touch screen into the approval gate: Ⓐ approves, Ⓑ denies. It builds on Linux, and has
a simulator for testing without a 3DS. Setup, build, deploy and troubleshooting are in
[pocket3ds/README.md](pocket3ds/README.md).

Approvals from any front end are logged in `runtime/audit.jsonl`, tagged with their source (`api:3ds`,
`api:3ds-browser`, `slack`).

---

## 6. Live presentation script (5 minutes)

### Stage layout
- **Projector:** one terminal split into three panes (tmux), font size 18 or larger:
  - **A (top, full width): `larr-e listen`.** Alerts and triage notes appear here.
  - **B (bottom left): the agent.** Everything you type goes here.
  - **C (bottom right, small): the injector.** Your teammate drives it; judges can see it, since honesty scores points.
- **Table:** the Pioreactor running a culture (or dyed water) with stirring and the thermostat on,
  the media and waste lines primed, and an empty beaker under the waste line. Angle the pump towards the judges.
- **Second screen (or the right half of the projector):** Slack, showing `#bioreactor-alerts`.
  A teammate's laptop is logged in as a *second user*, so the two-person approval is real.
- **In your pocket:** a 3DS running LARR-E, paired through `bun run bridge <3ds-ip>`. Optionally
  put it under a document camera for the finale.

### T-30 min pre-flight checklist
1. `ping pioreactor.local`, then `larr-e status`: the demo unit shows live OD, temperature and RPM.
2. Start `od_reading`, `growth_rate_calculating`, `stirring` (500 RPM) and the thermostat (30 °C) from the Pioreactor UI.
3. `larr-e exec "pump 0.5ml of media" --yes`: **the pump must audibly run.** If not, check `LARRE_LEADER_URL` reaches the leader's web UI.
4. `larr-e query "plot the last 10 minutes of OD against temperature"` renders a chart.
5. `export LARRE_ALERT_COOLDOWN_S=20`, so you can re-trigger if a judge asks.
6. Clear old alerts: `rm runtime/alerts.jsonl`. Leave `larr-e listen` running in pane A.
7. Start `larr-e slack` (in a hidden tmux window) and check that `/larr-e status` answers in
   `#bioreactor-alerts`. Set `LARRE_SLACK_ALLOW_SELF_APPROVAL=0` and put the teammate's user ID
   in `LARRE_SLACK_APPROVERS`.
8. Start `larr-e api` and the 3DS bridge. On the 3DS, the dot must be green and "Health check" must answer.
9. Type the demo commands into a scratch file so you can paste them; typos on stage cost seconds.

### Minute-by-minute

**0:00–0:40 · The problem** (presenter faces the judges, terminal idle)
> "Every bioreactor lab has the same setup: an alarm panel from the 2000s, a vendor SCADA that only
> runs on one Windows PC, and a grad student who gets paged at 3 a.m. and has to drive in to find
> out whether it's a bubble or a dead culture. The data is locked in, the alarms are dumb
> thresholds, and nobody else on the team can see or act. We built LARR-E."

**0:40–1:10 · The architecture** (point at the hardware, then pane A)
> "This is a Pioreactor, an open-source bioreactor. Everything it does is published over MQTT.
> LARR-E listens to that stream (pane A), and gives the whole team an AI operator they can talk
> to, from the terminal or right in Slack where the team already works."

**1:10–1:50 · Ask it anything** (pane B)
```bash
larr-e status
larr-e query "plot the last 10 minutes of OD against temperature on worker1"
```
> "No dashboards to configure. Plain English goes in, and Claude picks the tool, pulls the data
> straight from the Pioreactor's database, and draws it right here, with the correlation."
Point at the terminal chart and the r value in the answer.

**1:50–2:50 · Something goes wrong** (cue the teammate on pane C)
```bash
python inject_anomaly.py --unit worker1 --scenario od_spike --live-baseline
```
> "We can't wait six hours for biology on stage, so my teammate is injecting a fault onto the same
> MQTT topic the real sensor uses. To LARR-E it looks exactly like a real event."
- A red **🚨 CRITICAL · od1** panel appears in pane A, and the same alert lands in Slack.
- Click **📈 Plot last 15 min** in Slack: the chart appears in the alert thread.
- About 10–20 seconds later the **🧠 Triage** note appears, both in pane A and threaded under the
  Slack alert. Read it aloud:
> "It didn't just fire an alarm. It pulled the last few minutes of data, saw OD triple in seconds
> while growth rate and temperature stayed flat, and concluded that this looks like a sensor
> artifact, such as a bubble, rather than biology. Then it suggested a next step."

**2:50–3:50 · Act on the hardware, from Slack** (everyone looks at the pump)
- In the **alert's Slack thread**, type (no @mention needed):
  `dilute it with 1 ml of fresh media`
- The agent already knows which unit and which alert. An **Approve / Deny** card appears showing
  `dose pump=media, ml=1, unit=worker1`.
- Click **Approve** yourself: Slack privately replies *"Two-person rule…"*.
  > "I asked for it, so I can't approve it. Same rule as a lab sign-off."
- Your teammate clicks **Approve** on their laptop. **The pump spins on the table.** Pause and let
  the judges hear it.
- Fallback without Slack, in pane B: `larr-e exec "dilute worker1 with 1ml of fresh media"`, then press **y**.
> "Every hardware action goes through a human gate, and every decision is in the audit log: who
> asked, who approved, and exactly what was sent."

**3:50–4:15 · Safety is code, not vibes**
```bash
larr-e exec "dump 50ml of media into worker1"
```
> "The model can ask for anything, but the limits are enforced in Python: 5 ml maximum, 20 to 45
> degrees, 1500 RPM, and read-only SQL. It tells you the limit instead of finding a workaround."
Optional, if time allows: `larr-e query "turn the heater off"` shows that query mode has no
hardware tools at all.

**4:15–4:45 · One more thing** (pull out the 3DS)
> "Lab alarms deserve better hardware. So here's LARR-E on a Nintendo 3DS."
- The top screen shows the same live dashboard and the alert. Tap **Dilute 0.5 ml** (it's red
  because it moves hardware).
- The touch screen turns into **HARDWARE ACTION: run a pump**. Hold it up and press **Ⓐ**. The pump
  spins.
> "Same agent, same safety limits, same audit log. The approval button is just a lot more fun."

**4:45–5:00 · Close**
> "LARR-E turns a closed, single-screen alarm system into a shared, conversational operator: it
> watches, explains and acts with your approval, on open hardware, from a terminal, Slack, or a
> 3DS. Next: multi-reactor fleet views and auto-generated experiment logs. Thank you."

### If something breaks on stage
| Symptom | Recovery |
|---|---|
| No alert appears | Injector pane: rerun without `--live-baseline` (it warms up the detector itself) or with `--anomaly-samples 6` |
| Triage is slow | Keep talking about the alert. It runs in the background, and `LARRE_EFFORT=low` speeds it up |
| Pump doesn't move | Rerun with `--dry-run` and show the exact MQTT message: "this is what hits the broker" |
| Wi-Fi dies | Hotspot the leader to a phone. Rehearsal mode (section 2) runs entirely on the laptop |
| API error | Show `larr-e status`, `larr-e alerts` and the listener, none of which need the LLM |
| Slack is silent | Check the `larr-e slack` window for `not_in_channel` (run `/invite @LARR-E`); otherwise carry on in the terminal |
| Approval card never resolves | It expires after 180 s with nothing changed; rerun from the terminal with `larr-e exec` |
| 3DS dot is red | Skip the beat, or open the browser dashboard on the 3DS instead (same buttons, same approval card) |

---

## 7. Notes and limits
- Injected readings on a real cluster are stored by the leader under the experiment you name. Use a
  dedicated demo experiment (`--experiment larre-demo`) if your data must stay clean.
- `--live-baseline` waits for one real reading and spikes relative to it, so the injected values
  blend in with real telemetry on a unit that is still running `od_reading`.
- Time windows are anchored on the newest stored reading, not the wall clock, because Raspberry Pis
  have no RTC and clocks can drift.
- The listener keeps `runtime/live_state.json` current, so `status` and the agent's `get_live_status`
  work without querying the database.
