"""A synthetic George Wright.

Exists so the whole agent can run end-to-end at 2pm on a Thursday without a
login, a network call, or a real reservation. It deliberately reproduces the
three things that break real booking agents:

  1. Inventory does not exist until `release_at` (search returns []).
  2. Slots get taken between search and book (SlotUnavailable).
  3. The account may already hold a booking for the target date.

Seeded, so tests are deterministic.
"""

from __future__ import annotations

import itertools
import random
from datetime import date, datetime, timedelta

from booking_agent.adapters.base import AuthExpired, NotYetReleased, SlotUnavailable
from booking_agent.models import Booking, Slot

_COURSE = "George Wright Golf Course"  # exactly as the live API reports it


class FakeGolfAdapter:
    def __init__(
        self,
        *,
        release_at: datetime | None = None,
        seed: int = 7,
        steal_probability: float = 0.35,
        sold_out: bool = False,
        auth_expired: bool = False,
        now=datetime.now,
    ) -> None:
        self.release_at = release_at
        self.steal_probability = steal_probability
        self.sold_out = sold_out
        self.auth_expired = auth_expired
        self._now = now
        self._rng = random.Random(seed)
        self._bookings: dict[str, Booking] = {}
        self._counter = itertools.count(1)
        self._taken: set[str] = set()
        self.search_calls = 0

    # -- adapter surface ---------------------------------------------------

    def search_tee_times(self, day: date) -> list[Slot]:
        self._guard()
        self.search_calls += 1
        if self.sold_out:
            return []
        if self.release_at is not None and self._now() < self.release_at:
            # Mirrors the real server, which answers a pre-release date with a
            # 400 rather than an empty sheet. Verified against CPS 2026-08-10.
            raise NotYetReleased(
                "Sorry, you are not able to book this tee time currently."
            )
        return [s for s in self._inventory(day) if s.id not in self._taken]

    def book_tee_time(self, slot_id: str, players: int, holes: int) -> Booking:
        self._guard()
        if slot_id in self._taken:
            raise SlotUnavailable(f"{slot_id} already taken")
        if self._rng.random() < self.steal_probability:
            self._taken.add(slot_id)
            raise SlotUnavailable(f"{slot_id} claimed by another player mid-request")
        slot = self._by_id(slot_id)
        if slot is None:
            raise SlotUnavailable(f"{slot_id} not found")
        if slot.spots < players:
            raise SlotUnavailable(f"{slot_id} only has {slot.spots} spots")
        self._taken.add(slot_id)
        booking = Booking(
            id=f"GW-{next(self._counter):04d}",
            slot=slot,
            players=players,
            confirmed_at=self._now(),
        )
        self._bookings[booking.id] = booking
        return booking

    def cancel_booking(self, booking_id: str) -> None:
        self._guard()
        booking = self._bookings.pop(booking_id, None)
        if booking is not None:
            self._taken.discard(booking.slot.id)

    def get_existing_bookings(self) -> list[Booking]:
        self._guard()
        return list(self._bookings.values())

    # -- test helpers ------------------------------------------------------

    def preload_booking(self, day: date, hour: int = 9) -> Booking:
        """Seed an existing reservation, to exercise the idempotency path."""
        slot = Slot(
            id=f"seed-{day:%Y%m%d}-{hour}",
            course=_COURSE,
            tee_time=datetime.combine(day, datetime.min.time()) + timedelta(hours=hour),
            holes=18,
            spots=4,
        )
        booking = Booking(
            id=f"GW-SEED-{next(self._counter):04d}",
            slot=slot,
            players=4,
            confirmed_at=self._now(),
        )
        self._bookings[booking.id] = booking
        self._taken.add(slot.id)
        return booking

    # -- internals ---------------------------------------------------------

    def _guard(self) -> None:
        if self.auth_expired:
            raise AuthExpired("session cookie rejected — log in again")

    def _inventory(self, day: date) -> list[Slot]:
        """Tee times every 10 minutes from 06:00 to 18:00, with a plausible
        scatter of partially-filled and 9-hole-only slots."""
        rng = random.Random(f"{day:%Y%m%d}")
        base = datetime.combine(day, datetime.min.time()) + timedelta(hours=6)
        slots: list[Slot] = []
        for i in range(72):
            tee = base + timedelta(minutes=10 * i)
            if rng.random() < 0.55:
                continue  # already booked by the general public
            slots.append(
                Slot(
                    id=f"gw-{day:%Y%m%d}-{tee:%H%M}",
                    course=_COURSE,
                    tee_time=tee,
                    holes=18 if rng.random() > 0.15 else 9,
                    spots=rng.choice([1, 2, 2, 3, 4, 4, 4]),
                    price_usd=round(rng.uniform(38, 72), 2),
                )
            )
        return slots

    def _by_id(self, slot_id: str) -> Slot | None:
        try:
            day = datetime.strptime(slot_id.split("-")[1], "%Y%m%d").date()
        except (IndexError, ValueError):
            return None
        return next((s for s in self._inventory(day) if s.id == slot_id), None)
