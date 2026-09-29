"""Fast, hermetic tests. No network, no real clock, no sleeping.

Run: python -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import date, datetime, time, timedelta

from booking_agent.adapters.fake_golf import FakeGolfAdapter
from booking_agent.models import GolfPrefs, Slot, Status
from booking_agent.store import Ledger
from booking_agent.tasks import golf

PREFS = GolfPrefs(
    course="George Wright Golf Course",
    players=4,
    holes=18,
    no_earlier_than=time(6, 30),
    no_later_than=time(11, 0),
    max_attempts=3,
)

SATURDAY = date(2026, 8, 15)


class FakeClock:
    """Virtual time. sleep() advances it, so a 90s deadline costs 0ms."""

    def __init__(self, start: datetime) -> None:
        self.t = start

    def now(self) -> datetime:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


def slot(hour: int, minute: int = 0, *, holes: int = 18, spots: int = 4) -> Slot:
    return Slot(
        id=f"s-{hour:02d}{minute:02d}",
        course="George Wright Golf Course",
        tee_time=datetime.combine(SATURDAY, time(hour, minute)),
        holes=holes,
        spots=spots,
    )


def ledger() -> Ledger:
    return Ledger(":memory:")


class TestTargetDate(unittest.TestCase):
    def test_tuesday_targets_same_week_saturday(self):
        tuesday = date(2026, 8, 11)
        self.assertEqual(tuesday.weekday(), 1)
        self.assertEqual(golf.next_saturday(tuesday), date(2026, 8, 15))

    def test_saturday_rolls_forward_not_to_today(self):
        self.assertEqual(golf.next_saturday(date(2026, 8, 15)), date(2026, 8, 22))


class TestRanking(unittest.TestCase):
    def test_prefers_earliest_qualifying(self):
        ranked = golf.rank([slot(9), slot(7), slot(8)], PREFS)
        self.assertEqual([s.tee_time.hour for s in ranked], [7, 8, 9])

    def test_rejects_nine_hole_even_when_earliest(self):
        ranked = golf.rank([slot(6, 40, holes=9), slot(8)], PREFS)
        self.assertEqual([s.tee_time.hour for s in ranked], [8])

    def test_rejects_slot_without_room_for_four(self):
        ranked = golf.rank([slot(7, spots=2), slot(8)], PREFS)
        self.assertEqual([s.tee_time.hour for s in ranked], [8])

    def test_respects_time_window(self):
        ranked = golf.rank([slot(6, 0), slot(12, 0), slot(9)], PREFS)
        self.assertEqual([s.tee_time.hour for s in ranked], [9])


class TestIdempotency(unittest.TestCase):
    def test_existing_account_booking_blocks_second_booking(self):
        adapter = FakeGolfAdapter(steal_probability=0.0)
        adapter.preload_booking(SATURDAY)
        clock = FakeClock(datetime(2026, 8, 11, 7, 0))

        out = golf.run(
            adapter, PREFS, SATURDAY, ledger(),
            now=clock.now, sleep=clock.sleep,
        )
        self.assertIs(out.status, Status.ALREADY_BOOKED)
        self.assertEqual(len(adapter.get_existing_bookings()), 1)

    def test_ledger_blocks_retry_after_crash(self):
        led = ledger()
        adapter = FakeGolfAdapter(steal_probability=0.0)
        clock = FakeClock(datetime(2026, 8, 11, 7, 0))

        first = golf.run(
            adapter, PREFS, SATURDAY, led, now=clock.now, sleep=clock.sleep
        )
        self.assertIs(first.status, Status.BOOKED)

        # cron fires again against a *fresh* adapter (process died, site state
        # unknown) — the ledger alone must prevent the double booking.
        second = golf.run(
            FakeGolfAdapter(steal_probability=0.0),
            PREFS, SATURDAY, led, now=clock.now, sleep=clock.sleep,
        )
        self.assertIs(second.status, Status.ALREADY_BOOKED)


class TestRace(unittest.TestCase):
    def test_waits_for_release_then_books(self):
        start = datetime(2026, 8, 11, 6, 59, 55)
        clock = FakeClock(start)
        adapter = FakeGolfAdapter(
            release_at=datetime(2026, 8, 11, 7, 0, 0),
            steal_probability=0.0,
            now=clock.now,
        )

        out = golf.run(
            adapter, PREFS, SATURDAY, ledger(),
            deadline_s=60, poll_interval_s=0.5,
            now=clock.now, sleep=clock.sleep,
        )
        self.assertIs(out.status, Status.BOOKED)
        self.assertGreater(adapter.search_calls, 1, "should have polled before release")

    def test_falls_through_to_next_choice_when_sniped(self):
        clock = FakeClock(datetime(2026, 8, 11, 7, 0))
        # Every book() attempt loses -> exhausts max_attempts each poll.
        adapter = FakeGolfAdapter(steal_probability=1.0, now=clock.now)

        out = golf.run(
            adapter, PREFS, SATURDAY, ledger(),
            deadline_s=5, poll_interval_s=1.0,
            now=clock.now, sleep=clock.sleep,
        )
        self.assertIs(out.status, Status.UNAVAILABLE)
        self.assertGreaterEqual(len(out.attempted), PREFS.max_attempts)
        self.assertTrue(all("lost" in a for a in out.attempted))


class TestReleaseWindow(unittest.TestCase):
    """The real server answers a pre-release date with a 400, not an empty
    sheet. Treating that as an error would abort the run seconds before
    inventory drops — the single most expensive bug available here."""

    def test_not_yet_released_is_polled_through_not_fatal(self):
        clock = FakeClock(datetime(2026, 8, 11, 6, 59, 50))
        adapter = FakeGolfAdapter(
            release_at=datetime(2026, 8, 11, 7, 0, 0),
            steal_probability=0.0,
            now=clock.now,
        )
        out = golf.run(
            adapter, PREFS, SATURDAY, ledger(),
            deadline_s=60, poll_interval_s=0.5,
            now=clock.now, sleep=clock.sleep,
        )
        self.assertIs(out.status, Status.BOOKED)
        self.assertIsNone(out.error)
        self.assertTrue(any("release" in n for n in out.notes))

    def test_still_closed_at_deadline_reports_unavailable_not_error(self):
        clock = FakeClock(datetime(2026, 8, 11, 6, 0, 0))
        adapter = FakeGolfAdapter(
            release_at=datetime(2026, 8, 11, 7, 0, 0), now=clock.now
        )
        out = golf.run(
            adapter, PREFS, SATURDAY, ledger(),
            deadline_s=30, poll_interval_s=1.0,
            now=clock.now, sleep=clock.sleep,
        )
        self.assertIs(out.status, Status.UNAVAILABLE)
        self.assertIsNone(out.error)


class TestPartialAdapter(unittest.TestCase):
    """The live adapter can't read the account yet. That must degrade to
    ledger-only idempotency, not crash the run."""

    def test_missing_account_read_degrades_to_ledger(self):
        class HalfBuilt(FakeGolfAdapter):
            def get_existing_bookings(self):
                raise NotImplementedError("write path pending")

        clock = FakeClock(datetime(2026, 8, 11, 7, 0))
        out = golf.run(
            HalfBuilt(steal_probability=0.0, now=clock.now),
            PREFS, SATURDAY, ledger(),
            now=clock.now, sleep=clock.sleep,
        )
        self.assertIs(out.status, Status.BOOKED)
        self.assertTrue(any("ledger alone" in n for n in out.notes))


class TestConfigMatchesReality(unittest.TestCase):
    """Guards a bug that silently filters out every slot: preferences.toml said
    'George Wright' while the API reports 'George Wright Golf Course', so the
    course check rejected the entire sheet and the run looked like a sold-out
    Saturday. Cheap test, expensive failure."""

    def test_configured_course_name_matches_adapter_output(self):
        from booking_agent import config
        from booking_agent.adapters import fake_golf

        # The tracked example is what every setup starts from; the personal
        # preferences.toml (gitignored) is checked too when present.
        example = config.DEFAULT_PATH.with_name("preferences.example.toml")
        for path in (example, config.DEFAULT_PATH):
            if path.exists():
                with self.subTest(path=path.name):
                    prefs = config.golf_prefs(config.load(path))
                    self.assertEqual(prefs.course, fake_golf._COURSE)


class TestBookingReportsRealTeeTime(unittest.TestCase):
    """The live ReserveTeeTimes response carries ids, not a schedule, so its
    adapter returns a placeholder tee_time. The task must stitch the ranked
    slot back in — otherwise the notification tells you your tee time is
    'right now' and the ledger records a useless timestamp."""

    def test_outcome_carries_the_slot_we_ranked(self):
        class PlaceholderTime(FakeGolfAdapter):
            def book_tee_time(self, slot_id, players, holes):
                b = super().book_tee_time(slot_id, players, holes)
                return replace(b, slot=replace(b.slot, tee_time=datetime(2000, 1, 1)))

        clock = FakeClock(datetime(2026, 8, 11, 7, 0))
        adapter = PlaceholderTime(steal_probability=0.0, now=clock.now)
        out = golf.run(
            adapter, PREFS, SATURDAY, ledger(), now=clock.now, sleep=clock.sleep
        )
        self.assertIs(out.status, Status.BOOKED)
        self.assertEqual(out.booking.slot.tee_time.date(), SATURDAY)
        self.assertNotEqual(out.booking.slot.tee_time.year, 2000)


class TestLedgerIsolation(unittest.TestCase):
    """Regression for the 2026-08-11 miss: a `run --backend fake` demo wrote
    booking GW-0001 into the LIVE ledger, and the next real run saw it and
    refused to book, believing Saturday was already taken. The fake and live
    backends must use physically separate ledgers."""

    def test_fake_and_live_ledgers_are_different_files(self):
        from booking_agent import cli

        self.assertNotEqual(cli._ledger_path("live"), cli._ledger_path("fake"))
        self.assertEqual(cli._ledger_path("live"), cli.LEDGER_PATH)


class TestFailureModes(unittest.TestCase):
    def test_sold_out_reports_unavailable(self):
        clock = FakeClock(datetime(2026, 8, 11, 7, 0))
        out = golf.run(
            FakeGolfAdapter(sold_out=True), PREFS, SATURDAY, ledger(),
            deadline_s=3, poll_interval_s=1.0,
            now=clock.now, sleep=clock.sleep,
        )
        self.assertIs(out.status, Status.UNAVAILABLE)
        self.assertEqual(out.considered, 0)
        self.assertTrue(out.notes)

    def test_expired_session_is_an_error_not_a_silent_miss(self):
        clock = FakeClock(datetime(2026, 8, 11, 7, 0))
        out = golf.run(
            FakeGolfAdapter(auth_expired=True), PREFS, SATURDAY, ledger(),
            now=clock.now, sleep=clock.sleep,
        )
        self.assertIs(out.status, Status.ERROR)
        self.assertIn("log in", out.error)


if __name__ == "__main__":
    unittest.main()
