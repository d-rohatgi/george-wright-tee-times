# George Wright Tee Times

Automatically books a Saturday tee time at **George Wright Golf Course**
(Boston) the moment the tee sheet opens.

George Wright releases each date **4 days ahead at 07:00** — so Tuesday 07:00
opens the following Saturday. This agent is scheduled on your Mac for 06:58,
polls until inventory drops, grabs the earliest slot inside your time window,
and sends you a notification with the result. It reads the release rule from
the course's server rather than assuming it.

- **macOS only** — it uses Keychain, launchd, and macOS notifications.
- **Python 3.11+, standard library only.** No `pip install`.
- **Your own account.** It books with your City of Boston golf (CPS Golf)
  login and a card you've already saved on that account.
- **A Mac that's on at 7am**, Tuesday–Friday — a desktop, or a laptop left
  plugged in with the lid open.

### Setup at a glance

1. Python 3.11+ → 2. clone and run the tests → 3. golf account with a saved
card → 4. copy the example config → 5. password into Keychain → 6. `whoami` to
fill in your card → 7. set players / holes / time window → 8. dry run →
9. install the schedule and keep the Mac awake. About 15 minutes.

---

## Setup

### 1. Check Python

```bash
python3 --version        # needs 3.11 or newer
```

The Python that ships with macOS is 3.9 and **will not work** (it has no
`tomllib`). If yours is older, install a current one:

```bash
brew install python@3.12
```

Every command below uses `python3` — make sure it points at 3.11+.

### 2. Get the code and run the tests

```bash
git clone https://github.com/d-rohatgi/george-wright-tee-times.git
cd george-wright-tee-times
python3 -m unittest discover -s tests      # offline, ~1s
```

The tests use a fake George Wright, so they need no account or network.

### 3. Have a golf account with a saved card

You need an online account on Boston's golf booking site (the one George
Wright's "Book a tee time" link goes to) with **at least one credit card saved**.
The easiest way to save a card is to make one booking by hand on the website.

The card is held against no-shows; the green fee is paid at the course.

### 4. Create your config

```bash
cp config/preferences.example.toml config/preferences.toml
```

Open `config/preferences.toml` and set `email` to your golf account email.
Leave the other account fields for now. `preferences.toml` is gitignored — your
details never enter the repo.

### 5. Store your password in Keychain

```bash
security add-generic-password -a "you@example.com" -s georgewright-cps -w
```

Use your golf account email for `-a`. The `-w` flag prompts for the password,
which keeps it out of your shell history. The password is only ever read from
Keychain — it is never written to a file.

### 6. Fill in your card with `whoami`

```bash
python3 -m booking_agent.cli whoami
```

This signs in and lists what the config needs (card tokens are never shown):

```
member class NRES  (config: NRES)
saved cards  id        last4  type        expires  acct
             111111    1234   Visa        0128     22222222  (default)
card         FAILED — card id 0 is not on file ...
```

Copy into `[george_wright.account]`:

| config field   | from `whoami`                  |
|----------------|--------------------------------|
| `card_id`      | the `id` of the card to use    |
| `card_last4`   | its `last4`                    |
| `acct`         | its `acct`                     |
| `member_class` | `member class`                 |

Run `whoami` again — it should end with `token resolved` and a count of your
upcoming reservations.

> **Rate code:** `NRES` (non-resident) pairs with `rate_code = "NON-RES"`, and
> that is the combination this has been verified with. If you're a Boston
> resident your member class will differ; check the rate the website shows you
> at checkout and set `rate_code` to match.

### 7. Set your preferences

Still in `config/preferences.toml`:

| setting | default | meaning |
|---|---|---|
| `players` | `4` | seats to book; a slot needs this many open |
| `holes` | `18` | `9` or `18` |
| `no_earlier_than` / `no_later_than` | `10:00` / `14:00` | only book inside this window; earliest wins |
| `max_attempts` | `6` | how many slots to try if the first gets taken |
| `deadline_seconds` | `300` | how long to keep polling (must be ≥ 240 for a 06:58 start) |

### 8. Try it without booking

