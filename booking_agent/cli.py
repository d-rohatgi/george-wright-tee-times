"""Entry point. `python -m booking_agent.cli <command>`

Default backend is `fake` and the model is off. Both are opt-in:
    --backend live    hit the real CPS API
    --brief           run the Phase B model summary
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime
from pathlib import Path

from booking_agent import config, context, notify
from booking_agent.adapters.cps_golf import CPSGolfAdapter
from booking_agent.adapters.fake_golf import FakeGolfAdapter
from booking_agent.models import Status
from booking_agent.store import Ledger
from booking_agent.tasks import golf
from booking_agent.trace import PollTrace, summarize

_DATA = Path(__file__).resolve().parent.parent / "data"
LEDGER_PATH = _DATA / "ledger.db"
# The fake backend gets its own ledger. This is not cosmetic: on 2026-08-11 a
# `run --backend fake` demo wrote booking GW-0001 into the live ledger, and the
# next morning's real job saw it, believed Aug 15 was already booked, and
# refused to act. Test data must never be able to short-circuit a real run.
FAKE_LEDGER_PATH = _DATA / "ledger-fake.db"


def _ledger_path(backend: str) -> Path:
    return LEDGER_PATH if backend == "live" else FAKE_LEDGER_PATH


def _polls_dir(backend: str) -> Path:
    # Split like the ledger, so a fake rehearsal never shows up as a real run.
    return _DATA / ("polls" if backend == "live" else "polls-fake")


def _adapter(args):
    if args.backend == "live":
        if getattr(args, "login", False):
            from booking_agent.auth import AuthSession

            ident = config.identity()
            return CPSGolfAdapter(auth=AuthSession(ident.email), identity=ident)
        return CPSGolfAdapter()
    return FakeGolfAdapter(
        sold_out=getattr(args, "sold_out", False),
        auth_expired=getattr(args, "auth_expired", False),
        steal_probability=getattr(args, "contention", 0.35),
    )


def _target(args, adapter) -> date:
    if args.date:
        return date.fromisoformat(args.date)
    # Always target the upcoming Saturday. The job runs Tue–Fri (not Tuesday
    # only), so a holiday-shifted release — like Labor Day pushing the Sep 12
    # sheet past the Tuesday 07:00 window — gets caught the next morning
    # instead of missed. The ledger's idempotency guard makes every run after
    # the booking a no-op, so retrying daily costs nothing.
    return golf.next_saturday(date.today())


def cmd_run(args) -> int:
    prefs = config.golf_prefs()
    raw = config.load()["george_wright"]
    adapter = _adapter(args)
    target = _target(args, adapter)

    if getattr(args, "preload", False) and args.backend == "fake":
        adapter.preload_booking(target)

    ledger = Ledger(_ledger_path(args.backend))
    trace = PollTrace(
        _polls_dir(args.backend) / f"{datetime.now():%Y-%m-%d_%H%M%S}_{target}.jsonl"
    )
    try:
        outcome = golf.run(
            adapter,
            prefs,
            target,
            ledger,
            deadline_s=raw.get("deadline_seconds", 90),
            poll_interval_s=raw.get("poll_interval_seconds", 0.5),
            dry_run=args.dry_run,
            trace=trace,
        )
    finally:
        trace.close()
    if not outcome.ok:
        outcome.notes.append(f"poll log: data/{trace.path.parent.name}/{trace.path.name} "
                             "— `cli polls` shows the timeline")

    body = None
    if args.brief:
        from booking_agent import brief  # imported only when asked for

        body = brief.compose(outcome, weather=context.check_weather(target))
    elif outcome.ok and not outcome.dry_run:
        w = context.check_weather(target)
        if w.get("summary"):
            body = (
                f"Forecast: {w['summary']}, {w.get('temp_f')}°F, "
                f"{w.get('precip_pct')}% precip, wind {w.get('wind')}"
            )

    notify.send(outcome, body, desktop=not args.quiet)
    ledger.close()
    return 0 if outcome.ok else 1


def cmd_peek(args) -> int:
    """Read-only look at a tee sheet. No booking, no ledger."""
    adapter = _adapter(args)
    prefs = config.golf_prefs()
    target = _target(args, adapter)
    print(f"{target:%A %b %d %Y} — {args.backend} backend\n")
    try:
        slots = adapter.search_tee_times(target)
    except Exception as exc:  # noqa: BLE001
        print(f"  {type(exc).__name__}: {exc}")
        return 1
    if not slots:
        print("  (no tee times published)")
        return 0
    qualifying = {s.id for s in golf.rank(slots, prefs)}
    for s in sorted(slots, key=lambda s: s.tee_time):
        mark = "✓" if s.id in qualifying else " "
        price = f"${s.price_usd:.0f}" if s.price_usd else "—"
        print(f"  {mark} {s.tee_time:%-I:%M %p}  {s.holes:>2}h  "
              f"{s.spots} spot(s)  {price:>5}")
    print(f"\n  {len(qualifying)} of {len(slots)} match your rules")
    return 0


def cmd_rules(args) -> int:
    # The window depends on member class (MEM books a day earlier than
    # R/RES/NRES), so show the configured account's own rule when there is one.
    try:
        cls = config.identity().member_class
    except (OSError, KeyError):
        cls = "R"
    adapter = CPSGolfAdapter()
    rules = adapter.booking_rules(cls)
    print(f"George Wright booking rules for class {cls} (from the server):")
    for k, v in rules.items():
        print(f"  {k:<20} {v}")
    print(f"\n  next bookable date: {adapter.target_date(class_code=cls)}")
    return 0


def cmd_whoami(args) -> int:
    """Prove the Keychain credential and account config work, book nothing."""
    from booking_agent.auth import AuthSession

    ident = config.identity()
    adapter = CPSGolfAdapter(auth=AuthSession(ident.email), identity=ident)
    print(f"email        {ident.email}")
    try:
        print(f"golferId     {adapter.golfer_id}")
    except Exception as exc:  # noqa: BLE001
        print(f"golferId     FAILED — {exc}")
        return 1
    print(f"member class {adapter.member_class_code()}  (config: {ident.member_class})")
    # Tokens are never shown — only the lookup keys preferences.toml needs.
    print("saved cards  id        last4  type        expires  acct")
    for c in adapter.saved_cards():
        mark = "→" if c["id"] == ident.card_id else " "
        print(f"           {mark} {c['id']!s:<9} {c['last4']:<6} {c['type']!s:<11} "
              f"{c['expires']!s:<8} {c['acct']}{'  (default)' if c['default'] else ''}")
    try:
        token = adapter.resolve_card_token()
        print(f"card         id {ident.card_id} ending {ident.card_last4} "
              f"— token resolved ({len(token)} chars, not shown)")
    except Exception as exc:  # noqa: BLE001
        print(f"card         FAILED — {exc}")
        return 1
    bookings = adapter.get_existing_bookings()
    print(f"upcoming     {len(bookings)} reservation(s)")
    for b in bookings:
        print(f"             {b.id}  {b.slot.tee_time:%a %b %d %-I:%M %p}  "
              f"{b.slot.holes}h  {b.players}p")
    return 0


def cmd_cancel(args) -> int:
    from booking_agent.auth import AuthSession

    ident = config.identity()
    adapter = CPSGolfAdapter(auth=AuthSession(ident.email), identity=ident)
    adapter.cancel_booking(args.reservation_id)
    print(f"cancelled {args.reservation_id}")
    return 0


def cmd_preflight(args) -> int:
    """Monday-night go/no-go. Every line must be OK for the 07:00 run to fire
    and succeed. Motivated by 2026-08-11, where several of these were quietly
    not-OK and the run missed. Read-only."""
    import subprocess

    ident = config.identity()
    target = golf.next_saturday(date.today())
    checks: list[tuple[bool, str]] = []

    def sh(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=8).stdout
        except Exception:  # noqa: BLE001
            return ""

    # power: on AC and not going to idle-sleep
    batt = sh(["pmset", "-g", "batt"])
    on_ac = "AC Power" in batt
    checks.append((on_ac, f"on AC power  ({'yes' if on_ac else 'NO — plug it in'})"))

    sleepline = sh(["pmset", "-g"])
    no_sleep = bool(re.search(r"\bsleep\s+0\b", sleepline))
    checks.append((no_sleep, f"idle-sleep disabled on AC  ({'yes' if no_sleep else 'NO'})"))

    # scheduled wake present (backup path)
    wake = "wake at" in sh(["pmset", "-g", "sched"])
    checks.append((wake, f"pmset backup wake scheduled  ({'yes' if wake else 'no'})"))

    # launchd job loaded
    loaded = any(line.split()[-1].endswith("booking-agent")
                 for line in sh(["launchctl", "list"]).splitlines() if line.split())
    checks.append((loaded, f"launchd job loaded  ({'yes' if loaded else 'NO — load the plist'})"))

    # lid: can't read reliably; remind
    checks.append((True, "laptops: keep the lid OPEN overnight (closing forces sleep)"))

    # auth + card + account, and no stale ledger entry for the target
    ledger = Ledger(_ledger_path("live"))
    try:
        adapter = CPSGolfAdapter(auth=__import__(
            "booking_agent.auth", fromlist=["AuthSession"]
        ).AuthSession(ident.email), identity=ident)
        gid = adapter.golfer_id
        adapter.resolve_card_token()
        auth_ok, auth_msg = True, f"auth + card OK (golfer {gid}, card {ident.card_last4})"
    except Exception as exc:  # noqa: BLE001
        auth_ok, auth_msg = False, f"auth/card FAILED — {exc}"
    checks.append((auth_ok, auth_msg))

    prior = ledger.prior_success(golf.SERVICE, target)
    clean = prior is None
    checks.append((clean, f"no stale ledger booking for {target:%b %d}  "
                          f"({'clean' if clean else 'STALE: ' + prior})"))
    ledger.close()

    print(f"\nPreflight — target {target:%A %b %d %Y}\n")
    for ok, msg in checks:
        print(f"  {'✓' if ok else '✗'} {msg}")
    hard = all(ok for ok, _ in checks)
    print(f"\n  {'READY — it will fire at 06:58 and attempt the booking.' if hard else 'NOT READY — fix the ✗ lines above.'}\n")
    return 0 if hard else 1


def cmd_simulate(args) -> int:
    from datetime import datetime, timedelta

    prefs = config.golf_prefs()
    target = golf.next_saturday(date.today())
    adapter = FakeGolfAdapter(
        release_at=datetime.now() + timedelta(seconds=args.delay),
        steal_probability=args.contention,
        seed=args.seed,
    )
    ledger = Ledger(":memory:")
    print(f"Inventory releases in {args.delay}s. Polling for {args.deadline}s…")
    outcome = golf.run(
        adapter, prefs, target, ledger, deadline_s=args.deadline, poll_interval_s=0.25
    )
    print(f"search_tee_times() called {adapter.search_calls}x")
    notify.send(outcome, desktop=False)
    ledger.close()
    return 0 if outcome.ok else 1


def cmd_polls(args) -> int:
    """Timeline of one run's polls: when the date opened, what the sheet held,
    which filter excluded what, and every lock attempt."""
    if args.file:
        path = Path(args.file)
    else:
        logs = sorted(_polls_dir(args.backend).glob("*.jsonl"))
        if args.target:
            logs = [p for p in logs if p.stem.endswith(args.target)]
        if not logs:
            print(f"no poll logs in {_polls_dir(args.backend)}")
            return 1
        path = logs[-1]
    print(summarize(path))
    return 0


def cmd_history(args) -> int:
    ledger = Ledger(_ledger_path(args.backend))
    rows = ledger.recent(args.limit)
    if not rows:
        print("no attempts recorded yet")
    for r in rows:
        badge = "✓" if r["status"] == Status.BOOKED.value else " "
        print(f"{badge} {r['created_at']}  {r['service']:<16} "
              f"{r['target_date']}  {r['status']:<15} {r['booking_id'] or ''}")
    ledger.close()
    return 0


def cmd_check_model(args) -> int:
    from booking_agent import brief

    print(f"endpoint : {brief.MODEL_URL}\nmodel    : {brief.MODEL_NAME}")
    try:
        reply = brief._chat(
            '{"status":"BOOKED","booked":{"tee_time":"2026-08-22T07:10:00",'
            '"course":"George Wright","players":4,"holes":18,"price_usd":52.0},'
            '"forecast":{"summary":"Rain likely","precip_pct":70}}'
        )
        print(f"\n{reply}\n\nModel reachable.")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"\nModel unreachable: {exc}\nStart it with:  ollama serve")
        return 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="booking-agent")
    p.add_argument("--backend", default="fake", choices=["fake", "live"])
    p.add_argument("--login", action="store_true",
                   help="use the signed-in session (Keychain); required to book")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="book the next available Saturday tee time")
    r.add_argument("--date", help="YYYY-MM-DD; defaults to the upcoming Saturday")
    r.add_argument("--dry-run", action="store_true", help="rank but never book")
    r.add_argument("--brief", action="store_true", help="run the Phase B model")
    r.add_argument("--quiet", action="store_true", help="no desktop notification")
    r.add_argument("--preload", action="store_true", help="fake: seed a booking")
    r.add_argument("--sold-out", action="store_true", help="fake: empty sheet")
    r.add_argument("--auth-expired", action="store_true", help="fake: dead session")
    r.add_argument("--contention", type=float, default=0.35, help="fake: snipe rate")
    r.set_defaults(func=cmd_run)

    k = sub.add_parser("peek", help="read-only look at a tee sheet")
    k.add_argument("--date", help="YYYY-MM-DD")
    k.set_defaults(func=cmd_peek)

    sub.add_parser("rules", help="show the server's booking window").set_defaults(
        func=cmd_rules
    )

    s = sub.add_parser("simulate", help="rehearse the release-time race")
    s.add_argument("--delay", type=float, default=3.0)
    s.add_argument("--deadline", type=float, default=30.0)
    s.add_argument("--contention", type=float, default=0.6)
    s.add_argument("--seed", type=int, default=7)
    s.set_defaults(func=cmd_simulate)

    sub.add_parser("whoami", help="verify login, card and upcoming bookings"
                   ).set_defaults(func=cmd_whoami)

    sub.add_parser("preflight", help="Monday-night go/no-go before the run"
                   ).set_defaults(func=cmd_preflight)

    c = sub.add_parser("cancel", help="cancel a reservation by id")
    c.add_argument("reservation_id")
    c.set_defaults(func=cmd_cancel)

    g = sub.add_parser("polls", help="timeline of a run's polls (latest by default)")
    g.add_argument("--target", help="YYYY-MM-DD: latest run for this Saturday")
    g.add_argument("--file", help="a specific data/polls/*.jsonl")
    g.set_defaults(func=cmd_polls)

    h = sub.add_parser("history", help="show past attempts")
    h.add_argument("--limit", type=int, default=20)
    h.set_defaults(func=cmd_history)

    sub.add_parser("check-model", help="verify the local model responds").set_defaults(
        func=cmd_check_model
    )

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
