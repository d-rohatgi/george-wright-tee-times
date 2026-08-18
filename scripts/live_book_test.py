"""One real book-and-cancel against the live site, to prove book_tee_time().

Books the LATEST qualifying slot (least likely to be a prime time someone else
wants) and cancels immediately. Cancellation is in a finally block so a failure
partway through the six-call sequence still releases the reservation.

Run deliberately. This creates a real reservation.
"""

from __future__ import annotations

import sys
from datetime import date

from booking_agent import config
from booking_agent.adapters.cps_golf import CPSGolfAdapter
from booking_agent.auth import AuthSession

TARGET = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else date(2026, 8, 13)
MAX_PLAYERS = int(sys.argv[2]) if len(sys.argv) > 2 else 4
MIN_HOLES = int(sys.argv[3]) if len(sys.argv) > 3 else 9


def main() -> int:
    ident = config.identity()
    adapter = CPSGolfAdapter(auth=AuthSession(ident.email), identity=ident)

    print(f"[1] existing reservations: {len(adapter.get_existing_bookings())}")

    slots = adapter.search_tee_times(TARGET)
    usable = [s for s in slots if s.holes >= MIN_HOLES and s.spots >= 1]
    if not usable:
        print(f"    nothing bookable on {TARGET} "
              f"({len(slots)} slots: {sorted({(s.holes, s.spots) for s in slots})})")
        return 1

    slot = max(usable, key=lambda s: s.tee_time)  # latest = least contested
    players = min(slot.spots, MAX_PLAYERS)
    print(f"[2] booking {slot}  ({players} player(s), ${slot.price_usd} ea)")

    booking = None
    try:
        booking = adapter.book_tee_time(slot.id, players, slot.holes)
        print(f"[3] BOOKED  reservationId={booking.id}")

        upcoming = adapter.get_existing_bookings()
        print(f"[4] verify: {len(upcoming)} upcoming")
        for b in upcoming:
            print(f"    {b.id}  {b.slot.tee_time:%a %b %d %-I:%M %p}  "
                  f"{b.slot.holes}h  {b.players}p")
    finally:
        if booking is not None:
            print(f"[5] cancelling {booking.id} …")
            adapter.cancel_booking(booking.id)
            left = adapter.get_existing_bookings()
            print(f"[6] after cancel: {len(left)} upcoming "
                  f"{'— clean' if not left else '— STILL PRESENT, check manually'}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
