"""Preference loading. tomllib is stdlib, so this stays dependency-free."""

from __future__ import annotations

import tomllib
from datetime import time
from pathlib import Path

from booking_agent.models import GolfPrefs

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "preferences.toml"


def _parse_time(value: str) -> time:
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


def load(path: Path | str | None = None) -> dict:
    with open(Path(path) if path else DEFAULT_PATH, "rb") as fh:
        return tomllib.load(fh)


def identity(raw: dict | None = None):
    """Build the CPS Identity from config. Contains no credentials."""
    from booking_agent.adapters.cps_golf import Identity

    section = (raw or load())["george_wright"].get("account")
    if not section:
        raise KeyError(
            "config/preferences.toml is missing a [george_wright.account] "
            "section — needed for anything that writes"
        )
    return Identity(
        email=section["email"],
        acct=str(section["acct"]),
        card_id=int(section["card_id"]),
        card_last4=str(section["card_last4"]),
        member_class=section.get("member_class", "NRES"),
        rate_code=section.get("rate_code", "NON-RES"),
    )


def golf_prefs(raw: dict | None = None) -> GolfPrefs:
    section = (raw or load())["george_wright"]
    return GolfPrefs(
        course=section["course"],
        players=section["players"],
        holes=section["holes"],
        no_earlier_than=_parse_time(section["no_earlier_than"]),
        no_later_than=_parse_time(section["no_later_than"]),
        max_attempts=section.get("max_attempts", 3),
    )
