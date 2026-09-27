"""Command-line entry points and independent provider refresh scheduling."""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from usage_pace import __version__
from usage_pace.core import (
    DEFAULT_INTERVAL,
    DEFAULT_TOLERANCE,
    WINDOWS,
    UsageError,
    UsageWindow,
    build_report,
    parse_instant,
    utc_now,
)
from usage_pace.providers import claude, codex, cursor
from usage_pace.render import render_windows
from usage_pace.terminal import wait_for_key_or_timeout

PROVIDERS = {"cursor": cursor, "claude": claude, "codex": codex}
TITLES = {"cursor": "Cursor", "claude": "Claude (shared with Claude apps)", "codex": "Codex"}


@dataclass
class ProviderState:
    windows: list[UsageWindow] = field(default_factory=list)
    fetched_at: datetime | None = None
    error: str | None = None
    retry_at: float = 0


def collect(states: dict[str, ProviderState], args: argparse.Namespace) -> None:
    due = [name for name, state in states.items() if state.retry_at <= time.monotonic()]
    if not due:
        return
    with ThreadPoolExecutor(max_workers=len(due)) as executor:
        futures = {executor.submit(PROVIDERS[name].fetch, args): name for name in due}
        for future in as_completed(futures):
            state = states[futures[future]]
            delay = args.interval
            try:
                state.windows = future.result()
                state.fetched_at = utc_now()
                state.error = None
            except UsageError as exc:
                state.error = str(exc)
                delay = max(delay, exc.retry_after)
            except Exception as exc:
                # Keep a corrupt local file or changed provider response isolated.
                # Exception text may contain account details, so expose only its type.
                state.error = f"Could not read usage ({type(exc).__name__}); check this provider's login and configuration."
            state.retry_at = time.monotonic() + delay


def make_snapshot(states: dict[str, ProviderState], args: argparse.Namespace) -> dict[str, Any]:
    now = utc_now()
    return {
        "schema_version": 1,
        "generated_at": now.isoformat(),
        "providers": {
            name: {
                "fetched_at": state.fetched_at.isoformat() if state.fetched_at else None,
                "source": "manual" if args.usage is not None else name,
                "error": state.error,
                "stale": bool(state.error and state.windows),
                "retry_in_seconds": round(max(0, state.retry_at - time.monotonic()), 1),
                "windows": [build_report(window, args.tolerance, now) for window in state.windows],
            }
            for name, state in states.items()
        },
    }


def render_human(data: dict[str, Any]) -> str:
    sections = []
    for name, result in data["providers"].items():
        lines = [TITLES[name]]
        if result["error"]:
            lines.append(f"error: {result['error']}")
            lines.append(f"Retrying in {result['retry_in_seconds']:g}s")
        if result["stale"]:
            lines.append(f"Showing last-known usage from {result['fetched_at']}")
        if result["windows"]:
            lines.append(render_windows(result["windows"]))
        sections.append("\n\n".join(lines))
    return "\n\n".join(sections)


