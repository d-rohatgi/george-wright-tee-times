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


_LEDGERS: list[Ledger] = []


def ledger() -> Ledger:
    led = Ledger(":memory:")
    _LEDGERS.append(led)
    return led


def tearDownModule():
    for led in _LEDGERS:
        led.close()


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


class _NothingMatchesAdapter:
    """An open sheet with only 9-hole twilight slots — what George Wright's
    Saturday looks like once the prime tee times are gone."""

    search_calls = 0

    def get_existing_bookings(self):
        return []

    def search_tee_times(self, day):
        self.search_calls += 1
        return [slot(16, 45, holes=9), slot(17, 30, holes=9, spots=2)]


class TestPollTrace(unittest.TestCase):
    """The trace exists to answer 'why did Tuesday miss?' after the fact."""

    def _run_traced(self, adapter, clock, **kw):
        import json
        import tempfile
        from pathlib import Path

        from booking_agent.trace import PollTrace

        path = Path(tempfile.mkdtemp()) / "run.jsonl"
        trace = PollTrace(path, now=clock.now)
        out = golf.run(adapter, PREFS, SATURDAY, ledger(), now=clock.now,
                       sleep=clock.sleep, trace=trace, **kw)
        trace.close()
        events = [json.loads(line) for line in path.read_text().splitlines()]
        return out, events, path

    def test_records_release_then_the_winning_attempt(self):
        from booking_agent.trace import summarize

        clock = FakeClock(datetime(2026, 8, 11, 6, 59, 58))
        adapter = FakeGolfAdapter(release_at=datetime(2026, 8, 11, 7, 0, 0),
                                  steal_probability=0.0, now=clock.now)
        out, events, path = self._run_traced(adapter, clock,
                                             deadline_s=30, poll_interval_s=0.5)
        self.assertIs(out.status, Status.BOOKED)

        polls = [e for e in events if e["kind"] == "poll"]
        self.assertEqual(polls[0]["result"], "not_released")
        self.assertIn("msg", polls[0], "first refusal should keep the server's wording")
        opened = next(e for e in polls if e["result"] == "sheet")
        self.assertGreater(opened["match"], 0)
        self.assertIn("sheet", opened, "first open sheet should be dumped in full")
        self.assertEqual([e["result"] for e in events if e["kind"] == "attempt"], ["won"])
        self.assertEqual(events[0]["kind"], "start")
        self.assertEqual(events[-1]["status"], "BOOKED")

        timeline = summarize(path)
        self.assertIn("not released", timeline)
        self.assertIn("OPEN", timeline)
        self.assertIn("attempt", timeline)

    def test_note_says_the_date_never_opened(self):
        clock = FakeClock(datetime(2026, 8, 11, 6, 58, 0))
        adapter = FakeGolfAdapter(release_at=datetime(2026, 8, 11, 8, 0, 0),
                                  now=clock.now)
        out, _, _ = self._run_traced(adapter, clock, deadline_s=10, poll_interval_s=1.0)
        self.assertIs(out.status, Status.UNAVAILABLE)
        self.assertTrue(any("never opened" in n for n in out.notes), out.notes)

    def test_note_says_open_but_nothing_matched(self):
        clock = FakeClock(datetime(2026, 8, 11, 7, 0, 0))
        out, events, _ = self._run_traced(_NothingMatchesAdapter(), clock,
                                          deadline_s=3, poll_interval_s=1.0)
        self.assertIs(out.status, Status.UNAVAILABLE)
        self.assertTrue(any("none had 4 open seats" in n for n in out.notes), out.notes)
        sheet = next(e for e in events if e["kind"] == "poll")
        self.assertEqual((sheet["slots"], sheet["holes_ok"], sheet["match"]), (2, 0, 0))

    def test_note_says_sold_out(self):
        clock = FakeClock(datetime(2026, 8, 11, 7, 0, 0))
        out, _, _ = self._run_traced(FakeGolfAdapter(sold_out=True), clock,
                                     deadline_s=3, poll_interval_s=1.0)
        self.assertTrue(any("completely sold out" in n for n in out.notes), out.notes)


class _ScriptedAdapter:
    """Replays a script of results, for failure paths the fake course can't
    produce. Each step is an exception (raised) or a value (returned); the
    last step repeats once the script runs out."""

    def __init__(self, searches, books=(), existing=((),)):
        self._searches, self._books, self._existing = list(searches), list(books), list(existing)
        self.book_calls = 0
        self.search_calls = 0

    @staticmethod
    def _next(script):
        step = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(step, BaseException):
            raise step
        return step

    def get_existing_bookings(self):
        return list(self._next(self._existing))

    def search_tee_times(self, day):
        self.search_calls += 1
        return list(self._next(self._searches))

    def book_tee_time(self, slot_id, players, holes):
        self.book_calls += 1
        return self._next(self._books)


def _booking(s: Slot, rid: str = "R1"):
    from booking_agent.models import Booking

    return Booking(id=rid, slot=s, players=4, confirmed_at=datetime(2026, 8, 11, 7, 0, 5))


