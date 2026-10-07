"""Phase A — the deterministic hot path.

No LLM runs here. At 07:00:00 on a Tuesday this is a tight, boring loop whose
only job is to claim the earliest qualifying tee time before anyone else does.
The model gets involved afterwards, in brief.py, where being slow is free.
"""

from __future__ import annotations

import sys
import time as _time
import traceback
from dataclasses import replace
from datetime import date, datetime, timedelta

from booking_agent.adapters.base import (
    AdapterError,
    AuthExpired,
    GolfAdapter,
    NotYetReleased,
    RateLimited,
    SlotUnavailable,
    TransientError,
)
from booking_agent.models import GolfPrefs, Outcome, Slot, Status
from booking_agent.store import Ledger
from booking_agent.trace import NullTrace

SERVICE = "george_wright"


def next_saturday(from_date: date) -> date:
    """The Saturday this run is targeting. Run on a Tuesday, this is the
    Saturday of the same week (+4 days). Run *on* a Saturday, it rolls to the
    next one rather than targeting today."""
    delta = (5 - from_date.weekday()) % 7
    return from_date + timedelta(days=delta or 7)


def rank(slots: list[Slot], prefs: GolfPrefs) -> list[Slot]:
    """Qualifying slots, earliest first. 'Prefer the earliest available time'
    is the whole ranking function — but only among slots that clear the hard
    filters, so we never take a 9-hole or a 2-spot slot just because it's early."""
    return sorted((s for s in slots if prefs.matches(s)), key=lambda s: s.tee_time)


def _ms(t0: float) -> int:
    return round((_time.monotonic() - t0) * 1000)


def _sheet_counts(slots: list[Slot], prefs: GolfPrefs) -> dict:
    """How many slots cleared each filter. This is what a bare 'nothing
    matched' hides: sold out, wrong holes, too few seats, or outside the window."""
    window = [prefs.no_earlier_than <= s.tee_time.time() <= prefs.no_later_than
              for s in slots]
    return {
        "slots": len(slots),
        "holes_ok": sum(s.holes >= prefs.holes for s in slots),
        "seats_ok": sum(s.spots >= prefs.players for s in slots),
        "window_ok": sum(window),
        "match": sum(prefs.matches(s) for s in slots),
    }


def _compact(slots: list[Slot]) -> str:
    return " ".join(f"{s.tee_time:%H:%M}/{s.holes}h/{s.spots}"
                    for s in sorted(slots, key=lambda s: s.tee_time))


def run(
    adapter: GolfAdapter,
    prefs: GolfPrefs,
    target_date: date,
    ledger: Ledger,
    *,
    deadline_s: float = 90.0,
    poll_interval_s: float = 0.5,
    now=datetime.now,
    sleep=_time.sleep,
    dry_run: bool = False,
    trace=None,
) -> Outcome:
    """One booking run. `trace` (a PollTrace) records every poll and attempt,
    so a miss can be explained after the fact; default records nothing."""
    trace = trace or NullTrace()
    trace.event(
        "start", target=f"{target_date:%a %b %d %Y}", players=prefs.players,
        holes=prefs.holes, courses=list(prefs.courses),
        window=f"{prefs.no_earlier_than:%H:%M}–{prefs.no_later_than:%H:%M}",
        deadline_s=deadline_s, poll_interval_s=poll_interval_s, dry_run=dry_run,
    )
    stats = {"polls": 0}
    try:
        out = _run(adapter, prefs, target_date, ledger, deadline_s=deadline_s,
                   poll_interval_s=poll_interval_s, now=now, sleep=sleep,
                   dry_run=dry_run, trace=trace, stats=stats)
    except Exception as exc:  # noqa: BLE001
        # Last line of defence: a scheduled run must always end in a reported
        # outcome. On 2026-09-18 an uncaught read timeout ended one with no
        # notification and no ledger row — a silent miss.
        traceback.print_exc(file=sys.stderr)
        out = Outcome(status=Status.ERROR, target_date=target_date,
                      error=f"unexpected {type(exc).__name__}: {exc}")
        try:
            ledger.record(SERVICE, target_date, out)
        except Exception:  # noqa: BLE001
            pass
    trace.event("end", status=out.status.value, polls=stats["polls"],
                considered=out.considered, attempts=len(out.attempted),
                error=out.error)
    return out


