"""Per-model local buckets and server-observed limits; no sleeping here."""

from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
import math
import re

from app.config import Settings


def duration_seconds(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        number = float(value)
        return max(0.0, number) if math.isfinite(number) else 0.0
    except ValueError:
        pass
    parts = re.findall(r"(\d+(?:\.\d+)?)(ms|s|m|h|d)", value)
    if not parts or "".join(a + b for a, b in parts) != value:
        return 0.0
    return sum(float(number) * {"ms": .001, "s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
               for number, unit in parts)


def retry_after_seconds(value: str | None, now: float) -> float:
    if not value:
        return 0.0
    parsed = duration_seconds(value)
    if parsed:
        return parsed
    try:
        target = parsedate_to_datetime(value)
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        return max(0.0, target.timestamp() - now)
    except (TypeError, ValueError, OverflowError):
        return 0.0


@dataclass
class Bucket:
    capacity: float
    remaining: float
    updated: float

    def refill(self, now: float) -> None:
        self.remaining = min(self.capacity, self.remaining + max(0, now - self.updated) * self.capacity / 60)
        self.updated = now

    def delay(self, amount: int, now: float) -> float:
        self.refill(now)
        return max(0.0, (amount - self.remaining) * 60 / self.capacity)


class ModelLimiter:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.models: dict[str, dict] = {}

    def _state(self, model: str, now: float) -> dict:
        if model not in self.models:
            self.models[model] = {
                "requests": Bucket(self.settings.llm_requests_per_minute, self.settings.llm_requests_per_minute, now),
                "tokens": Bucket(self.settings.llm_tokens_per_minute, self.settings.llm_tokens_per_minute, now),
                "blocked_until": 0.0, "daily_remaining": None, "daily_reset": 0.0,
                "token_reset": 0.0,
            }
        return self.models[model]

    def delay(self, model: str, tokens: int, now: float) -> float:
        state = self._state(model, now)
        if tokens > state["tokens"].capacity:
            return float("inf")
        if state["daily_reset"] and now >= state["daily_reset"]:
            state["daily_remaining"] = None
            state["daily_reset"] = 0
        delays = [state["blocked_until"] - now, state["requests"].delay(1, now), state["tokens"].delay(tokens, now)]
        if state["daily_remaining"] is not None and state["daily_remaining"] < 1:
            delays.append(max(1.0, state["daily_reset"] - now))
        return max(0.0, *delays)

    def consume(self, model: str, tokens: int, now: float) -> None:
        state = self._state(model, now)
        state["requests"].refill(now)
        state["tokens"].refill(now)
        state["requests"].remaining -= 1
        state["tokens"].remaining -= tokens
        if state["daily_remaining"] is not None:
            state["daily_remaining"] -= 1

    def observe(self, model: str, headers: dict, now: float) -> None:
        state = self._state(model, now)
        headers = {key.lower(): value for key, value in headers.items()}
        def number(key):
            try:
                value = float(headers[key])
                return value if math.isfinite(value) and value >= 0 else None
            except (KeyError, TypeError, ValueError):
                return None
        capacity = number("x-ratelimit-limit-tokens")
        if capacity and capacity > 0:
            state["tokens"].capacity = min(capacity, self.settings.llm_tokens_per_minute)
        remaining = number("x-ratelimit-remaining-tokens")
        if remaining is not None:
            state["tokens"].remaining = min(remaining, state["tokens"].capacity)
            state["tokens"].updated = now
            reset = duration_seconds(headers.get("x-ratelimit-reset-tokens"))
            state["token_reset"] = now + reset
            if remaining == 0 and reset:
                state["blocked_until"] = max(state["blocked_until"], now + reset)
        remaining = number("x-ratelimit-remaining-requests")
        if remaining is not None:
            state["daily_remaining"] = remaining
            # These are RPD headers, not RPM. Missing reset fails conservatively.
            reset = duration_seconds(headers.get("x-ratelimit-reset-requests"))
            state["daily_reset"] = now + (reset or 86400)

    def block(self, model: str, until: float, now: float) -> None:
        state = self._state(model, now)
        state["blocked_until"] = max(state["blocked_until"], until)

    def snapshot(self, now: float) -> dict:
        return {model: {"retry_in_seconds": max(0.0, state["blocked_until"] - now),
                        "daily_requests_remaining": state["daily_remaining"],
                        "daily_reset_in_seconds": max(0.0, state["daily_reset"] - now)}
                for model, state in self.models.copy().items()}
