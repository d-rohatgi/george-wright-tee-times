"""Per-poll trace of one run — the evidence behind an outcome.

A notification can only say what happened in aggregate. When a release morning
goes wrong the question is always *which* of these it was:

  - the date never opened while we polled (release moved or delayed)
  - it opened, but nothing on the sheet matched (gone in the first second, or
    never offered online)
  - something matched, and we lost the lock race

One JSON line per poll answers that. A line costs microseconds to append, so
it is safe on the hot path, and it's line-buffered so a crash mid-run still
leaves everything up to the crash on disk.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


class NullTrace:
    """Records nothing. The default, so tests and callers that don't care
    pay nothing."""

    path: Path | None = None

    def event(self, kind: str, **fields) -> None:
        pass

    def close(self) -> None:
        pass


class PollTrace(NullTrace):
    def __init__(self, path: Path, now=datetime.now) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._now = now
        self._fh = open(path, "a", buffering=1, encoding="utf-8")

    def event(self, kind: str, **fields) -> None:
        rec = {"t": self._now().isoformat(timespec="milliseconds"), "kind": kind}
        rec.update(fields)
        self._fh.write(json.dumps(rec, default=str) + "\n")

    def close(self) -> None:
        self._fh.close()


# -- reading a trace back -----------------------------------------------------


def _clock(ts: str) -> str:
    return ts.split("T")[-1]


def _poll_label(ev: dict) -> str:
    result = ev.get("result")
    if result == "not_released":
        return "not released"
    if result == "sheet":
        if not ev.get("slots"):
            return "OPEN — sold out (no tee times at all)"
        return (
            f"OPEN — {ev['slots']} tee times · {ev.get('holes_ok', 0)} with enough "
            f"holes · {ev.get('seats_ok', 0)} with enough seats · "
            f"{ev.get('window_ok', 0)} in window · {ev.get('match', 0)} match"
        )
    return f"{result}: {ev.get('error', '')}".strip()


def summarize(path: Path) -> str:
    """A trace as a timeline. Consecutive identical polls collapse into one
    line; the sheet is printed whenever it changed."""
    events = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
    lines = [f"Poll log  {path.name}"]

    group: list[dict] = []

    def flush() -> None:
        if not group:
            return
        first, last = group[0], group[-1]
        ms = [e["ms"] for e in group if "ms" in e]
        span = _clock(first["t"])
        if len(group) > 1:
            span += f" → {_clock(last['t'])}"
        tail = f"  ×{len(group)}" if len(group) > 1 else ""
        if ms:
            tail += f"  ({sum(ms) / len(ms):.0f} ms/poll)"
        lines.append(f"  {span:<27} {_poll_label(first)}{tail}")
        if first.get("msg"):
            lines.append(f"  {'':<27} server said: {first['msg']}")
        if first.get("sheet"):
            lines.append(f"  {'':<27} sheet: {first['sheet']}")
        group.clear()

    def key(ev: dict) -> tuple:
        return (ev.get("result"), ev.get("slots"), ev.get("match"),
                ev.get("seats_ok"), ev.get("window_ok"), ev.get("error"))

    for ev in events:
        kind = ev.get("kind")
        if kind == "poll":
            if group and ("sheet" in ev or "msg" in ev or key(ev) != key(group[0])):
                flush()
            group.append(ev)
            continue
        flush()
        if kind == "start":
            lines.append(
                f"Target {ev.get('target')} · {ev.get('players')} players · "
                f"{ev.get('holes')} holes · {ev.get('window')} · "
                f"deadline {ev.get('deadline_s')}s{' · DRY RUN' if ev.get('dry_run') else ''}"
            )
        elif kind == "attempt":
            detail = f" ({ev['error']})" if ev.get("error") else ""
            lines.append(
                f"  {_clock(ev['t']):<27} attempt {ev.get('slot')} — "
                f"{ev.get('result')}{detail}  {ev.get('ms', '?')} ms"
            )
        elif kind == "end":
            lines.append(
                f"\n  {_clock(ev['t']):<27} end: {ev.get('status')} · "
                f"{ev.get('polls', 0)} polls · {ev.get('attempts', 0)} attempts"
                + (f" · error: {ev['error']}" if ev.get("error") else "")
            )
        else:
            lines.append(f"  {_clock(ev.get('t', '')):<27} {kind}: {ev}")
    flush()
    return "\n".join(lines)
