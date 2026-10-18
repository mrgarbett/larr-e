# Running LARR-E over Tailscale

**Why:** the phone hotspot and `pioreactor.local` lookups are fragile, and we're nervous about
testing anything that might break them. Tailscale gives every machine a fixed private address
(`100.x.y.z`) and a short name (`pio01`, `charon`) that works on any network. The Pi, the laptop and
a server can then reach each other whether they're on the hotspot, the venue Wi-Fi, or home
broadband, and LARR-E never has to look up `pioreactor.local` again.

**No code changes.** LARR-E reads every address from `.env` (`LARRE_MQTT_HOST`,
`LARRE_LEADER_URL`, `LARRE_LEADERS`), so this is configuration only.

Terms used below:

- **tailnet:** our private Tailscale network (account `alethkit`). The laptop (`lilith`) and
  several always-on Linux servers (`charon`, `charon-1`, `newton`, `homebox`) are already on it.
- **MagicDNS:** Tailscale's name lookup. `pio01` resolves to its `100.x` address on any tailnet
  machine. If a name doesn't resolve, use the `100.x` address from `tailscale status`.

There are three parts, in order of risk. Do them one at a time, and check each before starting the next.

| Part | Touches the Pi? | What it gets us |
|---|---|---|
| A. Twin test stack on a server | No | Break things freely: a broker and twins that never touch the Pi or the hotspot |
| B. Tailscale on `pio01` | Yes: installs a package and a network service | Reach the real Pi by a fixed name from anywhere |
| C. Point `.env` at the tailnet | No (laptop only) | LARR-E stops depending on mDNS |

---

## A. Twin test stack on a server (safe: start here)

This is a complete, separate copy of the setup: its own MQTT broker, its own twins, and LARR-E on
the laptop pointed at them. Nothing in it can reach the Pi, the hotspot or the demo Slack channel.
It also fixes a known rough edge: twin readings can no longer end up in the Pi's database.

Push the branch from the laptop first (`git push origin experiment-plans`), so the server gets the
same code. Then, on the server (examples use `charon`; any always-on tailnet Linux box will do):

```bash
ssh charon
sudo apt install -y mosquitto        # skip if already installed
sudo systemctl disable --now mosquitto   # we run our own broker below, not the system one
git clone https://github.com/alethkit/larr-e.git && cd larr-e
git checkout experiment-plans
uv sync
TS_IP=$(tailscale ip -4)
printf 'listener 1883 127.0.0.1\nlistener 1883 %s\nallow_anonymous true\n' "$TS_IP" > ~/larre-mosquitto.conf
```

The broker listens only on the server itself and on its tailnet address, never on its public
interface, so allowing anonymous logins is safe here. Run both in `tmux` so they survive a dropped
SSH session:

```bash
tmux new -s larre
mosquitto -c ~/larre-mosquitto.conf                    # window 1
uv run larr-e twin --units twin1,twin2,twin3 --experiment larre-twin \
  --mqtt-host 127.0.0.1 --host "$(tailscale ip -4)" --port 8790   # window 2 (Ctrl-b c)
```

Check from the laptop:

```bash
curl http://charon:8790/twin/state
```

On the laptop, put the overrides in a file next to `.env`. The name `.env.twintest` is gitignored
by the `.env.*` rule. It holds no secrets: the API keys still come from `.env`.

```bash
# .env.twintest: LARR-E against the twin stack on charon. Nothing here reaches the Pi.
LARRE_MQTT_HOST=charon
LARRE_MQTT_PORT=1883
LARRE_MQTT_USERNAME=
LARRE_MQTT_PASSWORD=
LARRE_LEADER_URL=http://charon:8790
LARRE_LEADERS=twin1=http://charon:8790,twin2=http://charon:8790,twin3=http://charon:8790
LARRE_RUNTIME_DIR=runtime-twintest
LARRE_RECORD_DB=1
LARRE_DB_PATH=runtime-twintest/larre.sqlite
LARRE_SLACK_WEBHOOK_URL=
```

Variables set in the shell take priority over `.env` (see `config.py`), and empty ones count as
unset. So load the overrides first, then run as usual:

```bash
set -a; . ./.env.twintest; set +a
mkdir -p runtime-twintest
uv run larr-e listen --no-triage
```

Use one terminal per command, and load `.env.twintest` in each one. `LARRE_RUNTIME_DIR` keeps
test plans out of `runtime/experiment_plans.json`, so a test plan can't be mistaken for the real
one. Inject faults with `curl -X POST http://charon:8790/twin/fault -d '{"unit":"twin1","fault":"stir_stall"}'`.

**Slack:** `.env.twintest` silences the listener's webhook. `larr-e slack` would still use the real
bot token from `.env`. To test the bot against twins, point `LARRE_SLACK_ALERT_CHANNEL` at a test
channel in `.env.twintest`, or don't run it.

---

## B. Tailscale on `pio01` (a human runs this, with nothing running on the reactor)

What it changes: it installs the `tailscale` package and starts a service that adds one network
interface (`tailscale0`) for `100.x` addresses. It does **not** change the Pi's Wi-Fi, its default
route, its DNS (we pass `--accept-dns=false`), or `~/.pioreactor/config.ini`. The Pi needs internet
access (the hotspot has it). It uses roughly 30–50 MB of the Pi 3 A+'s 512 MB of RAM.

### B1. Read-only checks first

