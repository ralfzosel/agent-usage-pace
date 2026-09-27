"""Cursor subscription usage via its internal dashboard endpoint."""

from __future__ import annotations

import argparse
import calendar
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_usage_pace.core import UsageError, UsageWindow, finite_number, parse_instant
from agent_usage_pace.net import retry_delay

API_URL = "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage"
ACCESS_KEY = "cursorAuth/accessToken"


def cursor_state_db_path() -> Path:
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library/Application Support/Cursor/User/globalStorage/state.vscdb"
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise UsageError("APPDATA is not set.")
        return Path(appdata) / "Cursor/User/globalStorage/state.vscdb"
    return home / ".config/Cursor/User/globalStorage/state.vscdb"


def read_access_token(explicit: str | None = None) -> str:
    for token in (explicit, os.environ.get("CURSOR_ACCESS_TOKEN")):
        if token and token.strip():
            return token.strip()
    path = cursor_state_db_path()
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
            row = connection.execute(
                "SELECT value FROM ItemTable WHERE key = ? LIMIT 1", (ACCESS_KEY,)
            ).fetchone()
    except sqlite3.Error as exc:
        raise UsageError(
            "Could not read Cursor's local login. Sign in to Cursor or set CURSOR_ACCESS_TOKEN."
        ) from exc
    if not row or not row[0]:
        raise UsageError("Cursor has no saved access token. Sign in to Cursor first.")
    return str(row[0])


def add_month(value: datetime) -> datetime:
    year = value.year + (value.month == 12)
    month = value.month % 12 + 1
    return value.replace(
        year=year, month=month, day=min(value.day, calendar.monthrange(year, month)[1])
    )


def billing_cycle(renew_day: int, now: datetime | None = None) -> tuple[datetime, datetime]:
    now = now or datetime.now().astimezone()
    year, month = now.year, now.month
    if now.day < renew_day:
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    start = datetime(year, month, renew_day, tzinfo=now.tzinfo)
    return start, add_month(start)


def parse_billing_instant(value: Any) -> datetime | None:
    number = finite_number(value)
    if number is not None:
        try:
            return datetime.fromtimestamp(number / 1000, timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    return parse_instant(value)


def extract_usage_percent(plan: dict[str, Any]) -> float | None:
    direct = finite_number(plan.get("totalPercentUsed"))
    if direct is not None:
        return max(0, min(100, direct))
    limit = finite_number(plan.get("limit"))
    remaining = finite_number(plan.get("remaining"))
    used = finite_number(plan.get("includedSpend"))
    if used is None and limit is not None and remaining is not None:
        used = max(0, limit - remaining)
    if used is None:
        used = finite_number(plan.get("totalSpend"))
    if used is not None and limit is not None and limit > 0:
        return max(0, min(100, used / limit * 100))
    return None


def parse_windows(payload: dict[str, Any], renew_day: int = 10) -> list[UsageWindow]:
    plan = payload.get("planUsage")
    if not isinstance(plan, dict):
        raise UsageError("Cursor usage response did not include planUsage.")
    percent = extract_usage_percent(plan)
    if percent is None:
        raise UsageError("Could not determine usage percent from Cursor's response.")
    start = parse_billing_instant(payload.get("billingCycleStart"))
    end = parse_billing_instant(payload.get("billingCycleEnd"))
    label = "Billing cycle"
    if start is None and end is None:
        start, end = billing_cycle(renew_day)
        label += f" (estimated from renew day {renew_day})"
    elif start is not None and end is None:
        end = add_month(start)
        label += " (estimated end)"
    start = start.astimezone(timezone.utc) if start else None
    end = end.astimezone(timezone.utc) if end else None
    duration = end - start if start and end and start < end else None
    breakdown = {}
    for key, name in (("autoPercentUsed", "Auto+Composer"), ("apiPercentUsed", "API usage")):
        value = finite_number(plan.get(key))
        if value is not None:
            breakdown[name] = max(0, min(100, value))
    return [UsageWindow("billing_cycle", label, percent, duration, end, start, breakdown)]


def fetch_usage(token: str, timeout: float = 20) -> dict[str, Any]:
    request = urllib.request.Request(
        API_URL,
        data=b"{}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
            "Accept": "application/json",
            "User-Agent": "agent-usage-pace",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code in {401, 403}:
            raise UsageError("Cursor rejected the login. Open Cursor and sign in again.") from exc
        if exc.code == 429:
            delay = retry_delay(exc.headers.get("Retry-After"))
            raise UsageError(
                f"Cursor usage API is rate limited. Retry in {delay:g}s.", delay
            ) from exc
        raise UsageError(f"Cursor usage request failed with HTTP {exc.code}.") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise UsageError(
            "Could not reach Cursor's usage API. Check your network connection."
        ) from exc
    except (ValueError, UnicodeError) as exc:
        raise UsageError("Cursor usage API returned invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise UsageError("Cursor usage response was not an object.")
    return payload


def fetch(args: argparse.Namespace) -> list[UsageWindow]:
    return parse_windows(fetch_usage(read_access_token(args.token), args.timeout), args.renew_day)
