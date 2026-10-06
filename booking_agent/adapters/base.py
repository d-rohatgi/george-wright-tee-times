"""The adapter contract.

Both the fake adapter and the real (Playwright / HTTP) adapter implement this.
Task logic is written against the Protocol only, so swapping in the real site
is a one-line change in cli.py and nothing else moves.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

from booking_agent.models import Booking, Slot


class AdapterError(Exception):
    """Base for anything an adapter can fail with."""


class SlotUnavailable(AdapterError):
    """The slot was taken between search and book. Expected during a release
    rush — the caller should fall through to its next choice, not abort."""


class NotYetReleased(AdapterError):
    """The target date is past the booking window and the server said so.

    CPS answers this with a *400*, not an empty list:
        "Sorry, you are not able to book this tee time currently.
         Your membership only allows..."

    This is the normal state at 06:59:59 on a Tuesday, so it must be treated
    as 'keep polling', never as an error. Getting this wrong means the run
    aborts one second before inventory drops.
    """


class AuthExpired(AdapterError):
    """Session cookie is dead. Not retryable; needs a human to log in again."""


class RateLimited(AdapterError):
    """Back off. Retryable with delay."""


class TransientError(AdapterError):
    """A network or server hiccup: read timeout, connection reset, 5xx.
    Retryable. At 07:00 the server is slowest exactly when it matters (6s
    responses on 2026-10-06), so one bad response must never end a run — on
    2026-09-18 an uncaught read timeout killed the run with no notification."""


@runtime_checkable
class GolfAdapter(Protocol):
    def search_tee_times(self, day: date) -> list[Slot]:
        """Return every visible slot for `day`. Empty list = nothing released
        yet, which is different from nothing matching."""
        ...

    def book_tee_time(self, slot_id: str, players: int, holes: int) -> Booking:
        """Claim a slot. Raises SlotUnavailable if someone beat us to it."""
        ...

    def cancel_booking(self, booking_id: str) -> None:
        """Release a booking. See store.Ledger — the task layer only ever calls
        this to roll back a booking it made in the same run."""
        ...

    def get_existing_bookings(self) -> list[Booking]:
        """Everything currently reserved on the account. Used for the
        idempotency check before any booking attempt."""
        ...
