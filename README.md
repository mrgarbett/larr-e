# LARR-E 🧫💬

What if you could talk to a bioreactor?

Existing bioreactors are clunky, require faffing about with a screen, and are near impossible to extend for one's own purposes.

And you have to continually babysit them.

Not anymore! Just chat to experiments via Slack!




##  Installation

### Requirements
- [uv](https://docs.astral.sh/uv/)
- An Anthropic API key (`export ANTHROPIC_API_KEY=…`)

### Install

```bash
git clone <your repo> larr-e && cd larr-e
uv sync                          # creates .venv; add --group dev for the tests
uv run larr-e status             # or `source .venv/bin/activate` and just `larr-e ...`
uv run --group dev pytest        # tests (no hardware, Slack or API key)
```


### Configuration (env vars; full list in `config.py`)

`cp .env.example .env` - Both settings and secrets should go in the .env


**LLM backend.** 


#### Anthropic
The default is Claude (`ANTHROPIC_API_KEY`).


#### Huggingface

```bash
export LARRE_LLM=hf
export HF_TOKEN=hf_...                                 # fine-grained, "Make calls to Inference Providers"
export LARRE_HF_MODEL=openai/gpt-oss-120b:fastest      # default; ":cheapest" or ":groq" etc. pick the provider
```

#### Gemini 
```bash
export LARRE_LLM=gemini
export GEMINI_API_KEY=...
export LARRE_GEMINI_MODEL=gemini-3.8-flash             # default
```

### Digital twin and mixed clusters

- Thermostat that heats toward its setpoint
- Biology runs 10× faster by default
  - Configurable via (`--bio-speed`)
  - Schedules run in real time.


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

Requests are converted into an experiment plan (`experiment.py`), which is then translated into Pioreactor's [experiment profile](https://docs.pioreactor.com/user-guide/experiment-profiles-schema).


## Safety guardrails

- `query` mode never offers hardware tools to the model.
- In `exec` mode every hardware call stops at a confirmation panel unless you pass `--yes`.
  In Slack it stops at an Approve/Deny card. You can limit who may approve, and optionally require
  a second person, and every decision is logged to `runtime/audit.jsonl`.
- Hard limits in Python, whatever the model asks for: ≤ 5 ml or ≤ 30 s per dose, setpoint 20–45 °C,
  0–1500 RPM, and an allow-list of jobs. Change them in `config.py`.
- SQL is read-only three ways: `mode=ro`, a single SELECT/WITH statement, and an SQLite authorizer.
- Claude Opus 5.5 runs with the API's server-side refusal fallback turned on
  (`LARRE_USE_FALLBACKS=0` disables it).

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
