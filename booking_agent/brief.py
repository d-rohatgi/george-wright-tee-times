"""Phase B — the cold path, where the model lives.

Runs *after* the booking is settled. Its job is to turn a structured Outcome
plus calendar and weather context into two sentences worth reading on a phone.
If the model is slow, wrong, or not running at all, you still got your tee
time and you still get a notification — this layer degrades to a template.

Talks to the OpenAI-compatible endpoint, so Ollama / LM Studio / a hosted
model are all the same code path; only BOOKING_AGENT_MODEL_URL changes.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from booking_agent.models import Outcome

MODEL_URL = os.environ.get("BOOKING_AGENT_MODEL_URL", "http://localhost:11434/v1")
MODEL_NAME = os.environ.get("BOOKING_AGENT_MODEL", "qwen3:8b")
TIMEOUT_S = float(os.environ.get("BOOKING_AGENT_MODEL_TIMEOUT", "25"))

_SYSTEM = """You write one-paragraph booking notifications for a phone screen.

Rules:
- 2 sentences maximum. No preamble, no sign-off, no bullet points.
- State what happened first, then the weather caveat only if the forecast is
  bad enough to be worth reconsidering. Skip it otherwise.
- If nothing was booked, say so plainly and give the reason. Do not apologise
  and do not suggest alternatives that were not in the data.
- Never invent a tee time, a price, or a forecast that is not in the input."""


def compose(outcome: Outcome, *, weather: dict | None = None) -> str:
    """Model-written summary, with a deterministic fallback."""
    payload = {
        "status": outcome.status.value,
        "target_date": outcome.target_date.isoformat(),
        "booked": (
            {
                "tee_time": outcome.booking.slot.tee_time.isoformat(),
                "course": outcome.booking.slot.course,
                "players": outcome.booking.players,
                "holes": outcome.booking.slot.holes,
                "price_usd": outcome.booking.slot.price_usd,
            }
            if outcome.booking
            else None
        ),
        "slots_considered": outcome.considered,
        "attempts": outcome.attempted,
        "error": outcome.error,
        "forecast": weather or {},
    }

    try:
        return _chat(json.dumps(payload, indent=2))
    except Exception as exc:  # noqa: BLE001 — never let the brief break the run
        return _fallback(outcome, weather, reason=str(exc))


def _chat(user_content: str) -> str:
    body = json.dumps(
        {
            "model": MODEL_NAME,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": user_content},
            ],
            "temperature": 0.2,
            "stream": False,
        }
    ).encode()

    req = urllib.request.Request(
        f"{MODEL_URL.rstrip('/')}/chat/completions",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        data = json.loads(resp.read())

    text = data["choices"][0]["message"]["content"].strip()
    # Small local models often emit a <think> block first; keep only the answer.
    if "</think>" in text:
        text = text.split("</think>", 1)[1].strip()
    if not text:
        raise ValueError("model returned an empty summary")
    return text


def _fallback(outcome: Outcome, weather: dict | None, *, reason: str) -> str:
    parts = [outcome.headline() + "."]
    if weather and weather.get("summary"):
        temp = f", {weather['temp_f']}°F" if weather.get("temp_f") else ""
        pop = weather.get("precip_pct")
        rain = f", {pop}% precip" if pop else ""
        parts.append(f"Forecast: {weather['summary']}{temp}{rain}.")
    parts.append(f"(Model summary unavailable: {reason})")
    return " ".join(parts)
