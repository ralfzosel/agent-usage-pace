import io
import sqlite3
import tempfile
import unittest
import urllib.error
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from agent_usage_pace.core import UsageError, build_report
from agent_usage_pace.providers import cursor


class CursorTests(unittest.TestCase):
    def test_spend_fallbacks(self):
        for plan, expected in [
            ({"totalPercentUsed": 35}, 35),
            ({"includedSpend": 20, "limit": 100}, 20),
            ({"remaining": 60, "limit": 100}, 40),
            ({"totalSpend": 70, "limit": 100}, 70),
            ({"totalPercentUsed": float("inf")}, None),
            ({"totalSpend": 10, "limit": 0}, None),
            ({}, None),
        ]:
            self.assertEqual(cursor.extract_usage_percent(plan), expected)

    def test_api_dates_and_breakdown(self):
        window = cursor.parse_windows(
            {
                "planUsage": {"totalPercentUsed": 25, "autoPercentUsed": 10, "apiPercentUsed": 15},
                "billingCycleStart": "2026-09-10T00:00:00Z",
                "billingCycleEnd": "2026-10-10T00:00:00Z",
            }
        )[0]
        report = build_report(window, 2, datetime(2026, 9, 25, tzinfo=timezone.utc))
        self.assertEqual(report["expected_percent"], 50)
        self.assertEqual(report["projected_percent"], 50)
        self.assertEqual(window.breakdown, {"Auto+Composer": 10, "API usage": 15})

    def test_epoch_is_milliseconds(self):
        self.assertEqual(cursor.parse_billing_instant(1000).timestamp(), 1)
        self.assertEqual(cursor.parse_billing_instant("1000").timestamp(), 1)
        self.assertIsNone(cursor.parse_billing_instant(True))

    def test_month_and_year_boundaries(self):
        start, end = cursor.billing_cycle(10, datetime(2026, 1, 5, tzinfo=timezone.utc))
        self.assertEqual(start.isoformat(), "2025-12-10T00:00:00+00:00")
        self.assertEqual(end.isoformat(), "2026-01-10T00:00:00+00:00")
        self.assertEqual(cursor.add_month(datetime(2024, 1, 31)).day, 29)

    def test_missing_start_keeps_usage_without_invented_pace(self):
        window = cursor.parse_windows(
            {"planUsage": {"totalPercentUsed": 20}, "billingCycleEnd": "2026-10-01T00:00:00Z"}
        )[0]
        self.assertIsNone(window.start)
        self.assertEqual(
            build_report(window, 2, datetime(2026, 9, 27, tzinfo=timezone.utc))["status"],
            "unavailable",
        )

    def test_missing_both_dates_labels_fallback(self):
        window = cursor.parse_windows({"planUsage": {"totalPercentUsed": 20}}, 12)[0]
        self.assertIn("estimated from renew day 12", window.label)

    def test_local_db_read_and_missing_login(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.vscdb"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("CREATE TABLE ItemTable (key TEXT, value TEXT)")
                connection.execute(
                    "INSERT INTO ItemTable VALUES (?, ?)", (cursor.ACCESS_KEY, "example-token")
                )
                connection.commit()
            with (
                patch.object(cursor, "cursor_state_db_path", return_value=path),
                patch.dict(cursor.os.environ, {}, clear=True),
            ):
                self.assertEqual(cursor.read_access_token(), "example-token")
            with (
                patch.object(cursor, "cursor_state_db_path", return_value=path.parent / "missing"),
                patch.dict(cursor.os.environ, {}, clear=True),
                self.assertRaises(UsageError),
            ):
                cursor.read_access_token()

    def test_request_is_post_and_errors_do_not_leak(self):
        with patch.object(
            cursor.urllib.request, "urlopen", return_value=io.StringIO("{}")
        ) as request:
            self.assertEqual(cursor.fetch_usage("example-token"), {})
            self.assertEqual(request.call_args.args[0].get_method(), "POST")
        for code in (401, 403, 429, 500):
            error = urllib.error.HTTPError(
                cursor.API_URL, code, "secret", {"Retry-After": "600"}, None
            )
            with (
                patch.object(cursor.urllib.request, "urlopen", side_effect=error),
                self.assertRaises(UsageError) as result,
            ):
                cursor.fetch_usage("secret-token")
            self.assertNotIn("secret", str(result.exception))
            if code == 429:
                self.assertEqual(result.exception.retry_after, 600)
