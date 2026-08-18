"""Core domain types shared by every adapter and task."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from enum import Enum


class Status(str, Enum):
    """Terminal state of a booking run. Every run ends in exactly one of these,
    and every one of them gets reported."""

    BOOKED = "BOOKED"
    WAITLISTED = "WAITLISTED"
    ALREADY_BOOKED = "ALREADY_BOOKED"
    UNAVAILABLE = "UNAVAILABLE"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class Slot:
    """A bookable tee time."""

    id: str
    course: str
    tee_time: datetime
    holes: int
    spots: int
    price_usd: float | None = None

    def __str__(self) -> str:
        return (
            f"{self.tee_time:%a %b %d %-I:%M %p} · {self.course} · "
            f"{self.holes}h · {self.spots} spots"
        )


@dataclass(frozen=True, slots=True)
class Booking:
    id: str
    slot: Slot
    players: int
    confirmed_at: datetime


@dataclass
class Outcome:
    """What happened, in enough detail to explain it without re-reading logs."""

    status: Status
    target_date: date
    booking: Booking | None = None
    considered: int = 0
    attempted: list[str] = field(default_factory=list)
    error: str | None = None
    notes: list[str] = field(default_factory=list)
    dry_run: bool = False
    would_book: Slot | None = None

    @property
    def ok(self) -> bool:
        return self.status in (Status.BOOKED, Status.WAITLISTED, Status.ALREADY_BOOKED)

    def headline(self) -> str:
        if self.dry_run:
            if self.would_book:
                return f"DRY RUN — would book {self.would_book}"
            return f"DRY RUN — nothing qualifying for {self.target_date:%a %b %d}"
        if self.status is Status.BOOKED and self.booking:
            return f"Booked {self.booking.slot}"
        if self.status is Status.ALREADY_BOOKED:
            return f"Already booked for {self.target_date:%a %b %d} — no action taken"
        if self.status is Status.UNAVAILABLE:
            return f"Nothing available for {self.target_date:%a %b %d}"
        if self.status is Status.ERROR:
            return f"Run failed: {self.error}"
        return f"{self.status.value} for {self.target_date:%a %b %d}"


@dataclass(frozen=True, slots=True)
class GolfPrefs:
    """Declarative booking rules. Loaded from config/preferences.toml so the
    task logic never hardcodes a preference."""

    course: str
    players: int
    holes: int
    no_earlier_than: time
    no_later_than: time
    max_attempts: int = 3

    def matches(self, slot: Slot) -> bool:
        return (
            slot.course == self.course
            and slot.holes >= self.holes
            and slot.spots >= self.players
            and self.no_earlier_than <= slot.tee_time.time() <= self.no_later_than
        )
