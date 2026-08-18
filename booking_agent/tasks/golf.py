"""Phase A — the deterministic hot path.

No LLM runs here. At 07:00:00 on a Tuesday this is a tight, boring loop whose
only job is to claim the earliest qualifying tee time before anyone else does.
The model gets involved afterwards, in brief.py, where being slow is free.
"""

from __future__ import annotations

import time as _time
from dataclasses import replace
from datetime import date, datetime, timedelta

from booking_agent.adapters.base import (
    AdapterError,
    AuthExpired,
    GolfAdapter,
    NotYetReleased,
    RateLimited,
    SlotUnavailable,
)
from booking_agent.models import GolfPrefs, Outcome, Slot, Status
from booking_agent.store import Ledger

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
) -> Outcome:
    out = Outcome(status=Status.ERROR, target_date=target_date)

    # --- idempotency: two independent checks, cheapest first ---------------
    prior = ledger.prior_success(SERVICE, target_date)
    if prior:
        out.status = Status.ALREADY_BOOKED
        out.notes.append(f"ledger already holds booking {prior} for this date")
        return out

    try:
        for booking in adapter.get_existing_bookings():
            if (
                booking.slot.tee_time.date() == target_date
                and booking.slot.course == prefs.course
            ):
                out.status = Status.ALREADY_BOOKED
                out.booking = booking
                out.notes.append("account already holds a booking for this date")
                return out
    except AuthExpired as exc:
        out.error = f"{exc} (re-run `python -m booking_agent.cli login`)"
        return out
    except NotImplementedError:
        # Live adapter can't read the account yet. Degrade to ledger-only
        # rather than refusing to run — but say so, because a booking made
        # outside this agent is now invisible to it.
        out.notes.append(
            "account check unavailable (write path not implemented) — "
            "relying on the local ledger alone for idempotency"
        )

    # --- poll for release, then claim -------------------------------------
    started = now()
    backoff = poll_interval_s
    waited_on_release = False

    while (now() - started).total_seconds() < deadline_s:
        try:
            slots = adapter.search_tee_times(target_date)
        except NotYetReleased:
            # The normal state at 06:59:59. Keep polling — this is precisely
            # the window we are here to win.
            if not waited_on_release:
                waited_on_release = True
                out.notes.append("waited for the release window to open")
            sleep(poll_interval_s)
            continue
        except AuthExpired as exc:
            out.error = str(exc)
            return out
        except RateLimited:
            backoff = min(backoff * 2, 5.0)
            sleep(backoff)
            continue
        except AdapterError as exc:
            out.error = f"search failed: {exc}"
            ledger.record(SERVICE, target_date, out)
            return out

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
            try:
                booking = adapter.book_tee_time(slot.id, prefs.players, prefs.holes)
            except SlotUnavailable as exc:
                out.attempted.append(f"{slot.tee_time:%-I:%M %p} — lost ({exc})")
                continue
            except AuthExpired as exc:
                out.error = str(exc)
                ledger.record(SERVICE, target_date, out)
                return out

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
        out.notes.append(
            f"no inventory appeared within {deadline_s:.0f}s — "
            "either the release window moved or the page shape changed"
        )
    ledger.record(SERVICE, target_date, out)
    return out
