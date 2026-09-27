import io
import json
import os
import unittest
import urllib.error
from datetime import datetime, timezone
from unittest.mock import patch

from agent_usage_pace import cli, core, net
from agent_usage_pace.providers import claude as pace

NOW = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)


def parse_args(argv):
    return cli.parse_args(["claude", *argv])


class ResponseTests(unittest.TestCase):
    def test_legacy_windows_and_null_model(self):
        windows = pace.parse_windows(
            {
                "five_hour": {"utilization": 0, "resets_at": None},
                "seven_day": {"utilization": 25, "resets_at": "2026-10-01T12:00:00Z"},
                "seven_day_opus": None,
                "seven_day_sonnet": {"utilization": 4, "resets_at": None},
                "extra_usage": {"is_enabled": True, "utilization": 30},
            }
        )
        self.assertEqual([w.key for w in windows], ["five_hour", "seven_day", "seven_day_sonnet"])

    def test_modern_rows_override_legacy_duplicates(self):
        windows = pace.parse_windows(
            {
                "limits": [
                    {"kind": "session", "percent": 10, "resets_at": None},
                    {"kind": "weekly_all", "percent": 20, "resets_at": None},
                    {
                        "kind": "weekly_scoped",
                        "percent": 30,
                        "scope": {"model": {"display_name": "Sonnet"}},
                    },
                    {"kind": "unknown", "percent": 40},
                ],
                "five_hour": {"utilization": 99},
                "seven_day": {"utilization": 99},
                "seven_day_sonnet": {"utilization": 99},
            }
        )
        self.assertEqual([w.percent for w in windows], [10, 20, 30])
        self.assertEqual(windows[-1].label, "Current week (Sonnet)")

    def test_missing_modern_session_falls_back_to_legacy(self):
        windows = pace.parse_windows(
            {
                "limits": [{"kind": "weekly_all", "percent": 20}],
                "five_hour": {"utilization": 10},
            }
        )
        self.assertEqual({w.key for w in windows}, {"five_hour", "seven_day"})

    def test_bad_values_are_not_reported_as_zero(self):
        for value in [True, "nan", float("inf"), -1, 101, {}, None]:
            with self.subTest(value=value), self.assertRaises(pace.UsageError):
                pace.parse_windows({"five_hour": {"utilization": value}})

    def test_invalid_reset_never_invents_a_timezone(self):
        for value in ["2026-09-27T12:00:00", "bad", 42, None]:
            self.assertIsNone(core.parse_instant(value))

    def test_remote_control_characters_are_removed(self):
        windows = pace.parse_windows(
            {
                "limits": [
                    {
                        "kind": "weekly_scoped",
                        "percent": 10,
                        "scope": {"model": {"display_name": "Sonnet\n\x1b[2J"}},
                    }
                ]
            }
        )
        self.assertNotIn("\x1b", windows[0].label)
        self.assertNotIn("\n", windows[0].label)


class AuthAndHttpTests(unittest.TestCase):
    def test_credentials_and_expiry(self):
        raw = json.dumps({"claudeAiOauth": {"accessToken": "example", "expiresAt": 2000}})
        with patch.object(pace.time, "time", return_value=1):
            self.assertEqual(pace.token_from_credentials(raw), "example")
        with (
            patch.object(pace.time, "time", return_value=2),
            self.assertRaisesRegex(pace.UsageError, "expired"),
        ):
            pace.token_from_credentials(raw)

    def test_missing_or_malformed_credentials(self):
        for raw in ["not JSON", "[]", "{}", '{"claudeAiOauth":{"accessToken":""}}']:
            with self.subTest(raw=raw), self.assertRaises(pace.UsageError):
                pace.token_from_credentials(raw)

    def test_env_and_explicit_token_precedence(self):
        with patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "env-example"}):
            self.assertEqual(pace.read_access_token(parse_args([])), "env-example")
            self.assertEqual(
                pace.read_access_token(parse_args(["--token", "explicit"])), "explicit"
            )

    def test_request_and_invalid_response(self):
        with patch.object(
            pace.urllib.request, "urlopen", return_value=io.StringIO("{}")
        ) as request:
            self.assertEqual(pace.fetch_usage("example"), {})
            sent = request.call_args.args[0]
            self.assertEqual(sent.full_url, pace.API_URL)
            self.assertEqual(sent.get_header("Authorization"), "Bearer example")
            self.assertEqual(sent.get_header("Anthropic-beta"), "oauth-2025-04-20")
        for raw in ["not JSON", "[]"]:
            with (
                patch.object(pace.urllib.request, "urlopen", return_value=io.StringIO(raw)),
                self.assertRaises(pace.UsageError),
            ):
                pace.fetch_usage("example")

    def test_http_errors_do_not_leak_response_or_token(self):
        for code in [401, 403, 429, 500]:
            error = urllib.error.HTTPError(
                pace.API_URL, code, "secret", {"Retry-After": "600"}, None
            )
            with (
                patch.object(pace.urllib.request, "urlopen", side_effect=error),
                self.assertRaises(pace.UsageError) as result,
            ):
                pace.fetch_usage("secret-token")
            self.assertNotIn("secret", str(result.exception))
            if code == 429:
                self.assertEqual(result.exception.retry_after, 600)

    def test_retry_after_dates_and_invalid_headers(self):
        with patch.object(net, "utc_now", return_value=NOW):
            self.assertEqual(pace.retry_delay("Sun, 27 Sep 2026 12:10:00 GMT"), 600)
        for value in [None, "invalid", "nan", "-1"]:
            self.assertEqual(pace.retry_delay(value), 300)