```bash
ssh pioreactor@pioreactor.local
free -m                                  # want 100+ MB "available"
sudo ss -tlnp | grep -E ':(80|1883) '    # broker and web server should listen on 0.0.0.0 / [::]
pio status 2>/dev/null || true           # note what's running, so you know it's unchanged afterwards
```

If port 1883 or 80 listens only on a specific address (not `0.0.0.0`, `*` or `[::]`), stop here: it
won't answer on the tailnet address, and fixing that means editing the broker or web server config.
Talk it over before changing anything.

### B2. Install and join

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --hostname=pio01 --accept-dns=false
```

`tailscale up` prints a login URL. Open it on the laptop, sign in to the `alethkit` tailnet and
approve the device. Optional: in the Tailscale admin console, open `pio01` and choose "Disable key
expiry", so it doesn't drop off the tailnet mid-experiment months from now.

### B3. Check from the laptop (read-only)

```bash
tailscale status | grep pio01
curl -s http://pio01.tail16b47e.ts.net/api/workers/pio01/experiment     # same call LARR-E makes
```

Then do part C and run `uv run larr-e status`: telemetry should arrive as it did over the hotspot.

### What actually happened (17 Oct, 19:00)

- `pio01` joined a teammate's tailnet (`tail16b47e`), not alethkit's (`golden-gopher`), and was
  **shared** into alethkit's. A shared device keeps its home tailnet's name and address:
  `pio01.tail16b47e.ts.net`, `100.92.181.57`. The short name `pio01` does **not** resolve on the laptop.
- alethkit's tailnet has **Tailnet Lock** on, so the shared Pi was locked out until a trusted key
  signed it (`tailscale lock status` lists locked-out nodes; `tailscale lock sign nodekey:…` signs one,
  and the laptop `lilith` holds a trusted key). If the Pi logs out and back in, its node key changes
  and it needs signing again.
- Checked from the laptop over Tailscale, all read-only: ping is direct over the hotspot (about 20 ms,
  no relay), REST returns 200, `larr-e status` connects to MQTT 1883 and shows `pio01`, and SSH works.
- SSH by IP needs the Pi's key and `IdentitiesOnly`, or the agent offers too many keys and the Pi
  hangs up. Add to `~/.ssh/config`:
  ```
  Host pio01.tail16b47e.ts.net 100.92.181.57
    User pioreactor
    IdentityFile ~/.ssh/pioreactor
    IdentitiesOnly yes
  ```
- The Pi still has Tailscale's DNS takeover on (`CorpDNS: true`). It's harmless so far. Turn it off
  with `sudo tailscale set --accept-dns=false`.
- Claude Code's sandbox can't read `.env`, so any `larr-e` command that needs the broker login has
  to run outside it, whatever the network.

### Rollback

```bash
sudo tailscale down                      # leave the tailnet, keep the package
sudo apt remove -y tailscale             # remove it completely
```

The hotspot route keeps working throughout. `pioreactor.local` doesn't depend on Tailscale.

### Optional, later: a second Wi-Fi network on the Pi

Tailscale keeps the Pi's address fixed, but it can't help if the Pi has no network at all. Adding
the venue Wi-Fi as a fallback would let the Pi survive the hotspot dropping, and LARR-E wouldn't
notice the switch. This **does** change the Pi's network configuration, and a typo can leave the Pi
unreachable, so do it only with someone at the reactor who can plug in a screen. On Raspberry Pi OS
Bookworm: `sudo nmcli dev wifi connect "<venue SSID>" password "<…>"`, then check that the hotspot
connection still has the higher priority (`nmcli -f NAME,AUTOCONNECT-PRIORITY con`).

---

## C. Point `.env` at the tailnet (laptop)

After B3 passes, change two lines in `.env` (and keep the old values in a comment for a quick
switch back):

```bash
LARRE_MQTT_HOST=pio01.tail16b47e.ts.net          # was pioreactor.local (or use 100.92.181.57)
LARRE_LEADER_URL=http://pio01.tail16b47e.ts.net  # was http://pioreactor.local
```

There was no `.env` on the laptop at the time, and `larr-e status` still connected: the Pi's broker
accepted a connection with no username. If you do set a broker login, keep it the same: it's the same
broker, reached by a different route. Only tailnet devices and the hotspot can reach it.

To run the real Pi and the twins together (decision 7 in `CLAUDE.md`), the twins still have to
publish to the Pi's broker, because the listener follows one broker. Run the twin on the server
with `--mqtt-host pio01.tail16b47e.ts.net`, put the broker login in that server's environment, and set
`LARRE_LEADERS=twin1=http://charon:8790,...` on the laptop. Use part A's separate stack for
anything experimental. Use this mixed setup only for the demo itself.

---

## What Tailscale doesn't solve

- **The 3DS can't join a tailnet.** It still reaches `larr-e api` at the laptop's hotspot address.
  The laptop then reaches the Pi over Tailscale, so the 3DS no longer depends on mDNS, but it still
  needs the laptop and the 3DS on the same Wi-Fi.
- **Claude Code's sandbox** may block raw MQTT connections to `100.x` addresses, as it does for
  `pioreactor.local` (untested). Run `listen` and `status` in your own terminal.
- **The listener still doesn't backfill.** A steadier link means fewer gaps, but readings missed
  during an outage are still lost.
- **Moving the Slack bot onto a server** would let it outlive the laptop. That's possible with this
  setup, but it means copying the `.env` secrets to that server. Not done here.
