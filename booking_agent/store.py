"""Attempt ledger — the idempotency guard.

The failure this prevents: the 7am job crashes after the site confirms a
booking but before the process records it, cron retries, and you end up
double-booked with a late-cancel fee. Every run checks here first.

SQLite because it is stdlib and its writes are atomic; a JSON file rewritten
in place is exactly the thing that loses a record when a run dies mid-write.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path

from booking_agent.models import Outcome, Status

_SCHEMA = """
CREATE TABLE IF NOT EXISTS attempts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    service     TEXT    NOT NULL,
    target_date TEXT    NOT NULL,
    status      TEXT    NOT NULL,
    booking_id  TEXT,
    detail      TEXT    NOT NULL,
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempts_lookup
    ON attempts (service, target_date, status);
"""


class Ledger:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)

    def prior_success(self, service: str, target_date: date) -> str | None:
        """Booking id from an earlier successful run for this target, if any.

        Checked *before* the adapter's own get_existing_bookings(), because
        this works even when the site is down or the session has expired.
        """
        row = self._conn.execute(
            """SELECT booking_id FROM attempts
               WHERE service = ? AND target_date = ? AND status = ?
               ORDER BY id DESC LIMIT 1""",
            (service, target_date.isoformat(), Status.BOOKED.value),
        ).fetchone()
        return row["booking_id"] if row else None

    def record(self, service: str, target_date: date, outcome: Outcome) -> None:
        detail = {
            "considered": outcome.considered,
            "attempted": outcome.attempted,
            "notes": outcome.notes,
            "error": outcome.error,
        }
        self._conn.execute(
            """INSERT INTO attempts
                 (service, target_date, status, booking_id, detail, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                service,
                target_date.isoformat(),
                outcome.status.value,
                outcome.booking.id if outcome.booking else None,
                json.dumps(detail),
                datetime.now().isoformat(timespec="seconds"),
            ),
        )

    def recent(self, limit: int = 20) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM attempts ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()

    def close(self) -> None:
        self._conn.close()
