#!/usr/bin/env python3
"""Schedule ONE run of the agent at a set minute — e.g. an ad hoc Sunday
booking at the moment that date opens.

    python3 scripts/schedule_once.py 2026-10-07 06:58 -- \\
        --backend live --login run --date 2026-10-11 --players 3 \\
        --window 06:00-16:00 --courses george-wright,devine

Installs a launchd job that fires at that minute, runs
`python -m booking_agent.cli <your arguments>`, then removes itself — a
calendar date repeats every year, so a one-off must not stay installed.
Output goes to data/once-<date>-<time>.log.

    python3 scripts/schedule_once.py 2026-10-07 06:58 --cancel

removes it before it fires. The job uses the Python you run this with, so run
it with 3.11+.
"""

from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
AGENTS = Path.home() / "Library" / "LaunchAgents"

# Runs under /bin/sh. Starts in / and cds into the repo itself: launched inside
# ~/Desktop, macOS privacy protection makes sh complain on startup.
SCRIPT = """repo="$1"; py="$2"; plist="$3"; label="$4"; shift 4
cd "$repo" || exit 1
"$py" -m booking_agent.cli "$@"
status=$?
# one-shot: remove the job so it never fires again
rm -f "$plist"
launchctl bootout "gui/$(id -u)/$label"
exit $status
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Run booking_agent.cli once, at a set minute, via launchd.")
    ap.add_argument("date", help="day to fire, YYYY-MM-DD")
    ap.add_argument("time", help="minute to fire, HH:MM (24h), e.g. 06:58")
    ap.add_argument("--cancel", action="store_true",
                    help="remove the one-off scheduled for that date and time")
    ap.add_argument("cli_args", nargs=argparse.REMAINDER,
                    help="-- followed by the booking_agent.cli arguments")
    a = ap.parse_args(argv)

    try:
        when = datetime.strptime(f"{a.date} {a.time}", "%Y-%m-%d %H:%M")
    except ValueError:
        ap.error(f"expected a date like 2026-10-07 and a time like 06:58, "
                 f"got {a.date!r} {a.time!r}")
    label = f"local.booking-agent.once-{when:%Y%m%d-%H%M}"
    plist = AGENTS / f"{label}.plist"
    target = f"gui/{os.getuid()}/{label}"

    if a.cancel:
        subprocess.run(["launchctl", "bootout", target], capture_output=True)
        plist.unlink(missing_ok=True)
        print(f"cancelled {label}")
        return 0

    args = a.cli_args[1:] if a.cli_args[:1] == ["--"] else a.cli_args
    if not args:
        ap.error("put the booking_agent.cli arguments after --")
    if when <= datetime.now():
        ap.error(f"{when:%a %b %d %H:%M} is already past")
    if sys.version_info < (3, 11):
        ap.error("run this with Python 3.11+ — the job uses the same interpreter")

    log = REPO / "data" / f"once-{when:%Y%m%d-%H%M}.log"
    log.parent.mkdir(exist_ok=True)
    AGENTS.mkdir(parents=True, exist_ok=True)
    job = {
        "Label": label,
        "ProgramArguments": ["/bin/sh", "-c", SCRIPT, "once", str(REPO),
                             sys.executable, str(plist), label, *args],
        "WorkingDirectory": "/",
        "StartCalendarInterval": {"Month": when.month, "Day": when.day,
                                  "Hour": when.hour, "Minute": when.minute},
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        "RunAtLoad": False,
    }
    # Re-scheduling the same minute replaces the old job.
    subprocess.run(["launchctl", "bootout", target], capture_output=True)
    plist.write_bytes(plistlib.dumps(job))
    subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)],
                   check=True)
    print(f"scheduled {label}\n"
          f"  fires  {when:%a %b %d %Y %H:%M}\n"
          f"  runs   python -m booking_agent.cli {' '.join(args)}\n"
          f"  log    {log.relative_to(REPO)}\n"
          f"  cancel python3 scripts/schedule_once.py {a.date} {a.time} --cancel")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
