#!/usr/bin/env python3
"""
Unit tests for hr_derby_runner.py

Run with:
    python -m pytest test_hr_derby.py -v
    python test_hr_derby.py
"""
import csv
import io
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open

# Ensure the module can be imported without real env vars
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-token")
os.environ.setdefault("OPTICODDS_API_KEY", "test-key")

import hr_derby_runner as runner


# ── Helpers ────────────────────────────────────────────────────────────────────

def make_csv_content(player_names):
    """Build CSV content with a Market Name column."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=["Market Name", "Odds"])
    writer.writeheader()
    for name in player_names:
        writer.writerow({"Market Name": name, "Odds": "+500"})
    return buf.getvalue()


def make_stats_response(hr_data):
    """
    Build a mock MLB Stats API response.
    hr_data: list of (player_name, team_abbrev, hr_count)
    """
    splits = []
    for name, team, hrs in hr_data:
        splits.append({
            "player": {"fullName": name},
            "team": {"abbreviation": team},
            "stat": {"homeRuns": hrs},
        })
    return {"stats": [{"splits": splits}]}


def make_schedule_response(total_games):
    return {"totalGames": total_games}


# ── Tests: CSV parsing ─────────────────────────────────────────────────────────

class TestCSVParsing(unittest.TestCase):
    """Test parsing a CSV to extract player names."""

    def test_parse_csv_extracts_market_names(self):
        content = make_csv_content(["Aaron Judge", "Shohei Ohtani", "Pete Alonso"])
        reader = csv.DictReader(io.StringIO(content))
        names = [row["Market Name"].strip() for row in reader if row["Market Name"].strip()]
        self.assertEqual(names, ["Aaron Judge", "Shohei Ohtani", "Pete Alonso"])

    def test_parse_csv_skips_blank_names(self):
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=["Market Name", "Odds"])
        writer.writeheader()
        writer.writerow({"Market Name": "Aaron Judge", "Odds": "+300"})
        writer.writerow({"Market Name": "", "Odds": "+500"})
        writer.writerow({"Market Name": "  ", "Odds": "+600"})
        writer.writerow({"Market Name": "Pete Alonso", "Odds": "+400"})
        content = buf.getvalue()

        reader = csv.DictReader(io.StringIO(content))
        names = [row["Market Name"].strip() for row in reader if row["Market Name"].strip()]
        self.assertEqual(names, ["Aaron Judge", "Pete Alonso"])

    def test_empty_csv_no_players(self):
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=["Market Name", "Odds"])
        writer.writeheader()
        content = buf.getvalue()

        reader = csv.DictReader(io.StringIO(content))
        names = [row["Market Name"].strip() for row in reader if row["Market Name"].strip()]
        self.assertEqual(names, [])


# ── Tests: MLB Stats API parsing ───────────────────────────────────────────────

class TestStatsAPIParsing(unittest.TestCase):
    """Test parsing MLB Stats API response to find HR hitters."""

    def test_parse_hr_hitters(self):
        resp = make_stats_response([
            ("Aaron Judge", "NYY", 2),
            ("Shohei Ohtani", "LAD", 1),
            ("Pete Alonso", "NYM", 0),
        ])
        hr_hitters = {}
        total_hrs = 0
        for group in resp.get("stats", []):
            for split in group.get("splits", []):
                hrs = split.get("stat", {}).get("homeRuns", 0)
                if hrs > 0:
                    name = split["player"]["fullName"]
                    team = split.get("team", {}).get("abbreviation", "")
                    hr_hitters[name.lower()] = (hrs, team)
                    total_hrs += hrs

        self.assertIn("aaron judge", hr_hitters)
        self.assertIn("shohei ohtani", hr_hitters)
        self.assertNotIn("pete alonso", hr_hitters)
        self.assertEqual(total_hrs, 3)
        self.assertEqual(hr_hitters["aaron judge"], (2, "NYY"))

    def test_empty_stats_response(self):
        resp = {"stats": []}
        hr_hitters = {}
        total_hrs = 0
        for group in resp.get("stats", []):
            for split in group.get("splits", []):
                hrs = split.get("stat", {}).get("homeRuns", 0)
                if hrs > 0:
                    name = split["player"]["fullName"]
                    team = split.get("team", {}).get("abbreviation", "")
                    hr_hitters[name.lower()] = (hrs, team)
                    total_hrs += hrs
        self.assertEqual(hr_hitters, {})
        self.assertEqual(total_hrs, 0)


# ── Tests: Cross-referencing (WON vs LOST) ────────────────────────────────────

class TestCrossReference(unittest.TestCase):
    """Test cross-referencing slate players vs HR hitters."""

    def test_won_and_lost_classification(self):
        players = ["Aaron Judge", "Pete Alonso", "Shohei Ohtani"]
        hr_hitters = {
            "aaron judge": (2, "NYY"),
            "shohei ohtani": (1, "LAD"),
        }
        won, lost = [], []
        for player in players:
            if player.lower() in hr_hitters:
                _, team = hr_hitters[player.lower()]
                won.append(f"{player} ({team})")
            else:
                lost.append(player)

        self.assertEqual(won, ["Aaron Judge (NYY)", "Shohei Ohtani (LAD)"])
        self.assertEqual(lost, ["Pete Alonso"])

    def test_nobody_hit_hr(self):
        players = ["Pete Alonso", "Freddie Freeman"]
        hr_hitters = {}
        won, lost = [], []
        for player in players:
            if player.lower() in hr_hitters:
                _, team = hr_hitters[player.lower()]
                won.append(f"{player} ({team})")
            else:
                lost.append(player)

        self.assertEqual(won, [])
        self.assertEqual(lost, ["Pete Alonso", "Freddie Freeman"])

    def test_everyone_hit_hr(self):
        players = ["Aaron Judge", "Shohei Ohtani"]
        hr_hitters = {
            "aaron judge": (1, "NYY"),
            "shohei ohtani": (1, "LAD"),
        }
        won, lost = [], []
        for player in players:
            if player.lower() in hr_hitters:
                _, team = hr_hitters[player.lower()]
                won.append(f"{player} ({team})")
            else:
                lost.append(player)

        self.assertEqual(len(won), 2)
        self.assertEqual(lost, [])


# ── Tests: Slack message formatting ────────────────────────────────────────────

class TestMessageFormatting(unittest.TestCase):
    """Test formatting the Slack results message correctly."""

    def _build_message(self, won, lost, total_hrs, game_count, day_label):
        lines = [f":baseball: *HR Derby Results — {day_label}* ({game_count} games on the slate)", ""]
        if won:
            lines += [f":white_check_mark: *WON ({len(won)} players hit a HR):*"] + [f"• {p}" for p in won]
        else:
            lines.append(":goat: Tough day — nobody on the slate went yard")
        lines += [
            "",
            f":x: *LOST ({len(lost)} players did not hit a HR):*",
            ", ".join(lost),
            "",
            f":bar_chart: *{total_hrs} total HRs hit across {game_count} games yesterday*",
        ]
        return "\n".join(lines)

    def test_format_with_winners_and_losers(self):
        msg = self._build_message(
            won=["Aaron Judge (NYY)"],
            lost=["Pete Alonso", "Freddie Freeman"],
            total_hrs=15,
            game_count=8,
            day_label="Sat, May 3",
        )
        self.assertIn(":baseball: *HR Derby Results", msg)
        self.assertIn("WON (1 players hit a HR)", msg)
        self.assertIn("Aaron Judge (NYY)", msg)
        self.assertIn("LOST (2 players did not hit a HR)", msg)
        self.assertIn("Pete Alonso, Freddie Freeman", msg)
        self.assertIn("15 total HRs hit across 8 games", msg)

    def test_format_nobody_hit_hr(self):
        msg = self._build_message(
            won=[],
            lost=["Pete Alonso", "Freddie Freeman"],
            total_hrs=5,
            game_count=6,
            day_label="Sun, May 4",
        )
        self.assertIn("Tough day", msg)
        self.assertNotIn("WON", msg)

    def test_format_header_includes_game_count(self):
        msg = self._build_message(
            won=["Judge (NYY)"], lost=[], total_hrs=10, game_count=12,
            day_label="Wed, May 7",
        )
        self.assertIn("12 games on the slate", msg)


# ── Tests: result_yesterday full function ──────────────────────────────────────

class TestResultYesterday(unittest.TestCase):
    """Test result_yesterday() with mocked HTTP and filesystem."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.orig_script_dir = runner.SCRIPT_DIR
        runner.SCRIPT_DIR = Path(self.tmpdir)

    def tearDown(self):
        runner.SCRIPT_DIR = self.orig_script_dir
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_csv(self, dt, player_names):
        fname = f"HR Derby MLB {dt.strftime('%m-%d-%Y')}.csv"
        path = Path(self.tmpdir) / fname
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["Market Name", "Odds"])
            writer.writeheader()
            for name in player_names:
                writer.writerow({"Market Name": name, "Odds": "+500"})
        return path

    @patch("hr_derby_runner.slack_post")
    @patch("hr_derby_runner.requests.get")
    def test_result_yesterday_with_winners(self, mock_get, mock_slack):
        yesterday = date(2026, 5, 3)
        self._write_csv(yesterday, ["Aaron Judge", "Pete Alonso"])

        stats_resp = MagicMock()
        stats_resp.json.return_value = make_stats_response([
            ("Aaron Judge", "NYY", 2),
            ("Mike Trout", "LAA", 1),
        ])
        stats_resp.raise_for_status = MagicMock()

        schedule_resp = MagicMock()
        schedule_resp.json.return_value = make_schedule_response(8)
        schedule_resp.raise_for_status = MagicMock()

        mock_get.side_effect = [stats_resp, schedule_resp]

        runner.result_yesterday(yesterday)

        mock_slack.assert_called_once()
        msg = mock_slack.call_args[0][0]
        self.assertIn("Aaron Judge (NYY)", msg)
        self.assertIn("Pete Alonso", msg)
        self.assertIn("WON (1 players hit a HR)", msg)
        self.assertIn("LOST (1 players did not hit a HR)", msg)

    @patch("hr_derby_runner.slack_post")
    @patch("hr_derby_runner.requests.get")
    def test_result_yesterday_nobody_hit_hr(self, mock_get, mock_slack):
        yesterday = date(2026, 5, 3)
        self._write_csv(yesterday, ["Pete Alonso", "Freddie Freeman"])

        stats_resp = MagicMock()
        stats_resp.json.return_value = make_stats_response([
            ("Mike Trout", "LAA", 1),
        ])
        stats_resp.raise_for_status = MagicMock()

        schedule_resp = MagicMock()
        schedule_resp.json.return_value = make_schedule_response(6)
        schedule_resp.raise_for_status = MagicMock()

        mock_get.side_effect = [stats_resp, schedule_resp]

        runner.result_yesterday(yesterday)

        msg = mock_slack.call_args[0][0]
        self.assertIn("Tough day", msg)
        self.assertNotIn("WON", msg)

    def test_result_yesterday_no_csv(self):
        """When no CSV exists, result_yesterday should return without posting."""
        yesterday = date(2026, 5, 3)
        # No CSV written — function should return silently
        result = runner.result_yesterday(yesterday)
        self.assertIsNone(result)

    @patch("hr_derby_runner.slack_post")
    @patch("hr_derby_runner.requests.get")
    def test_result_yesterday_empty_csv(self, mock_get, mock_slack):
        """Empty CSV with headers only should return without posting."""
        yesterday = date(2026, 5, 3)
        self._write_csv(yesterday, [])
        result = runner.result_yesterday(yesterday)
        self.assertIsNone(result)
        mock_slack.assert_not_called()
        mock_get.assert_not_called()


