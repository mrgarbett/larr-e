# LARR-E: working notes for Claude Code and the team

Read this first. It records what was decided and why, the rules for changing the code, and what's
still open. History and planned extensions: `docs/design-notes.md`. Raw material (the ChatGPT design
thread, the organisers' prompting playbook) may be in `notes/`, which is gitignored. Read it only
when asked: it is long, and most of it is superseded by this file.

## What LARR-E is for

The problem, in the words of the scientist on the team: a culture is usually grown in a vial at 37 °C
and 180 rpm for 16–20 hours. Setting that up means a proprietary bioreactor GUI, and then
repeatedly checking on it. LARR-E: **describe the experiment in Slack; LARR-E sets it up, keeps an
eye on it, and sends graphs, updates and alerts, interrupting you only when needed.**

The claim is *reducing the human attention an experiment needs*, not "chat control of a reactor".
Pioreactor already ships an MCP server, so Claude plus MCP can already press buttons from chat.
LARR-E's value is everything around that: approval before hardware moves, a saved plan, triage
that knows what's intentional, and updates that arrive without being asked for.

## Hackathon constraints (Science Works × Inherent, "Reinvent the Wheel", 16–18 Oct 2026)

- **Deadline Sunday 18 Oct, 10:00:** submit the repo URL and commit hash. Pitches run 10:15–12:15:
  3 minutes of problem and demo, then 2 minutes of questions.
- **Judging, equally weighted (10 points each):** scientific impact, technical execution, presentation.
  Communication counts, so keep explanations simple.
- **Repo rules:** a new public repo, a permissive licence (Apache-2.0 is on `alethkit/larr-e`
  main), no commits from before the hackathon, setup that works from a fresh clone.
- **Open: which repo gets submitted.** `mrgarbett/larr-e` main is behind (no LICENSE, none of the
  plan or twin work). Either merge `experiment-plans` there, or submit `alethkit/larr-e` after
  merging `experiment-plans` into its main. The commit hash must contain the work.

## Architecture (one paragraph)

The Pioreactor runs the experiment (jobs, automations, experiment profiles) and owns timing and
control loops. LARR-E (`listener.py`) watches its MQTT telemetry, records it, and flags anomalies.
Claude (`agent.py`) investigates and acts only through `tools.py`, which enforces hard limits.
Hardware actions need human approval (terminal prompt, Slack card, or API). Front ends: CLI, Slack
(`slack_bot.py`), HTTP API plus 3DS (`api_server.py`, `pocket3ds/`). `twin.py` simulates units with
the same interfaces.

## Decisions, and why

1. **Pioreactor owns the schedule; Claude writes the plan.** A request like "37 °C, 180 rpm, 18 h"
   becomes an `ExperimentPlan` (`experiment.py`). That becomes a Pioreactor experiment profile,
   which is uploaded (the leader validates it) and executed on the leader. The experiment ends
   correctly even if the laptop, the bot or the API goes away. No LLM in the control loop.
2. **The plan is persisted** (`runtime/experiment_plans.json`, one per unit). It feeds the agent's
   context and alert triage (`expected_state`), so a job stopping at the planned end isn't a fault.
   It also drives Slack progress updates, which make no LLM call.
3. **Start jobs over REST, never MQTT.** Pioreactor 26.11 has no subscriber for
   `pioreactor/<unit>/<exp>/run/<job>`: checked in the source installed on `pio01`, and on 17 Oct a
   publish there started nothing while the old tool reported `ok`. `rest` (the leader's API, the same
   calls the UI and MCP server make) is the default, `pios` works on the leader, and any other
   `LARRE_PUMP_TRANSPORT` is refused. `.../set` and `$state/set` over MQTT work (also tested on
   `pio01`). An event log doesn't need the command transport: jobs publish their `$state` on MQTT
   however they were started.
4. **Stop a plan by job id.** Stopping `job_name=experiment_profile` through the leader stops
   *every* profile in the experiment, including other units' plans. `stop_experiment` finds its own
   run by `profile_filename` and stops it by `job_id`.
5. **Each unit's experiment comes from its leader** (`GET /api/workers/<unit>/experiment`), not from a
   global `LARRE_EXPERIMENT`. That setting broke as soon as two units ran different experiments.
6. **Our own digital twin, not Pioreactor's test mode.** Their mocks are for unit tests: OD growth
   is off (`max_gr = 0`), temperature is a sine wave around 25 °C that ignores the heater, and RPM is
   constant. `twin.py` responds to control and can inject faults. It runs biology at 10× by default:
   at 60×, healthy growth trips the rolling-median OD-spike rule.
7. **Mixed cluster:** `LARRE_LEADERS` maps twin units to the twin's URL. The real Pi uses
   `LARRE_LEADER_URL`. Claim it honestly: "one real Pioreactor plus N digital twins under one agent".
   Twins are labelled as simulated.
8. **The listener records history** (`LARRE_RECORD_DB=1`) when LARR-E runs off the Pi. The Pi's
   database stays on the Pi, and twins have none.
9. **Keep the custom agent loop** (`agent.py`) for the hackathon. Tau (a Python agent runtime) was
   considered. It would add sessions and context management, but migrating is a risk with no demo
   payoff. Keep tools independent of the loop so it can be swapped later. Long experiments don't
   need long contexts: the plan and history live outside the model.
10. **No Datalog/Prolog, no event bus, no new scheduler this weekend.** Plain Python conditions
    decide when to involve Claude. These are extensions, not rejections: see `docs/design-notes.md`
    section 3 for Twitch, drawing profiles on the 3DS, Datalog attention rules, an event log, Tau and
    a remote cluster, and where each one plugs in.
11. **Secrets live in `.env`** (gitignored, loaded by `config.py`). Claude Code is denied reading it
    (`.claude/settings.json`). Never print or echo secrets. Use a dedicated Anthropic key with a
    spend limit.
12. **Tooling:** `uv sync`, `uv run larr-e …`, `uv run --group dev pytest`. `requirements.txt`
    stays for plain pip on the Pi; keep it in step with `pyproject.toml`.

## The real Pioreactor

`pio01`: a Raspberry Pi 3 Model A+ running Pioreactor 26.11.0 (pioreactor_20ml), leader and worker
in one, at `http://pioreactor.local` on the phone hotspot. SSH as `pioreactor` with a key; MQTT user
`pioreactor` on port 1883. **Only the stirrer and heater are connected, no pump**: a dose would
"succeed" and move nothing, so never use one as proof that control works. The Pi logs to
`/var/log/pioreactor.log` in local time (UTC+1).

## Working rules

Adapted from the organisers' prompting playbook. A summary is in `docs/design-notes.md`.

- **Hardware is a human's call.** Never start, stop, dose or change settings on the real Pi
  without the person at the reactor saying so in this session. Read-only checks are fine. Don't
  edit the Pi's `~/.pioreactor/config.ini`: LARR-E doesn't need it, and the shared-vs-leader MQTT
  settings are easy to break.
- **During an experiment, only one of the Pioreactor UI and LARR-E acts on a unit.** A job started in
  the UI makes the profile's start fail, and LARR-E's plan won't know about it.
- **Test first.** Write the failing test, see it fail, make it pass. Never edit a test to make it
  pass. The known-right answer comes from the team or the instrument, not the model. Keep
  `tests/test_twin_cluster.py` (broker + twin + listener + tools) passing: it is the end-to-end test.
- **One piece at a time, commit after each working piece**, with a clear message. Stop and show
  the human before the next piece.
- **Verify against the real thing.** Claims about Pioreactor behaviour come from its source
  (github.com/Pioreactor/pioreactor) or docs, not memory. Note the version you checked.
- **Look for problems in a fresh session** before polishing ("this was written with AI and nobody
  checked it; find where it fails in real use; don't be nice"). Rank the findings before fixing.
- **Simple wins.** Explain any term a non-engineer wouldn't know. No new frameworks this weekend.

## Status (as of Saturday afternoon)

Branch `experiment-plans` on `alethkit/larr-e`. 23 tests pass offline; 1 is skipped unless the
Pioreactor source is importable. Run them with `uv run`: calling `.venv/bin/python -m pytest`
directly leaves `amqtt` off PATH, and the end-to-end twin tests then skip silently.

| Area | State |
|---|---|
| Experiment plans → profiles, approval card with profile YAML, Slack progress updates | Built, tested offline and against the twin |
| REST transport, stop by job id, experiment from leader | Built, tested against the twin and a fake leader |
| `/plan` page (`web/plan.html`): drag the plan's numbers, live profile YAML, dashed target line vs measured, Start through the approval gate, no LLM | Built 17 Oct, tested offline and in a browser against the twin. Not yet tried on `pio01` |
| Digital twin (`larr-e twin`), faults, `/twin/state` | Built, tested end to end |
| Listener recorder, `list_units`, `/larr-e units` | Built, tested |
| 3DS: browser dashboard and native PocketJS app with an Ⓐ/Ⓑ approval gate | Built (by the team, before this branch). Shown in the PocketJS simulator; check it on the handheld |
| Real Pi on the phone hotspot | Confirmed 17 Oct: Pioreactor 26.11.0, one unit `pio01` (leader and worker) assigned to "Demo experiment", clock synced, MQTT login, `listen` + `status`, REST job start, MQTT setting change and stop. **Not yet:** a real 5-minute plan starting and stopping on schedule |
| `.env`, uv, LICENSE | Done (LICENSE only on `alethkit` main) |

## Open items, in priority order

1. **Real 5-minute plan on `pio01`**, with a human watching: `larr-e exec "run pio01 at 30 degrees
   and 300 rpm for 5 minutes"`. Profile visible in the UI → jobs start → they stop at 5 minutes. The
   read-only checks are done (see Status). Use stirring and the heater only: `pio01` has no pump.
2. **Decide the submission repo** and merge `experiment-plans`. Check: fresh clone, `uv sync`,
   tests pass, no secrets, LICENSE present.
3. **Triage before notifying.** `listener.py` posts an alert to Slack *before* Claude investigates.
   For the attention-saving claim it should investigate first, then interrupt only if needed.
   Critical safety alarms still go straight through. It should also know about stops: today a
   stirring stop done in the Pioreactor UI raises a critical "stirring stall" alert.
4. **The pitch** (3 min):
   - The problem: the legacy GUI plus babysitting. Set up the same run by clicking, timed, against
     one Slack message.
   - The real reactor responding, plus an automatic update with a graph.
   - A real catch: on 17 Oct LARR-E's stall rule flagged `pio01`'s stirrer dropping to 0 RPM, which
     the Pi's own log confirms happened four times in 15 minutes.
   - The twin cluster as the scale story.
   - Prepare answers for "isn't this just Pioreactor's MCP server?" and "what happens if the laptop
     dies?" (the profile keeps running).
5. **README:** a person should rewrite it short; it is long and model-written. Keep the commands.
6. **Known rough edges:**
   - `api_server.py` has its own approval flow, separate from Slack's.
   - The OD-spike rule doesn't account for growth (fine at real speed, trips at a fast twin
     bio-speed).
   - The Pi's `mqtt_to_db_streaming` may store twin readings, so give the twin its own experiment
     name.
   - `requirements.txt` and `uv.lock` can drift apart.
   - **`pio01`'s stirrer stalls.** On 17 Oct the Pi logged "Stirring RPM is 0" and kicked the fan at
     13:36, 13:40, 13:44 and 13:50 UTC. After a restart at 500 RPM it ran 6 minutes clean (467–534
     RPM). Watch it before the demo; check the stir bar and vial seating if it recurs.
   - **The listener doesn't backfill.** When the laptop dropped off the hotspot it reconnected by
     itself but missed the readings in between, including one stall.
   - **`pioreactor.local` (mDNS) lookups are flaky**, and inside Claude Code's sandbox ssh and MQTT
     can't resolve it at all (curl works through the proxy). Expect a few seconds' delay on connect.
     Tailscale (`docs/tailscale.md`): `pio01` is on the tailnet (shared in and signed for Tailnet
     Lock) at `pio01.tail16b47e.ts.net` / `100.92.181.57`. SSH, REST, MQTT and `larr-e status` were
     checked over it on 17 Oct. Not yet done: `.env` switched over, and the twin test stack on a server.
   - The `./.env.*` read deny also hides `.env.example` from Claude Code.

## Running it

```bash
cp .env.example .env            # fill in yourself; Claude can't read it
uv sync
uv run larr-e listen --no-triage    # terminal 1: telemetry, alerts, recorder
uv run larr-e status                # live table, no LLM
uv run larr-e exec "…"              # asks before touching hardware
uv run larr-e twin --units twin1,twin2 --experiment larre-twin --mqtt-host pioreactor.local --port 8790
#   the twin's thermostat runs at real speed (a reading every 225 s, ~25 min to 37 °C); add
#   --temp-speed 10 for a stage demo. Gaps from a real Pioreactor: docs/twin-fidelity.md
uv run --group dev pytest           # ~55 s; the twin test needs mosquitto or amqtt (dev group)
```
