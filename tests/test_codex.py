import json
import queue
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from usage_pace import core, render
from usage_pace.providers import codex as pace

NOW = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)


def row(used=25, minutes=300, reset=None):
    return {
        "usedPercent": used,
        "windowDurationMins": minutes,
        "resetsAt": reset if reset is not None else (NOW + timedelta(minutes=150)).timestamp(),
    }


class ResponseTests(unittest.TestCase):
    def test_multiple_buckets_preferred_over_legacy(self):
        windows = pace.parse_windows(
            {
                "rateLimits": {"primary": row(99)},
                "rateLimitsByLimitId": {
                    "codex": {"primary": row(25), "secondary": row(30, 10080)},
                    "other": {"limitName": "Other model", "primary": row(40, 60)},
                },
            }
        )
        self.assertEqual([w.percent for w in windows], [25, 30, 40])
        self.assertEqual(
            [w.duration for w in windows],
            [timedelta(hours=5), timedelta(days=7), timedelta(hours=1)],
        )
        self.assertEqual(windows[-1].label, "Other model — 1 hour")

    def test_legacy_fallback_and_null_secondary(self):
        for buckets in [None, {}]:
            windows = pace.parse_windows(
                {
                    "rateLimitsByLimitId": buckets,
                    "rateLimits": {
                        "limitId": "codex",
                        "primary": row(0),
                        "secondary": None,
                    },
                }
            )
            self.assertEqual(len(windows), 1)
            self.assertEqual(windows[0].percent, 0)

    def test_missing_or_invalid_usage_is_not_zero(self):
        for value in [None, True, "nan", "inf", -1, 101]:
            with self.subTest(value=value), self.assertRaises(pace.UsageError):
                pace.parse_windows({"rateLimits": {"primary": row(value)}})
        with self.assertRaises(pace.UsageError):
            pace.parse_windows({"rateLimits": {"primary": None, "secondary": None}})

    def test_missing_duration_retains_usage_and_reset(self):
        window = pace.parse_windows({"rateLimits": {"primary": row(minutes=None)}})[0]
        report = core.build_report(window, 2, NOW)
        self.assertEqual(report["status"], "unavailable")
        self.assertEqual(report["usage_percent"], 25)
        self.assertIsNone(report["window_seconds"])
        self.assertIn("Resets", render.render_windows([report]))

    def test_invalid_timing_is_unavailable(self):
        for value in [None, True, float("inf"), 1e100, "bad"]:
            self.assertIsNone(pace.parse_reset(value))
        for minutes in [None, -1, 0, float("inf"), 1e100]:
            window = pace.parse_windows({"rateLimits": {"primary": row(minutes=minutes)}})[0]
            self.assertIsNone(core.build_report(window, 2, NOW)["expected_percent"])

    def test_labels_cannot_inject_terminal_commands(self):
        window = pace.parse_windows(
            {"rateLimits": {"limitName": "Model\n\x1b[2J", "primary": row()}}
        )[0]
        self.assertNotIn("\x1b", window.label)
        self.assertNotIn("\n", window.label)


class ProtocolTests(unittest.TestCase):
    def test_notifications_are_skipped(self):
        messages = queue.Queue()
        messages.put(json.dumps({"method": "account/updated", "params": {}}))
        messages.put(json.dumps({"id": 99, "result": {}}))
        messages.put(json.dumps({"id": 2, "result": {"rateLimits": {}}}))
        self.assertEqual(pace.rpc_result(messages, 2, None, 1), {"rateLimits": {}})

    def test_protocol_errors_and_eof(self):
        for message in [
            None,
            "bad JSON",
            "[]",
            '{"id":2,"result":null}',
            '{"id":2,"error":{"message":"secret-token"}}',
        ]:
            messages = queue.Queue()
            messages.put(message)
            with self.subTest(message=message), self.assertRaises(pace.UsageError) as error:
                pace.rpc_result(messages, 2, None, 1)
            self.assertNotIn("secret-token", str(error.exception))

    def test_request_timeout(self):
        with self.assertRaisesRegex(pace.UsageError, "timed out"):
            pace.rpc_result(queue.Queue(), 2, None, 0.01)

    def test_missing_cli_is_actionable(self):
        with self.assertRaisesRegex(pace.UsageError, "Install the Codex CLI"):
            pace.fetch_usage("/nonexistent/codex")

    def test_real_subprocess_handshake_and_cleanup(self):
        # Exercise actual pipes, process lifetime, and message order without a login or network.
        fake = """import json, sys, time
def receive():
    return json.loads(sys.stdin.readline())
def send(message):
    print(json.dumps(message), flush=True)
initial = receive()
assert initial["method"] == "initialize"
send({"id": initial["id"], "result": {"userAgent": "test"}})
assert receive()["method"] == "initialized"
request = receive()
assert request["method"] == "account/rateLimits/read"
send({"method": "account/rateLimits/updated", "params": {}})
send({"id": request["id"], "result": {"rateLimits": {"primary": {"usedPercent": 25}}}})
time.sleep(60)
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fake_server.py"
            path.write_text(fake)
            popen = subprocess.Popen
            processes = []

            def start(command, **kwargs):
                self.assertEqual(command, ["codex", "app-server"])
                process = popen([sys.executable, str(path)], **kwargs)
                processes.append(process)
                return process

            with patch.object(pace.subprocess, "Popen", side_effect=start):
                result = pace.fetch_usage(timeout=2)
            self.assertEqual(result["rateLimits"]["primary"]["usedPercent"], 25)
            self.assertIsNotNone(processes[0].poll())