def _account_booking(adapter, target_date: date, prefs: GolfPrefs):
    """The account's existing booking for target_date at this course, if any."""
    for booking in adapter.get_existing_bookings():
        if (booking.slot.tee_time.date() == target_date
                and booking.slot.course in prefs.courses):
            return booking
    return None


def _run(adapter, prefs, target_date, ledger, *, deadline_s, poll_interval_s,
         now, sleep, dry_run, trace, stats) -> Outcome:
    out = Outcome(status=Status.ERROR, target_date=target_date)

    # --- idempotency: two independent checks, cheapest first ---------------
    prior = ledger.prior_success(SERVICE, target_date)
    if prior:
        out.status = Status.ALREADY_BOOKED
        out.notes.append(f"ledger already holds booking {prior} for this date")
        return out

    for tries_left in (2, 1, 0):
        try:
            existing = _account_booking(adapter, target_date, prefs)
        except TransientError as exc:
            # A network blip at 06:58 must not abort the run — there are two
            # minutes to spare. Retry, then fall back to the ledger.
            if tries_left:
                sleep(1.0)
                continue
            out.notes.append(
                f"account check failed ({exc}) — relying on the local ledger alone"
            )
            break
        except AuthExpired as exc:
            out.error = f"{exc} (check the Keychain password: `python -m booking_agent.cli whoami`)"
            return out
        except NotImplementedError:
            # Live adapter can't read the account yet. Degrade to ledger-only
            # rather than refusing to run — but say so, because a booking made
            # outside this agent is now invisible to it.
            out.notes.append(
                "account check unavailable (write path not implemented) — "
                "relying on the local ledger alone for idempotency"
            )
            break
        if existing:
            out.status = Status.ALREADY_BOOKED
            out.booking = existing
            out.notes.append("account already holds a booking for this date")
            return out
        break

    # --- poll for release, then claim -------------------------------------
    started = now()
    backoff = poll_interval_s
    waited_on_release = False
    not_released = sheets = peak = transient = 0
    last_sheet = None

    while (now() - started).total_seconds() < deadline_s:
        stats["polls"] += 1
        t0 = _time.monotonic()
        try:
            slots = adapter.search_tee_times(target_date)
        except NotYetReleased as exc:
            # The normal state at 06:59:59. Keep polling — this is precisely
            # the window we are here to win.
            not_released += 1
            extra = {} if waited_on_release else {"msg": str(exc)[:200]}
            trace.event("poll", result="not_released", ms=_ms(t0), **extra)
            if not waited_on_release:
                waited_on_release = True
                out.notes.append("waited for the release window to open")
            sleep(poll_interval_s)
            continue
        except AuthExpired as exc:
            trace.event("poll", result="auth_expired", ms=_ms(t0), error=str(exc)[:200])
            out.error = str(exc)
            return out
        except RateLimited:
            trace.event("poll", result="rate_limited", ms=_ms(t0))
            backoff = min(backoff * 2, 5.0)
            sleep(backoff)
            continue
        except TransientError as exc:
            # Timeout, reset or 5xx — most likely right at 07:00, when the
            # server is swamped. Never fatal: poll again.
            transient += 1
            trace.event("poll", result="transient", ms=_ms(t0), error=str(exc)[:200])
            sleep(poll_interval_s)
            continue
        except AdapterError as exc:
            trace.event("poll", result="error", ms=_ms(t0), error=str(exc)[:200])
            out.error = f"search failed: {exc}"
            ledger.record(SERVICE, target_date, out)
            return out

        sheets += 1
        peak = max(peak, len(slots))
        sheet = _compact(slots)
        trace.event("poll", result="sheet", ms=_ms(t0), **_sheet_counts(slots, prefs),
                    **({"sheet": sheet} if sheet != last_sheet else {}))
        last_sheet = sheet

        candidates = rank(slots, prefs)
        out.considered = max(out.considered, len(candidates))

        if dry_run:
            if candidates:
                out.dry_run = True
                out.status = Status.BOOKED
                out.would_book = candidates[0]
                out.notes.extend(f"fallback: {s}" for s in candidates[1:3])
                return out
            sleep(poll_interval_s)
            continue

        # Walk down the ranked list. During a release rush the top slot is
        # frequently gone by the time our book() lands — that is expected, not
        # an error, so fall through instead of aborting the run.
        for slot in candidates[: prefs.max_attempts]:
            t1 = _time.monotonic()
            try:
                booking = adapter.book_tee_time(slot.id, prefs.players, prefs.holes)
            except SlotUnavailable as exc:
                trace.event("attempt", slot=f"{slot.tee_time:%H:%M}", result="lost",
                            ms=_ms(t1), error=str(exc)[:160])
                out.attempted.append(f"{slot.tee_time:%-I:%M %p} — lost ({exc})")
                continue
            except AuthExpired as exc:
                trace.event("attempt", slot=f"{slot.tee_time:%H:%M}",
                            result="auth_expired", ms=_ms(t1), error=str(exc)[:160])
                out.error = str(exc)
                ledger.record(SERVICE, target_date, out)
                return out
            except AdapterError as exc:
                # A timeout or server error mid-booking is ambiguous: the
                # reservation may exist even though its reply never arrived.
                # Ask before trying another slot.
                trace.event("attempt", slot=f"{slot.tee_time:%H:%M}", result="error",
                            ms=_ms(t1), error=str(exc)[:160])
                try:
                    landed = _account_booking(adapter, target_date, prefs)
                except Exception:  # noqa: BLE001 — can't confirm either way
                    landed = None
                if landed is None:
                    out.attempted.append(f"{slot.tee_time:%-I:%M %p} — error ({exc})")
                    continue
                trace.event("attempt", slot=f"{landed.slot.tee_time:%H:%M}",
                            result="won (confirmed after error)", ms=_ms(t1))
                out.status = Status.BOOKED
                out.booking = landed
                out.attempted.append(
                    f"{landed.slot.tee_time:%-I:%M %p} — won (confirmed after: {exc})"
                )
                ledger.record(SERVICE, target_date, out)
                return out

            trace.event("attempt", slot=f"{slot.tee_time:%H:%M}", result="won",
                        ms=_ms(t1))

            out.status = Status.BOOKED
            # The live adapter can't know the tee time it just claimed — its
            # reserve response carries ids, not a schedule. Stitch the slot we
            # ranked back in, or the notification reports "now" as your tee time.
            out.booking = replace(booking, slot=slot)
            out.attempted.append(f"{slot.tee_time:%-I:%M %p} — won")
            ledger.record(SERVICE, target_date, out)
            return out

        # Losing every candidate means the sheet is churning *right now*.
        # Re-read it immediately rather than idling a full poll interval —
        # slots freed by other people's abandoned holds appear in this window.
        # Small floor so a pathological loop still can't hammer the server.
        sleep(min(poll_interval_s, 0.15) if candidates else poll_interval_s)

    out.status = Status.UNAVAILABLE
    if out.considered == 0:
        # Say WHICH kind of nothing — they have different fixes.
        window = f"{prefs.no_earlier_than:%-I:%M %p}–{prefs.no_later_than:%-I:%M %p}"
        failed = f", {transient} failed requests" if transient else ""
        if sheets == 0 and not_released == 0 and transient:
            out.notes.append(
                f"couldn't get a usable answer from the server in {deadline_s:.0f}s "
                f"— all {transient} requests failed (timeouts or server errors)"
            )
        elif sheets == 0:
            out.notes.append(
                f"the date never opened in {deadline_s:.0f}s ({not_released} "
                f"'not released yet' answers{failed}) — the release was late or moved"
            )
        elif peak == 0:
            out.notes.append("the date was open but completely sold out — no tee times at all")
        else:
            out.notes.append(
                f"the sheet opened (up to {peak} tee times) but none had "
                f"{prefs.players} open seats for {prefs.holes} holes between {window}"
            )
    ledger.record(SERVICE, target_date, out)
    return out