def run_live(states: dict[str, ProviderState], args: argparse.Namespace) -> None:
    sys.stdout.write("\x1b[?1049h\x1b[?25l")
    try:
        while True:
            collect(states, args)
            sys.stdout.write(
                f"\x1b[H\x1b[2J{render_human(make_snapshot(states, args))}\n\n"
                f"Refreshing every {args.interval:g}s — press any key to quit "
                f"(updated {datetime.now():%H:%M:%S})"
            )
            sys.stdout.flush()
            delay = max(0.05, min(state.retry_at for state in states.values()) - time.monotonic())
            if wait_for_key_or_timeout(delay):
                break
    finally:
        sys.stdout.write("\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare coding-agent subscription usage with time elapsed."
    )
    parser.add_argument(
        "provider", nargs="?", choices=PROVIDERS, help="provider to show (default: all)"
    )
    parser.add_argument("--all", action="store_true", help="show all providers together")
    parser.add_argument("--version", action="version", version=f"agent-usage-pace {__version__}")
    parser.add_argument(
        "--once", action="store_true", help="print one snapshot (default: live in a TTY)"
    )
    parser.add_argument("--json", action="store_true", help="print one machine-readable snapshot")
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL,
        help=f"live refresh seconds (default: {DEFAULT_INTERVAL:g})",
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=DEFAULT_TOLERANCE,
        help="on-pace band in percentage points (default: 2)",
    )
    parser.add_argument(
        "--timeout", type=float, default=20, help="network/app-server timeout seconds (default: 20)"
    )
    auth = parser.add_argument_group("provider options")
    auth.add_argument("--token", help="explicit token for a single Cursor or Claude provider")
    auth.add_argument("--credentials-file", type=Path, help="Claude Code credentials JSON file")
    auth.add_argument("--codex", default="codex", help="Codex CLI executable")
    auth.add_argument(
        "--renew-day",
        type=int,
        default=10,
        help="Cursor fallback billing-cycle day, 1–28 (default: 10)",
    )
    manual = parser.add_argument_group("offline snapshot")
    manual.add_argument("--usage", type=float, help="manual usage percent for one provider")
    manual.add_argument(
        "--window",
        choices=WINDOWS,
        default="seven_day",
        help="manual Claude/Codex window (default: seven_day)",
    )
    manual.add_argument("--resets-at", help="manual reset time in ISO 8601, including timezone")
    args = parser.parse_args(argv)
    if args.provider and args.all:
        parser.error("choose a provider or --all, not both")
    for name in ("interval", "timeout"):
        if not math.isfinite(getattr(args, name)) or getattr(args, name) <= 0:
            parser.error(f"--{name} must be finite and positive")
    if not math.isfinite(args.tolerance) or not 0 <= args.tolerance <= 100:
        parser.error("--tolerance must be between 0 and 100")
    if not 1 <= args.renew_day <= 28:
        parser.error("--renew-day must be between 1 and 28")
    if args.token and args.provider not in {"cursor", "claude"}:
        parser.error("--token requires a single cursor or claude provider")
    if args.credentials_file and args.provider != "claude":
        parser.error("--credentials-file requires the claude provider")
    if args.usage is not None:
        if not args.provider:
            parser.error("--usage requires a single provider")
        if not math.isfinite(args.usage) or not 0 <= args.usage <= 100:
            parser.error("--usage must be between 0 and 100")
        if args.resets_at is not None:
            args.resets_at = parse_instant(args.resets_at)
            if args.resets_at is None:
                parser.error("--resets-at must be an ISO 8601 timestamp including timezone")
        if args.provider != "cursor" and args.resets_at is None:
            parser.error("--usage requires --resets-at for Claude and Codex")
    elif args.resets_at is not None:
        parser.error("--resets-at requires --usage")
    return args


def manual_windows(args: argparse.Namespace) -> list[UsageWindow]:
    if args.provider == "cursor":
        start, end = cursor.billing_cycle(args.renew_day)
        end = args.resets_at or end
        start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
        return [
            UsageWindow(
                "billing_cycle", "Billing cycle (manual)", args.usage, end - start, end, start
            )
        ]
    label, duration = WINDOWS[args.window]
    return [UsageWindow(args.window, label, args.usage, duration, args.resets_at)]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    names = [args.provider] if args.provider else list(PROVIDERS)
    states = {name: ProviderState() for name in names}
    try:
        if args.usage is not None:
            states[args.provider] = ProviderState(
                windows=manual_windows(args), fetched_at=utc_now()
            )
        elif not (args.once or args.json or not sys.stdout.isatty() or not sys.stdin.isatty()):
            run_live(states, args)
            return 0
        else:
            collect(states, args)
        data = make_snapshot(states, args)
        print(json.dumps(data, indent=2, allow_nan=False) if args.json else render_human(data))
        return int(any(state.error for state in states.values()))
    except KeyboardInterrupt:
        return 130


def cursor_main() -> int:
    return main(["cursor", *sys.argv[1:]])


def claude_main() -> int:
    return main(["claude", *sys.argv[1:]])


def codex_main() -> int:
    return main(["codex", *sys.argv[1:]])
