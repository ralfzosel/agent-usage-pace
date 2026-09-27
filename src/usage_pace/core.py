"""Provider-independent usage windows and pace calculations."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

DEFAULT_INTERVAL = 20.0
DEFAULT_TOLERANCE = 2.0
WINDOWS = {
    "five_hour": ("Current session (5 hours)", timedelta(hours=5)),
    "seven_day": ("Current week", timedelta(days=7)),
}


class UsageError(RuntimeError):
    def __init__(self, message: str, retry_after: float = 0):
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class UsageWindow:
    key: str
    label: str
    percent: float
    duration: timedelta | None
    resets_at: datetime | None
    start: datetime | None = None
    breakdown: dict[str, float] = field(default_factory=dict)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def parse_instant(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    # API resets must identify an instant; never guess a timezone.
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def build_report(window: UsageWindow, tolerance: float, now: datetime) -> dict[str, Any]:
    end = window.resets_at
    start = window.start
    if start is None and end and window.duration:
        try:
            start = end - window.duration
        except OverflowError:
            pass
    expected = delta = projected = None
    remaining = max(0.0, (end - now).total_seconds()) if end else None
    status = "unavailable"
    reason = "Reset time or window length is unavailable; pace cannot be calculated."
    if start and end and start < end:
        if start <= now < end:
            fraction = (now - start).total_seconds() / (end - start).total_seconds()
            expected = fraction * 100
            delta = window.percent - expected
            status = "on track" if abs(delta) <= tolerance else "below" if delta < 0 else "above"
            # Keep projections above 100% visible: they signal likely exhaustion.
            projected = window.percent / fraction if fraction > 0 else None
            reason = None
        elif now >= end:
            reason = "Reported reset has passed; waiting for updated usage."
        else:
            reason = "Reset time is outside the expected window; pace cannot be calculated."
    return {
        "key": window.key,
        "label": window.label,
        "breakdown": window.breakdown,
        "usage_percent": window.percent,
        "window_start": start.isoformat() if start else None,
        "resets_at": end.isoformat() if end else None,
        "window_seconds": window.duration.total_seconds() if window.duration else None,
        "seconds_remaining": remaining,
        "expected_percent": expected,
        "delta_percent": delta,
        "projected_percent": projected,
        "tolerance_pp": tolerance,
        "status": status,
        "pace_note": reason,
    }