# ── Tests: --dry-run flag ──────────────────────────────────────────────────────

class TestDryRunFlag(unittest.TestCase):
    """Test that --dry-run prints to stdout instead of posting to Slack."""

    def setUp(self):
        self.orig_dry_run = runner.DRY_RUN

    def tearDown(self):
        runner.DRY_RUN = self.orig_dry_run

    def test_slack_post_dry_run_prints_to_stdout(self):
        runner.DRY_RUN = True
        import io as _io
        captured = _io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            result = runner.slack_post("Hello world")
            output = captured.getvalue()
        finally:
            sys.stdout = old_stdout

        self.assertIn("[DRY-RUN]", output)
        self.assertIn("Hello world", output)
        self.assertEqual(result["ok"], True)

    def test_slack_post_dry_run_with_thread(self):
        runner.DRY_RUN = True
        import io as _io
        captured = _io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            runner.slack_post("threaded msg", thread_ts="123.456")
            output = captured.getvalue()
        finally:
            sys.stdout = old_stdout

        self.assertIn("(thread 123.456)", output)

    @patch("hr_derby_runner.requests.post")
    def test_slack_post_dry_run_does_not_call_api(self, mock_post):
        runner.DRY_RUN = True
        import io as _io
        captured = _io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            runner.slack_post("test message")
        finally:
            sys.stdout = old_stdout
        mock_post.assert_not_called()