```bash
python3 -m booking_agent.cli rules                   # the server's booking window
python3 -m booking_agent.cli --backend live peek     # upcoming Saturday's tee sheet (--date for another day)
python3 -m booking_agent.cli --backend live --login run --dry-run --date YYYY-MM-DD
```

`--dry-run` ranks the sheet and tells you what it *would* book — nothing is
reserved. Pick a `--date` that's already open (today through 4 days out);
on an unreleased date it just polls until `deadline_seconds` runs out.

### 9. Schedule it

The run needs to fire at 06:58 **with the Mac awake**. A job that runs late
misses the 07:00 rush just as surely as one that never runs.

**Install the launchd job** (from the repo folder):

```bash
mkdir -p data
PY="$(command -v python3)"; "$PY" -c "import tomllib" && echo "using $PY"
sed -e "s|__PYTHON__|$PY|g" -e "s|__REPO_DIR__|$PWD|g" \
    scripts/booking-agent.plist > ~/Library/LaunchAgents/local.booking-agent.plist
launchctl load ~/Library/LaunchAgents/local.booking-agent.plist
```

**Keep the Mac awake and plugged in.** In System Settings, stop the computer
sleeping automatically when on power, or run:

```bash
sudo pmset -c sleep 0                        # never idle-sleep on AC power
sudo pmset repeat wake MTWRFSU 06:55:00      # backup: scheduled wake each morning
```

On a laptop, leave the lid **open** overnight — closing it forces sleep.

**Check it's ready** — the evening before, or any time:

```bash
python3 -m booking_agent.cli --backend live preflight
```

Every line should be ✓, ending with `READY — it will fire at 06:58`.

---

## Using it

It runs by itself **Tuesday–Friday at 06:58**, always for the upcoming
Saturday. Tuesday is the release morning. Wednesday–Friday catch a release
that opened late (holiday weeks shift it) — once you're booked, those runs
see the reservation and do nothing.

After each run you get a macOS notification, and the result is logged:

```bash
python3 -m booking_agent.cli history                  # every attempt and outcome
python3 -m booking_agent.cli --backend live polls     # second-by-second timeline of the last run
tail -30 data/launchd.log                             # full output of scheduled runs
python3 -m booking_agent.cli whoami                   # your upcoming reservations
python3 -m booking_agent.cli cancel <reservation_id>  # id from history or whoami
```

Every run ends in exactly one of: `BOOKED`, `ALREADY_BOOKED`, `UNAVAILABLE`,
`WAITLISTED`, or `ERROR` — it never fails silently.

### Why did it miss? — the poll log

Every run writes a trace to `data/polls/` (one JSON line per poll), and
`polls` turns it into a timeline — for example:

```
Target Sat Oct 10 2026 · 4 players · 18 holes · 10:00–14:00 · deadline 300s
  06:58:01.2 → 06:59:59.8  not released  ×131  (370 ms/poll)
                           server said: "... You may book this tee time starting at [7:00 AM]."
  07:00:00.6               OPEN — 38 tee times · 30 with enough holes · 14 with enough seats · 12 in window · 5 match
                           sheet: 07:00/18h/4 07:10/18h/2 ...
  07:00:00.9               attempt 10:10 — lost (...)  140 ms
  07:00:01.1               attempt 10:30 — won  120 ms
```

It shows whether the date opened on time, exactly what the sheet held, which
of your rules excluded each slot, and how every lock attempt went. Add
`--target YYYY-MM-DD` for a specific Saturday's latest run.

### Know the course's rules

The server enforces these, and `rules` shows the live values:

- **One booking per day** per account.
- **Two no-shows and your account is restricted.** The agent books every
  Saturday whether or not you end up playing — **cancel any week you can't
  make** so a skipped round doesn't cost you a strike.

---

## Troubleshooting

