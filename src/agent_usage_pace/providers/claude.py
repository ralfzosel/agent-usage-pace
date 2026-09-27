"""Claude subscription usage via its internal OAuth usage endpoint."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import timedelta
from pathlib import Path
from typing import Any

from agent_usage_pace.core import WINDOWS, UsageError, UsageWindow, finite_number, parse_instant
from agent_usage_pace.net import retry_delay

API_URL = "https://api.anthropic.com/api/oauth/usage"
LOGIN_HINT = "Run `claude auth login` with your Claude subscription, then retry."


def token_from_credentials(raw: str) -> str:
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise UsageError("Could not parse Claude Code credentials. " + LOGIN_HINT) from exc
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    token = oauth.get("accessToken") if isinstance(oauth, dict) else None
    if not isinstance(token, str) or not token.strip():
        raise UsageError("Claude Code has no saved subscription access token. " + LOGIN_HINT)
    expires = finite_number(oauth.get("expiresAt"))
    if expires is not None and expires <= time.time() * 1000:
        raise UsageError(
            "Claude Code's access token has expired. Open Claude Code to refresh "
            "its login, or run `claude auth login`, then retry."
        )
    return token.strip()


def read_access_token(args: argparse.Namespace) -> str:
    for candidate in (args.token, os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")):
        if candidate and candidate.strip():
            return candidate.strip()
    if args.credentials_file:
        try:
            return token_from_credentials(args.credentials_file.expanduser().read_text())
        except OSError as exc:
            raise UsageError("Could not read the specified Claude credentials file.") from exc

    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise UsageError("Could not read Claude Code's macOS Keychain login.") from exc
        if result.returncode == 0 and result.stdout.strip():
            return token_from_credentials(result.stdout)

    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    try:
        return token_from_credentials((config_dir / ".credentials.json").read_text())
    except OSError as exc:
        raise UsageError("Claude Code login not found. " + LOGIN_HINT) from exc


def fetch_usage(token: str, timeout: float = 20) -> dict[str, Any]:
    request = urllib.request.Request(
        API_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "Accept": "application/json",
            "User-Agent": "agent-usage-pace",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code in {401, 403}:
            raise UsageError(
                "Claude rejected the login or its usage permissions. "
                + LOGIN_HINT
                + " A token needs user:profile scope; an API key cannot read subscription usage."
            ) from exc
        if exc.code == 429:
            delay = retry_delay(exc.headers.get("Retry-After"))
            raise UsageError("Claude usage API is rate limited.", delay) from exc
        raise UsageError(f"Claude usage request failed with HTTP {exc.code}.") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise UsageError(
            "Could not reach Claude's usage API. Check your network connection."
        ) from exc
    except (ValueError, UnicodeError) as exc:
        raise UsageError("Claude usage API returned invalid JSON.") from exc
    if not isinstance(payload, dict):
        raise UsageError("Claude usage response was not an object.")
    return payload


def parse_windows(payload: dict[str, Any]) -> list[UsageWindow]:
    windows: list[UsageWindow] = []
    covered: set[str] = set()
    # Current clients receive server-defined limits; older responses use named fields.
    limits = payload.get("limits")
    if isinstance(limits, list):
        for index, row in enumerate(limits):
            if not isinstance(row, dict):
                continue
            kind = row.get("kind")
            if kind == "session":
                key = "five_hour"
                label, duration = WINDOWS[key]
            elif kind == "weekly_all":
                key = "seven_day"
                label, duration = WINDOWS[key]
            elif kind == "weekly_scoped":
                key = f"weekly_scoped_{index}"
                duration = timedelta(days=7)
                scope = row.get("scope")
                names = []
                if isinstance(scope, dict):
                    for scope_type in ("model", "surface"):
                        detail = scope.get(scope_type)
                        if isinstance(detail, dict) and isinstance(detail.get("display_name"), str):
                            names.append(detail["display_name"])
                # Do not let remote labels inject terminal control sequences.
                name = " / ".join(names) or "scoped limit"
                name = "".join(c for c in name if c.isprintable())[:100]
                label = f"Current week ({name})"
            else:
                continue
            percent = finite_number(row.get("percent"))
            if percent is not None and 0 <= percent <= 100:
                windows.append(
                    UsageWindow(key, label, percent, duration, parse_instant(row.get("resets_at")))
                )
                covered.add(key)

    for key, row in payload.items():
        if key in covered or not isinstance(row, dict):
            continue
        if key in WINDOWS:
            label, duration = WINDOWS[key]
        elif key.startswith("seven_day_"):
            # Modern scoped rows replace the legacy per-model counters.
            if any(w.key.startswith("weekly_scoped_") for w in windows):
                continue
            label = f"Current week ({key[len('seven_day_') :].replace('_', ' ').title()})"
            duration = timedelta(days=7)
        else:
            continue
        percent = finite_number(row.get("utilization"))
        if percent is not None and 0 <= percent <= 100:
            windows.append(
                UsageWindow(key, label, percent, duration, parse_instant(row.get("resets_at")))
            )
    if not windows:
        raise UsageError("Claude returned no supported subscription usage windows.")
    return windows


def fetch(args: argparse.Namespace) -> list[UsageWindow]:
    return parse_windows(fetch_usage(read_access_token(args), args.timeout))
