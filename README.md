# booking-agent

Books George Wright tee times. Release is Tuesday 07:00 for the following
Saturday — confirmed from the server, not assumed.

Zero dependencies, stdlib only. **No model in the loop** — that's opt-in later.

```bash
python3 -m unittest discover -s tests -v          # 16 tests, ~20ms
python3 -m booking_agent.cli rules                # server's booking window
python3 -m booking_agent.cli --backend live peek  # today's bookable sheet
python3 -m booking_agent.cli --backend live run --dry-run
python3 -m booking_agent.cli simulate             # rehearse the 7am race
python3 -m booking_agent.cli history
```

Default backend is `fake`. `--backend live` hits the real CPS API.

## Status

| | |
|---|---|
| Read path (search, booking rules) | **working, live, no browser needed** |
| Ranking, idempotency, polling, reporting | **done, 16 tests** |
| Auth, card resolution, cancel, list bookings | **working, verified live** |
| `book_tee_time` | **working** — booked + cancelled a test reservation on 2026-08-10 |
| Phase B model summary | written, dormant behind `--brief` |
| Scheduling (launchd) | plist written, not installed |

> Verified with 2 players / 18 holes, the only shape available in the booking
> window at the time. The 4-player path uses the same seat logic driven by
> `availableParticipantNo`, but has not itself been executed.

Before anything authenticated works, seed the Keychain (the `-w` prompt keeps
it out of shell history):

```bash
security add-generic-password -a you@example.com -s georgewright-cps -w
```

Then verify without booking anything:

```bash
python3 -m booking_agent.cli whoami
```

## Booking is a hold-then-confirm flow

```
POST /LockTeeTimes            ← the 07:00:00 race is won HERE, ~10 min hold
POST /CheckRestrictReservation
POST /TeeTimePricesCalculation   bookingList: one entry per player, holes
POST /RegisterTransactionId   x2
POST /ReserveTeeTimes         → reservationId, bookingIds
```

The hot path is only `search → lock`. Everything after runs inside the hold, at
a relaxed pace. `UnLockTeeTimes` fires on any abort so a failed run doesn't sit
on a slot for ten minutes.

**`LockTeeTimes` reports failure as `{"error": "..."}` in a 200 response**, not
an HTTP error status. Checking the status code alone would silently "succeed"
on a lost slot and fail confusingly three calls later.

## Credentials

Nothing sensitive is in this repo.

| what | where |
|---|---|
| password | macOS Keychain, service `georgewright-cps` |
| card token | fetched per booking, in memory only, never written |
| card id + last 4 | `preferences.toml` — lookup keys, not credentials |

An account can have **two saved cards sharing a last-4**, and the account
default is a *different* card. So `card_id` and `card_last4` must both match or
`resolve_card_token()` refuses to book rather than silently charging the wrong
card.

## What the server told us

`cli.py rules` reads this live rather than hardcoding it:

```
days_in_advance      4        →  Tuesday 07:00 releases Saturday
release_time         07:00:00
max_daily_bookings   1        →  one booking per day, server-enforced
no_show_limit        2        →  two no-shows and the account is restricted
```

`max_daily_bookings: 1` means the idempotency guard maps to a real constraint.
`no_show_limit: 2` is the argument for the Friday weather re-check — an
auto-booked tee time you skip in the rain actually costs you something.

## Two findings that changed the design

**Not-yet-released is a 400, not an empty list.** The API answers a date past
the window with `400 "Sorry, you are not able to book this tee time
currently."` The obvious implementation treats that as an error and aborts —
one second before inventory drops, every single Tuesday. `NotYetReleased` is a
distinct exception that the poll loop swallows and retries. Tested both ways.

**The course name has to match exactly.** `preferences.toml` originally said
`"George Wright"`; the API reports `"George Wright Golf Course"`. The filter
rejected the entire sheet and the run looked like a sold-out Saturday.
`TestConfigMatchesReality` guards it now — cheap test, expensive failure.

## Layout

```
booking_agent/
  models.py            Slot / Booking / Outcome / GolfPrefs, Status enum
  config.py            preferences.toml → GolfPrefs
  store.py             SQLite attempt ledger — the idempotency guard
  notify.py            outcome → stdout + macOS notification
  context.py           check_weather() via api.weather.gov (live, keyless)
  brief.py             Phase B model summary — dormant, opt-in
  adapters/
    base.py            Protocol + the exception taxonomy
    cps_golf.py        live CPS Golf adapter (read path done)
    fake_golf.py       synthetic George Wright, mirrors real failure modes
  tasks/golf.py        the deterministic run
config/preferences.toml
docs/RECON.md          how the API was mapped
```

## Deliberate choices

**Two independent idempotency checks.** Ledger consulted *before* the site,
because it still works when the session is dead. Prevents: job books, crashes
before recording, cron retries, you're double-booked into a no-show strike.

**Partial adapters degrade, they don't crash.** The live adapter can't read
your existing bookings yet, so the run falls back to ledger-only and *says so*
in the notification rather than silently dropping a safety check.

**Fall-through on snipe.** Your top choice is routinely gone between `search`
and `book`. That's `SlotUnavailable`, expected, walk down to `max_attempts`.

**Every run reports a terminal state.** `BOOKED` / `WAITLISTED` /
`ALREADY_BOOKED` / `UNAVAILABLE` / `ERROR`. Silence is never an outcome.

**Bounded "earliest".** `no_earlier_than = 06:30`, `no_later_than = 11:00`.
Widen in June; the evening twilight 18-hole slots are correctly excluded now.

**Weather is advisory, never a gate.** Book first, report the forecast,
re-check Friday.

## Next

1. **HAR capture** — log in, book one tee time manually with DevTools open,
   save the Network tab as HAR. That unblocks `book_tee_time`,
   `cancel_booking`, and `get_existing_bookings` in `adapters/cps_golf.py`.
   Credentials go in Keychain, never in this repo:
   `security add-generic-password -a <email> -s georgewright-cps -w`
2. **cron at 06:57** — pin to `America/New_York` or DST bites twice a year.
   Warm the token before 07:00 rather than cold-starting into the race.
3. **Then the agent layer** — `--brief`, Ollama, tool-calling loop.
