"""Delivery of the outcome. The single most important tool in the set.

Auto-booking is only reasonable because the result reaches you within seconds
and every booking is cancellable. If notification is broken, turn auto-book off.
"""

from __future__ import annotations

import subprocess
from datetime import datetime

from booking_agent.models import Outcome, Status

_ICON = {
    Status.BOOKED: "✅",
    Status.WAITLISTED: "⏳",
    Status.ALREADY_BOOKED: "↩️",
    Status.UNAVAILABLE: "❌",
    Status.ERROR: "🔥",
}


def render(outcome: Outcome, body: str | None = None) -> str:
    icon = "🧪" if outcome.dry_run else _ICON[outcome.status]
    lines = [f"{icon} {outcome.headline()}"]
    if body:
        lines += ["", body]
    if outcome.attempted:
        lines += ["", "Attempts:"] + [f"  · {a}" for a in outcome.attempted]
    if outcome.notes:
        lines += ["", "Notes:"] + [f"  · {n}" for n in outcome.notes]
    if outcome.booking:
        lines += ["", f"Cancel with: cancel {outcome.booking.id}"]
    return "\n".join(lines)


def send(outcome: Outcome, body: str | None = None, *, desktop: bool = True) -> str:
    text = render(outcome, body)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n[{stamp}] {'=' * 60}\n{text}\n")

    if desktop:
        _macos_notification(
            title="Booking agent",
            subtitle=outcome.headline()[:110],
        )
    return text


def _macos_notification(title: str, subtitle: str) -> None:
    """Best-effort. A failed toast must never take down the run — the stdout
    copy above is the durable record."""
    script = (
        f'display notification {_osa(subtitle)} '
        f'with title {_osa(title)}'
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _osa(value: str) -> str:
    """Quote a Python string as an AppleScript string literal."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
