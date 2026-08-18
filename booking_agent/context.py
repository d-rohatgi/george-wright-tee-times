"""check_weather — context tool for Phase B.

Deliberately *not* consulted by the booking decision. A Saturday forecast read
on Tuesday is inside the NWS 7-day window but still low-confidence, and it is
not worth forfeiting a tee time over at 07:00:00 while you are asleep. Book
first, report the forecast, then re-check on Friday (`cli.py review`) when the
number actually means something.

api.weather.gov is free, keyless, and rate-limits politely. It only asks for a
User-Agent that identifies you.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

# George Wright Golf Course, Hyde Park MA. Verify against the scorecard if you
# care about grid precision — NWS grids are ~2.5km so this is plenty close.
LAT, LON = 42.2625, -71.1122

USER_AGENT = os.environ.get(
    "BOOKING_AGENT_UA", "booking-agent (personal use; contact via github)"
)
CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "weather_cache.json"
CACHE_TTL = timedelta(hours=3)


def check_weather(day: date) -> dict:
    """Forecast for `day`.

    Returns {} when the day is outside the forecast window or the service is
    unreachable — an empty dict means 'no information', never 'good weather'.
    """
    try:
        periods = _forecast_periods()
    except Exception:  # noqa: BLE001 — context is a nice-to-have, never fatal
        return {}

    daytime = [
        p
        for p in periods
        if p.get("isDaytime")
        and datetime.fromisoformat(p["startTime"]).date() == day
    ]
    if not daytime:
        return {}

    p = daytime[0]
    pop = (p.get("probabilityOfPrecipitation") or {}).get("value")
    return {
        "summary": p.get("shortForecast", ""),
        "temp_f": p.get("temperature"),
        "precip_pct": pop,
        "wind": p.get("windSpeed", ""),
        "playable": _playable(p.get("shortForecast", ""), pop),
    }


def _playable(summary: str, pop: int | None) -> bool:
    """Crude, and intentionally so — the model gets the raw fields too and can
    say something smarter. This is just a fast boolean for the notification."""
    bad = ("thunderstorm", "heavy rain", "snow", "sleet", "ice")
    if any(term in summary.lower() for term in bad):
        return False
    return pop is None or pop < 60


def _forecast_periods() -> list[dict]:
    cached = _read_cache()
    if cached is not None:
        return cached

    point = _get(f"https://api.weather.gov/points/{LAT},{LON}")
    forecast_url = point["properties"]["forecast"]
    periods = _get(forecast_url)["properties"]["periods"]
    _write_cache(periods)
    return periods


def _get(url: str) -> dict:
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _read_cache() -> list[dict] | None:
    try:
        blob = json.loads(CACHE_PATH.read_text())
        if datetime.fromisoformat(blob["fetched_at"]) + CACHE_TTL > datetime.now():
            return blob["periods"]
    except (OSError, ValueError, KeyError):
        pass
    return None


def _write_cache(periods: list[dict]) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(
            json.dumps(
                {"fetched_at": datetime.now().isoformat(), "periods": periods}
            )
        )
    except OSError:
        pass
