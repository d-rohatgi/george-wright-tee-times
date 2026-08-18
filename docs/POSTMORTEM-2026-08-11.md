# Postmortem — missed the Aug 15 booking (2026-08-11)

The armed job ran but booked nothing. The Saturday tee time was not reserved.

## Impact

No booking made; no double-booking; no charge; account clean. Pure miss.

## Timeline

- **2026-08-10 19:03** — while demoing the happy path, `run --backend fake`
  wrote booking `GW-0001` (target 2026-08-15) into the **live** ledger.
- **2026-08-11 06:55** — scheduled `pmset` wake did **not** fire. Machine
  stayed asleep. (Likely on battery / lid-closed overnight.)
- **2026-08-11 07:00** — tee times released. Nothing was polling.
- **2026-08-11 ~09:06** — machine woke (manually); launchd ran the missed job.
  The idempotency guard found `GW-0001` for Aug 15 in the ledger and reported
  `ALREADY_BOOKED — no action taken`. It never attempted a real booking.

## Root causes (two, independent — either alone loses the slot)

1. **Test data poisoned the production ledger.** The fake backend shared the
   live ledger file. A demo booking looked exactly like a real one to the
   idempotency guard. This is what made the run a no-op.

2. **The laptop did not wake.** `StartCalendarInterval` runs a missed job on
   the next wake, which for a 07:00 release is far too late. Scheduled wake on
   a MacBook is unreliable when on battery or lid-closed.

## Fixes applied

- **Ledger split by backend** (`cli._ledger_path`): live → `data/ledger.db`,
  fake → `data/ledger-fake.db`. Test runs physically cannot touch the live
  ledger. Guarded by `TestLedgerIsolation`.
- **Purged** the poisoned live ledger.
- **Verified the 4-player booking path** end-to-end for the first time
  (`numberOfPlayer=4`, booked and cancelled clean). The
  earlier live test had only exercised the 1–2 player path.

## Still open — runtime reliability

The wake failure is operational, not a code bug. A laptop is a poor host for a
to-the-second timed race. Options tracked in the runtime decision:
- Laptop + `pmset`, kept on AC with lid open — best effort, failed once.
- Small always-on VPS — the design is stdlib-only, so the port is ~20 min
  (swap Keychain lookup for an env var / secret; launchd plist → cron).

Next real firing: **Tue 2026-08-18 07:00**, targeting **Sat 2026-08-22**.
