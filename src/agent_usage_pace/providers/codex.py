"""Codex subscription usage through the local app-server protocol."""

from __future__ import annotations

import argparse
import contextlib
import json
import queue
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from agent_usage_pace import __version__
from agent_usage_pace.core import UsageError, UsageWindow, finite_number


def rpc_result(
    messages: queue.Queue, request_id: int, process: subprocess.Popen, timeout: float
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise UsageError(
                "Codex usage request timed out. Check your connection and Codex login."
            )
        try:
            line = messages.get(timeout=remaining)
        except queue.Empty as exc:
            raise UsageError(
                "Codex usage request timed out. Check your connection and Codex login."
            ) from exc
        if line is None:
            raise UsageError(
                "Codex app server exited unexpectedly. Check `codex app-server --help` and your Codex configuration."
            )
        try:
            message = json.loads(line)
        except (ValueError, UnicodeError) as exc:
            raise UsageError("Codex app server returned invalid JSON.") from exc
        if not isinstance(message, dict):
            raise UsageError("Codex app server returned an invalid response.")
        if "method" in message:
            if "id" in message:
                send_rpc(
                    process,
                    {
                        "id": message["id"],
                        "error": {
                            "code": -32601,
                            "message": "This usage reader does not handle server requests.",
                        },
                    },
                )
            continue
        if message.get("id") != request_id:
            continue
        if "error" in message:
            # Raw server errors can contain account details; keep diagnostics local.
            raise UsageError(
                "Codex could not read subscription usage. Run `codex login` with ChatGPT, "
                "then retry. Also check your connection; API-key logins have no subscription limits."
            )
        result = message.get("result")
        if not isinstance(result, dict):
            raise UsageError("Codex app server returned an invalid result.")
        return result


def send_rpc(process: subprocess.Popen, message: dict[str, Any]) -> None:
    process.stdin.write((json.dumps(message) + "\n").encode())
    process.stdin.flush()


def read_lines(stream: Any, messages: queue.Queue) -> None:
    try:
        for line in stream:
            messages.put(line)
    except OSError:
        pass
    finally:
        messages.put(None)


def fetch_usage(codex: str = "codex", timeout: float = 20) -> dict[str, Any]:
    try:
        process = subprocess.Popen(
            [codex, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise UsageError(
            "Could not start Codex. Install the Codex CLI or pass --codex PATH."
        ) from exc
    messages: queue.Queue = queue.Queue()
    reader = threading.Thread(target=read_lines, args=(process.stdout, messages), daemon=True)
    reader.start()
    try:
        send_rpc(
            process,
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "agent-usage-pace", "version": __version__},
                },
            },
        )
        rpc_result(messages, 1, process, timeout)
        send_rpc(process, {"method": "initialized"})
        send_rpc(process, {"id": 2, "method": "account/rateLimits/read"})
        return rpc_result(messages, 2, process, timeout)
    except (OSError, ValueError) as exc:
        raise UsageError("Could not communicate with the Codex app server.") from exc
    finally:
        # This private helper never starts a chat or inference turn.
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        reader.join(timeout=2)
        with contextlib.suppress(OSError):
            process.stdin.close()
        with contextlib.suppress(OSError):
            process.stdout.close()


def parse_reset(value: Any) -> datetime | None:
    seconds = finite_number(value)
    if seconds is None:
        return None
    try:
        return datetime.fromtimestamp(seconds, timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def duration_label(duration: timedelta | None) -> str:
    if duration is None:
        return "unknown window"
    minutes = duration.total_seconds() / 60
    if minutes % 1440 == 0:
        amount, unit = minutes / 1440, "day"
    elif minutes % 60 == 0:
        amount, unit = minutes / 60, "hour"
    else:
        amount, unit = minutes, "minute"
    return f"{amount:g} {unit}{'' if amount == 1 else 's'}"


def parse_windows(payload: dict[str, Any]) -> list[UsageWindow]:
    windows: list[UsageWindow] = []
    buckets = payload.get("rateLimitsByLimitId")
    if not isinstance(buckets, dict) or not buckets:
        legacy = payload.get("rateLimits")
        buckets = {legacy.get("limitId") or "codex": legacy} if isinstance(legacy, dict) else {}
    for bucket_id, bucket in buckets.items():
        if not isinstance(bucket, dict):
            continue
        name = bucket.get("limitName") or bucket_id
        name = "".join(c for c in str(name) if c.isprintable())[:100]
        if name == "codex":
            name = "Codex"
        for key in ("primary", "secondary"):
            row = bucket.get(key)
            if not isinstance(row, dict):
                continue
            percent = finite_number(row.get("usedPercent"))
            if percent is None or not 0 <= percent <= 100:
                continue
            minutes = finite_number(row.get("windowDurationMins"))
            duration = None
            if minutes is not None and minutes > 0:
                try:
                    duration = timedelta(minutes=minutes)
                except OverflowError:
                    pass
            label = f"{name} — {duration_label(duration)}"
            windows.append(
                UsageWindow(
                    f"{bucket_id}/{key}", label, percent, duration, parse_reset(row.get("resetsAt"))
                )
            )
    if not windows:
        raise UsageError(
            "Codex returned no subscription usage windows. "
            "Sign in with ChatGPT using `codex login`; missing limits are not zero usage."
        )
    return windows


def fetch(args: argparse.Namespace) -> list[UsageWindow]:
    return parse_windows(fetch_usage(args.codex, args.timeout))