class TestServerHiccups(unittest.TestCase):
    """At 07:00 the server answers in up to 6s and sometimes not at all. None of
    that may end a run early, and every run must still report an outcome."""

    def setUp(self):
        from booking_agent.adapters.base import TransientError

        self.Transient = TransientError
        self.clock = FakeClock(datetime(2026, 8, 11, 7, 0, 0))

    def _run(self, adapter, led=None):
        return golf.run(adapter, PREFS, SATURDAY, led or ledger(), deadline_s=30,
                        poll_interval_s=0.5, now=self.clock.now, sleep=self.clock.sleep)

    def test_search_timeouts_are_retried_not_fatal(self):
        adapter = _ScriptedAdapter(
            searches=[self.Transient("read timed out"), self.Transient("503"), [slot(10)]],
            books=[_booking(slot(10))],
        )
        out = self._run(adapter)
        self.assertIs(out.status, Status.BOOKED)
        self.assertEqual(adapter.search_calls, 3)

    def test_booking_timeout_that_actually_landed_is_reported_booked(self):
        # The reply to the reserve call never arrived, but the reservation
        # exists. Must report it — not try a second slot and double-book.
        landed = _booking(slot(10), "R-landed")
        adapter = _ScriptedAdapter(
            searches=[[slot(10), slot(10, 30)]],
            books=[self.Transient("read timed out")],
            existing=[[], [landed]],
        )
        out = self._run(adapter)
        self.assertIs(out.status, Status.BOOKED)
        self.assertEqual(out.booking.id, "R-landed")
        self.assertEqual(adapter.book_calls, 1, "must not attempt a second slot")

    def test_booking_error_that_did_not_land_moves_to_the_next_slot(self):
        adapter = _ScriptedAdapter(
            searches=[[slot(10), slot(10, 30)]],
            books=[self.Transient("502"), _booking(slot(10, 30), "R2")],
        )
        out = self._run(adapter)
        self.assertIs(out.status, Status.BOOKED)
        self.assertEqual(out.booking.id, "R2")
        self.assertTrue(any("error" in a for a in out.attempted), out.attempted)

    def test_account_check_blip_falls_back_to_the_ledger(self):
        adapter = _ScriptedAdapter(
            searches=[[slot(10)]], books=[_booking(slot(10))],
            existing=[self.Transient("timed out")],
        )
        out = self._run(adapter)
        self.assertIs(out.status, Status.BOOKED)
        self.assertTrue(any("account check failed" in n for n in out.notes), out.notes)

    def test_unexpected_crash_still_reports_and_records_an_outcome(self):
        # 2026-09-18: an uncaught error ended the run with no notification and
        # no ledger row. Whatever goes wrong, run() must return an Outcome.
        import contextlib
        import io

        led = ledger()
        with contextlib.redirect_stderr(io.StringIO()):  # the traceback is expected
            out = self._run(_ScriptedAdapter(searches=[RuntimeError("boom")]), led)
        self.assertIs(out.status, Status.ERROR)
        self.assertIn("RuntimeError", out.error)
        self.assertEqual(led.recent(1)[0]["status"], "ERROR")

    def test_note_when_the_server_never_answered(self):
        out = self._run(_ScriptedAdapter(searches=[self.Transient("timed out")]))
        self.assertIs(out.status, Status.UNAVAILABLE)
        self.assertTrue(any("requests failed" in n for n in out.notes), out.notes)


class TestCpsErrorMapping(unittest.TestCase):
    """How raw network failures surface from the live adapter. No network:
    urlopen is patched."""

    def _adapter(self):
        import time as _t

        from booking_agent.adapters.cps_golf import CPSGolfAdapter

        a = CPSGolfAdapter()
        a._token, a._token_fetched_at = "cached", _t.monotonic()  # skip the token call
        return a

    @staticmethod
    def _http_error(code: int, body: bytes):
        import io
        import urllib.error

        return urllib.error.HTTPError("https://x", code, "err", {}, io.BytesIO(body))

    def _raising(self, exc):
        from unittest import mock

        return mock.patch("urllib.request.urlopen", side_effect=exc)

    def test_read_timeout_is_transient_not_a_crash(self):
        from booking_agent.adapters.base import TransientError

        with self._raising(TimeoutError("The read operation timed out")):
            with self.assertRaises(TransientError):
                self._adapter()._get("/TeeTimes")

    def test_server_5xx_is_transient(self):
        from booking_agent.adapters.base import TransientError

        with self._raising(self._http_error(503, b"Service Unavailable")):
            with self.assertRaises(TransientError):
                self._adapter()._get("/TeeTimes")

    def test_not_released_400_is_still_recognised(self):
        from booking_agent.adapters.base import NotYetReleased

        body = b'"Sorry, you are not able to book this tee time currently."'
        with self._raising(self._http_error(400, body)):
            with self.assertRaises(NotYetReleased):
                self._adapter()._get("/TeeTimes")

    def test_login_network_blip_is_transient_not_a_bad_password(self):
        import urllib.error

        from booking_agent.adapters.base import TransientError
        from booking_agent.auth import AuthSession

        with self._raising(urllib.error.URLError("nodename nor servname provided")):
            with self.assertRaises(TransientError):
                AuthSession("you@example.com")._grant({})

    def test_empty_sheet_is_confirmed_once_before_believed(self):
        # The server sometimes answers NO_TEETIMES for a sheet that has tee
        # times (6 of 212 polls on 2026-10-06). One re-check catches it.
        a = self._adapter()
        answers = [{"content": {"messageKey": "NO_TEETIMES"}},
                   {"content": [{"teeSheetId": 1, "startTime": "2026-08-15T10:00:00",
                                 "holes": 18, "availableParticipantNo": [1, 2, 3, 4]}]}]
        a._post = lambda path, body: {}
        a._get = lambda path: answers.pop(0)
        slots = a.search_tee_times(SATURDAY)
        self.assertEqual([s.tee_time.hour for s in slots], [10])

    def test_booking_rules_ask_for_the_given_member_class(self):
        a = self._adapter()
        asked = []
        a._get = lambda path: asked.append(path) or {}
        a.booking_rules("MEM")
        self.assertIn("classcode=MEM", asked[0])