# ── Tests: argparse modes ─────────────────────────────────────────────────────

class TestArgParse(unittest.TestCase):
    """Test CLI argument parsing."""

    def test_default_no_args(self):
        args = runner.parse_args([])
        self.assertFalse(args.results_only)
        self.assertFalse(args.dry_run)

    def test_results_only(self):
        args = runner.parse_args(["--results-only"])
        self.assertTrue(args.results_only)
        self.assertFalse(args.dry_run)

    def test_dry_run(self):
        args = runner.parse_args(["--dry-run"])
        self.assertFalse(args.results_only)
        self.assertTrue(args.dry_run)

    def test_both_flags(self):
        args = runner.parse_args(["--results-only", "--dry-run"])
        self.assertTrue(args.results_only)
        self.assertTrue(args.dry_run)


# ── Tests: Integration --results-only --dry-run ───────────────────────────────

class TestResultsOnlyDryRunIntegration(unittest.TestCase):
    """Integration test: --results-only --dry-run runs end-to-end without Slack."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.orig_script_dir = runner.SCRIPT_DIR
        self.orig_dry_run = runner.DRY_RUN
        runner.SCRIPT_DIR = Path(self.tmpdir)

    def tearDown(self):
        runner.SCRIPT_DIR = self.orig_script_dir
        runner.DRY_RUN = self.orig_dry_run
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_csv(self, dt, player_names):
        fname = f"HR Derby MLB {dt.strftime('%m-%d-%Y')}.csv"
        path = Path(self.tmpdir) / fname
        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["Market Name", "Odds"])
            writer.writeheader()
            for name in player_names:
                writer.writerow({"Market Name": name, "Odds": "+500"})
        return path

    @patch("hr_derby_runner.requests.get")
    @patch("hr_derby_runner.requests.post")
    def test_results_only_dry_run_no_csv(self, mock_post, mock_get):
        """No CSV: should print 'no derby was running' to stdout, not call Slack API."""
        import io as _io
        captured = _io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            runner.main(["--results-only", "--dry-run"])
            output = captured.getvalue()
        finally:
            sys.stdout = old_stdout

        self.assertIn("[DRY-RUN]", output)
        self.assertIn("no derby was running", output)
        mock_post.assert_not_called()

    @patch("hr_derby_runner.requests.get")
    @patch("hr_derby_runner.requests.post")
    def test_results_only_dry_run_with_csv(self, mock_post, mock_get):
        """With CSV: should print formatted results to stdout, not call Slack API."""
        # Figure out what yesterday is so we can write the right CSV
        now_utc = datetime.now(timezone.utc)
        ct_offset = timedelta(hours=-5)
        now_ct = now_utc + ct_offset
        yesterday_ct = (now_ct - timedelta(days=1)).date()

        self._write_csv(yesterday_ct, ["Aaron Judge", "Pete Alonso"])

        stats_resp = MagicMock()
        stats_resp.json.return_value = make_stats_response([
            ("Aaron Judge", "NYY", 1),
        ])
        stats_resp.raise_for_status = MagicMock()

        schedule_resp = MagicMock()
        schedule_resp.json.return_value = make_schedule_response(10)
        schedule_resp.raise_for_status = MagicMock()

        mock_get.side_effect = [stats_resp, schedule_resp]

        import io as _io
        captured = _io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            runner.main(["--results-only", "--dry-run"])
            output = captured.getvalue()
        finally:
            sys.stdout = old_stdout

        self.assertIn("[DRY-RUN]", output)
        self.assertIn("HR Derby Results", output)
        self.assertIn("Aaron Judge (NYY)", output)
        self.assertIn("Pete Alonso", output)
        mock_post.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class TestDateFlag(unittest.TestCase):
    """Tests for the --date argument."""

    def test_parse_date_arg(self):
        args = runner.parse_args(["--results-only", "--date", "2026-05-02"])
        self.assertTrue(args.results_only)
        self.assertEqual(args.date, "2026-05-02")

    def test_parse_no_date_defaults_none(self):
        args = runner.parse_args(["--results-only"])
        self.assertIsNone(args.date)

    @patch("hr_derby_runner.slack_post")
    def test_results_only_with_specific_date_no_csv(self, mock_post):
        runner.DRY_RUN = True
        runner.run_results_only(target_date="2026-01-01")
        mock_post.assert_called_once()
        msg = mock_post.call_args[0][0]
        self.assertIn("Jan 1", msg)
        self.assertIn("No HR Derby CSV found", msg)

    def test_results_only_invalid_date_format(self):
        runner.DRY_RUN = True
        with self.assertRaises(SystemExit):
            runner.run_results_only(target_date="05-02-2026")

    @patch("hr_derby_runner.result_yesterday")
    @patch("hr_derby_runner.slack_post")
    def test_results_only_with_date_and_csv(self, mock_post, mock_result):
        runner.DRY_RUN = True
        target = "2026-05-02"
        csv_path = Path(runner.SCRIPT_DIR) / "HR Derby MLB 05-02-2026.csv"
        csv_path.write_text("Market Name\nAaron Judge\n")
        try:
            runner.run_results_only(target_date=target)
            from datetime import date as date_cls
            mock_result.assert_called_once_with(date_cls(2026, 5, 2))
        finally:
            csv_path.unlink(missing_ok=True)
