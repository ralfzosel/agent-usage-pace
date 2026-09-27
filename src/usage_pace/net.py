"""Shared retry timing for HTTP providers."""

from email.utils import parsedate_to_datetime

from usage_pace.core import finite_number, utc_now

RATE_LIMIT_BACKOFF = 300.0


def retry_delay(value: str | None) -> float:
    seconds = finite_number(value)
    if seconds is not None:
        return max(RATE_LIMIT_BACKOFF, seconds)
    if value:
        try:
            reset = parsedate_to_datetime(value)
            return max(RATE_LIMIT_BACKOFF, (reset - utc_now()).total_seconds())
        except (TypeError, ValueError, OverflowError):
            pass
    return RATE_LIMIT_BACKOFF