| symptom | likely cause |
|---|---|
| `No module named 'tomllib'` | Python older than 3.11 — see step 1; reinstall the plist with the right `$PY` |
| `UNAVAILABLE` — "the date never opened" | the release came late or moved (holiday weeks). The next morning's run retries |
| `UNAVAILABLE` — "the sheet opened … but none had N open seats" | the sheet was open but nothing matched your rules — `polls` shows what was there and why each slot failed |
| `UNAVAILABLE` — "completely sold out" | no tee times at all that day — a tournament or closure, or simply booked up (common for Saturdays by midweek) |
| `ERROR` about the card | `card_id` / `card_last4` don't match a saved card — rerun `whoami` |
| `ERROR` about auth / Keychain | password missing or changed — redo step 5 |
| nothing happened at all | the Mac was asleep or the job isn't loaded — run `preflight` |
| every slot filtered out | `course` in the config must be exactly `George Wright Golf Course` |

### Uninstall

```bash
launchctl unload ~/Library/LaunchAgents/local.booking-agent.plist
rm ~/Library/LaunchAgents/local.booking-agent.plist
security delete-generic-password -s georgewright-cps
```

---

## Where things live

| what | where |
|---|---|
| password | macOS Keychain, service `georgewright-cps` |
| account + card lookup keys | `config/preferences.toml` (gitignored) |
| card token | fetched per booking, held in memory only, never written |
| attempt ledger, logs, poll traces | `data/` (gitignored) |

Nothing sensitive is committed to this repo.

---

## How it works

```
booking_agent/
  cli.py               commands (run, peek, rules, whoami, preflight, cancel, history, polls, simulate)
  tasks/golf.py        the run: poll → rank → lock → confirm → report
  adapters/cps_golf.py the live CPS Golf API
  adapters/fake_golf.py a synthetic George Wright for tests and `simulate`
  auth.py              Keychain password → signed-in session
  store.py             SQLite ledger — the idempotency guard
  trace.py             per-poll trace + the `polls` timeline
  config.py            preferences.toml → settings
  notify.py            outcome → stdout + macOS notification
  context.py           weather forecast (api.weather.gov), advisory only
  brief.py             optional local-model summary (off by default)
config/preferences.example.toml
docs/RECON.md          how the API was mapped
docs/POSTMORTEM-2026-08-11.md
scripts/booking-agent.plist   launchd template
```

The `--backend` flag defaults to `fake`; pass `--backend live` for the real
site, plus `--login` for anything that books.

**Booking is hold-then-confirm.** `LockTeeTimes` takes the slot out of
circulation for ~10 minutes; the remaining calls (price, restriction check,
reserve) run inside that hold. So the 07:00 race is only `search → lock`, and
a failed run releases its hold instead of sitting on the slot.

**"Not released yet" is an HTTP 400, not an empty list.** Treated as an error,
it would abort one second before inventory drops, every week. The poll loop
recognises it and keeps polling.

**`LockTeeTimes` reports a lost slot as `{"error": ...}` in a 200.** Checking
only the status code would "succeed" on a slot someone else took.

**If your first choice is sniped,** it walks down the ranked list, up to
`max_attempts`.

**Two independent double-booking checks:** the local ledger (works even if the
session is dead) and your live reservations on the server.

**Weather is advisory, never a gate** — it books first and includes the
forecast in the notification.

### Rehearse the race

```bash
python3 -m booking_agent.cli simulate     # fake 07:00 release with competing bookers
```

### Optional: model summary

`run --brief` asks a local model (e.g. [Ollama](https://ollama.com)) for a
one-paragraph summary of the outcome. Off by default and never on the booking
path. Configure with `BOOKING_AGENT_MODEL_URL` (default
`http://localhost:11434/v1`) and `BOOKING_AGENT_MODEL` (default `qwen3:8b`);
check with `python3 -m booking_agent.cli check-model`.

---

## Caveats

- **Unofficial.** This talks to the same API the course's booking website uses.
  It isn't affiliated with the course or the city, and it can break whenever
  the site changes. Use it with your own account and within the course's
  booking rules.
- The site sits behind Cloudflare bot detection. This tool makes ordinary API
  calls and makes no attempt to get around it; if requests start being
  challenged, it will stop working rather than escalate.
- Verified end to end with the non-resident rate and both 2- and 4-player
  bookings.

---

## License

[MIT](LICENSE) — use it, change it, share it. No warranty: it books real tee
times on a real account, so read what it does before you schedule it.
