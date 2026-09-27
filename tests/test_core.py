import unittest
from datetime import datetime, timedelta, timezone

from usage_pace import core as pace
from usage_pace import render

NOW = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)


class PaceTests(unittest.TestCase):
    def report(self, percent, remaining=timedelta(days=3.5), now=NOW):
        window = pace.UsageWindow("seven_day", "Week", percent, timedelta(days=7), NOW + remaining)
        return pace.build_report(window, 2, now)

    def test_status_and_unclamped_projection(self):
        for usage, status in [(10, "below"), (48, "on track"), (52, "on track"), (90, "above")]:
            with self.subTest(usage=usage):
                report = self.report(usage)
                self.assertEqual(report["expected_percent"], 50)
                self.assertEqual(report["status"], status)
                self.assertEqual(report["projected_percent"], usage * 2)

    def test_first_minute_uses_fractional_time(self):
        report = self.report(1, timedelta(days=7) - timedelta(minutes=1))
        self.assertGreater(report["expected_percent"], 0)
        self.assertLess(report["expected_percent"], 0.01)

    def test_window_start_does_not_divide_by_zero(self):
        report = self.report(0, timedelta(days=7))
        self.assertEqual(report["expected_percent"], 0)
        self.assertIsNone(report["projected_percent"])

    def test_expired_and_future_windows_have_no_pace(self):
        for remaining in [timedelta(0), timedelta(seconds=-1), timedelta(days=8)]:
            report = self.report(60, remaining)
            self.assertEqual(report["status"], "unavailable")
            self.assertIsNone(report["projected_percent"])
            self.assertIsNone(report["expected_percent"])

    def test_no_reset_retains_usage_without_guessing_pace(self):
        window = pace.UsageWindow("five_hour", "Session", 0, timedelta(hours=5), None)
        report = pace.build_report(window, 2, NOW)
        self.assertEqual(report["usage_percent"], 0)
        self.assertEqual(report["status"], "unavailable")
        self.assertIn("Reset time or window length is unavailable", render.render_windows([report]))

    def test_dst_does_not_change_elapsed_window_length(self):
        end = pace.parse_instant("2026-10-25T04:00:00+01:00")
        now = pace.parse_instant("2026-10-25T02:30:00+02:00")
        window = pace.UsageWindow("five_hour", "Session", 20, timedelta(hours=5), end)
        self.assertEqual(pace.build_report(window, 2, now)["expected_percent"], 50)
