import contextlib
import io
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from agent_usage_pace import cli
from agent_usage_pace.core import UsageError, UsageWindow

NOW = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
WINDOW = UsageWindow("weekly", "Week", 25, timedelta(days=7), NOW + timedelta(days=3.5))


class CliTests(unittest.TestCase):
    def test_default_interval_and_provider_selection(self):
        self.assertEqual(cli.parse_args([]).interval, 20)
        self.assertIsNone(cli.parse_args([]).provider)
        self.assertEqual(cli.parse_args(["codex"]).provider, "codex")
        self.assertTrue(cli.parse_args(["--all"]).all)

    def test_invalid_arguments(self):
        for args in [
            ["codex", "--all"],
            ["--interval", "nan"],
            ["--timeout", "0"],
            ["--tolerance", "-1"],
            ["--renew-day", "29"],
            ["--token", "example"],
            ["codex", "--token", "example"],
            ["--usage", "20"],
            ["claude", "--usage", "101"],
            ["claude", "--usage", "20"],
            ["claude", "--resets-at", "2026-09-27T12:00:00Z"],
        ]:
            with (
                self.subTest(args=args),
                contextlib.redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as exc,
            ):
                cli.parse_args(args)
            self.assertEqual(exc.exception.code, 2)

    def test_manual_snapshots_never_fetch(self):
        for provider in cli.PROVIDERS:
            output = io.StringIO()
            args = [provider, "--usage", "75", "--json"]
            if provider != "cursor":
                args += ["--resets-at", "2026-10-01T00:00:00Z"]
            with (
                patch.object(cli, "collect", side_effect=AssertionError),
                patch.object(cli, "utc_now", return_value=NOW),
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(cli.main(args), 0)
            data = json.loads(output.getvalue())
            self.assertEqual(data["schema_version"], 1)
            self.assertEqual(data["providers"][provider]["source"], "manual")
            if provider != "cursor":
                self.assertEqual(
                    data["providers"][provider]["windows"][0]["projected_percent"], 150
                )

    def test_provider_errors_do_not_hide_successful_results(self):
        output = io.StringIO()
        with (
            patch.object(cli.claude, "fetch", side_effect=UsageError("Rate limited", 600)),
            patch.object(cli.codex, "fetch", return_value=[WINDOW]),
            patch.object(cli.cursor, "fetch", return_value=[WINDOW]),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(cli.main(["--all", "--json"]), 1)
        data = json.loads(output.getvalue())["providers"]
        self.assertEqual(data["claude"]["error"], "Rate limited")
        self.assertEqual(data["codex"]["windows"][0]["usage_percent"], 25)
        self.assertEqual(data["cursor"]["windows"][0]["usage_percent"], 25)

    def test_backoff_is_independent_and_stale_is_explicit(self):
        args = cli.parse_args(["--all"])
        states = {
            "claude": cli.ProviderState(windows=[WINDOW], fetched_at=NOW),
            "codex": cli.ProviderState(),
        }
        with (
            patch.object(cli.time, "monotonic", return_value=100),
            patch.object(cli.claude, "fetch", side_effect=UsageError("Rate limited", 600)),
            patch.object(cli.codex, "fetch", return_value=[WINDOW]),
        ):
            cli.collect(states, args)
        self.assertEqual(states["claude"].retry_at, 700)
        self.assertEqual(states["codex"].retry_at, 120)
        with (
            patch.object(cli.time, "monotonic", return_value=121),
            patch.object(cli.claude, "fetch", side_effect=AssertionError),
            patch.object(cli.codex, "fetch", return_value=[WINDOW]) as fetch,
        ):
            cli.collect(states, args)
        fetch.assert_called_once()
        data = cli.make_snapshot(states, args)
        self.assertTrue(data["providers"]["claude"]["stale"])
        self.assertIn("Showing last-known usage", cli.render_human(data))

    def test_unexpected_errors_are_isolated_without_leaking_details(self):
        states = {"claude": cli.ProviderState(), "codex": cli.ProviderState()}
        with (
            patch.object(cli.claude, "fetch", side_effect=ValueError("secret-token")),
            patch.object(cli.codex, "fetch", return_value=[WINDOW]),
        ):
            cli.collect(states, cli.parse_args(["--all"]))
        self.assertNotIn("secret-token", states["claude"].error)
        self.assertIn("ValueError", states["claude"].error)
        self.assertEqual(states["codex"].windows, [WINDOW])

    def test_success_clears_a_previous_error(self):
        states = {"codex": cli.ProviderState(error="old error")}
        with patch.object(cli.codex, "fetch", return_value=[WINDOW]):
            cli.collect(states, cli.parse_args(["codex"]))
        self.assertIsNone(states["codex"].error)

    def test_live_interrupt_restores_terminal(self):
        output = io.StringIO()
        with (
            patch.object(cli, "collect", side_effect=KeyboardInterrupt),
            contextlib.redirect_stdout(output),
            self.assertRaises(KeyboardInterrupt),
        ):
            cli.run_live({"codex": cli.ProviderState()}, cli.parse_args(["codex"]))
        self.assertTrue(output.getvalue().endswith("\x1b[?25h\x1b[?1049l"))

    def test_shortcut_entry_points_forward_arguments(self):
        for name in cli.PROVIDERS:
            with (
                patch.object(cli.sys, "argv", [name + "-usage-pace", "--once"]),
                patch.object(cli, "main", return_value=0) as main,
            ):
                self.assertEqual(getattr(cli, name + "_main")(), 0)
                main.assert_called_once_with([name, "--once"])