DEVINE = "William J. Devine"


class TestMultiCourse(unittest.TestCase):
    """George Wright and Devine share one CPS tenant: one search covers both,
    and each booking must be made at its own course."""

    def test_prefs_accept_any_listed_course(self):
        devine_slot = replace(slot(10), course=DEVINE)
        self.assertFalse(PREFS.matches(devine_slot))
        both = replace(PREFS, also_courses=(DEVINE,))
        self.assertTrue(both.matches(devine_slot))
        self.assertTrue(both.matches(slot(10)))

    def test_one_search_request_covers_every_course(self):
        from booking_agent.adapters.cps_golf import CPSGolfAdapter

        a = CPSGolfAdapter(also_course_ids=(1,))
        asked = []
        a._post = lambda path, body: {}
        a._get = lambda path: asked.append(path) or {"content": []}
        a.search_tee_times(SATURDAY)
        self.assertIn("courseIds=2%2C1", asked[0])

    def test_a_devine_slot_is_booked_at_devine(self):
        from booking_agent.adapters.cps_golf import CPSGolfAdapter, Identity

        ident = Identity(email="you@example.com", acct="0", card_id=7,
                         card_last4="1234", golfer_id=99)
        a = CPSGolfAdapter(also_course_ids=(1,), auth=object(), identity=ident)
        a._raw_slots["555"] = {"teeSheetId": 555, "courseId": 1, "siteId": 1,
                               "startTime": "2026-08-15T10:00:00", "holes": 18,
                               "availableParticipantNo": [1, 2, 3, 4]}
        posts = []
        replies = {"/LockTeeTimes": {"sessionId": "s", "error": ""},
                   "/TeeTimePricesCalculation": {"transactionId": "tx"},
                   "/ReserveTeeTimes": {"reservationId": 42, "bookingIds": [1]}}
        a._post = lambda path, body: posts.append((path, body)) or replies.get(path, {})
        a._get = lambda path: [{"id": 7, "ccMaskedNumber": "XXXX1234", "ccToken": "t" * 32}]

        booking = a.book_tee_time("555", 3, 18)

        self.assertEqual(booking.id, "42")
        restrict = next(body for path, body in posts if path == "/CheckRestrictReservation")
        self.assertEqual((restrict["courseId"], restrict["siteId"]), (1, 1))

    def test_a_booking_at_either_course_counts_as_already_booked(self):
        held = _booking(replace(slot(15), course=DEVINE))
        out = golf.run(
            _ScriptedAdapter(searches=[[slot(10)]], existing=[[held]]),
            replace(PREFS, also_courses=(DEVINE,)), SATURDAY, ledger(),
            now=FakeClock(datetime(2026, 8, 11, 7, 0)).now,
        )
        self.assertIs(out.status, Status.ALREADY_BOOKED)

    def test_cli_overrides_make_one_off_prefs_without_touching_config(self):
        import argparse
        from unittest import mock

        from booking_agent import cli

        args = argparse.Namespace(players=3, holes=18, window="06:00-16:00",
                                  courses="george-wright,devine")
        with mock.patch.object(cli.config, "golf_prefs", return_value=PREFS):
            prefs = cli._prefs(args)
        self.assertEqual(prefs.players, 3)
        self.assertEqual((prefs.no_earlier_than, prefs.no_later_than), (time(6), time(16)))
        self.assertEqual(prefs.courses, ("George Wright Golf Course", DEVINE))
        self.assertEqual(PREFS.players, 4, "config prefs must be untouched")

    def test_cli_rejects_an_unknown_course(self):
        import argparse

        from booking_agent import cli

        with self.assertRaises(SystemExit):
            cli._course_names(argparse.Namespace(courses="franklin-park"))


if __name__ == "__main__":
    unittest.main()
