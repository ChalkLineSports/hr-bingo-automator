#!/usr/bin/env python3
"""
QA guardrail tests for HR Derby lineup quality.

These tests exist because of real complaints about the generated cards:
  - TEN Yankees on one card (no per-team cap)
  - players who hit only 1 HR all season
  - players who are OUT on the Injured List
  - retired players (e.g. Anthony Rendon)
  - pitchers offered as HR hitters
  - the actual HR leaders missing entirely

Each complaint now has a test that fails if the bug ever comes back, covering
BOTH paths the automation runs:
  1. Producing the list (Mon->Tue / Fri->Sat lineup generation)
  2. Sharing the results (Wed / Sun results posting)

Run with:
    python -m pytest test_hr_derby_quality.py -v
"""
import csv
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-token")
os.environ.setdefault("OPTICODDS_API_KEY", "test-key")

import hr_derby_runner as runner
import hr_derby_generator as generator

GENERATOR_PATH = Path(__file__).parent / "hr_derby_generator.py"


# ── Shared fixtures ──────────────────────────────────────────────────────────────

SLATE_TEAMS = {"New York Yankees", "Cleveland Guardians", "Los Angeles Dodgers"}

# A small contestant map. NOTE: "Munetaka Murakami" is deliberately ABSENT to
# exercise the missing-from-map warning, and "Jose Ramirez" is stored ASCII while
# the StatsAPI feed below uses the accented "José Ramírez".
CONTESTANT_MAP = {
    "Aaron Judge":        {"id": 4838, "team": "New York Yankees"},
    "Ben Rice":           {"id": 9043, "team": "New York Yankees"},
    "Jazz Chisholm":      {"id": 5045, "team": "New York Yankees"},
    "Giancarlo Stanton":  {"id": 1557, "team": "New York Yankees"},
    "Trent Grisham":      {"id": 5037, "team": "New York Yankees"},
    "Anthony Volpe":      {"id": 8232, "team": "New York Yankees"},
    "Austin Wells":       {"id": 8226, "team": "New York Yankees"},
    "Jasson Dominguez":   {"id": 8233, "team": "New York Yankees"},
    "Oswald Peraza":      {"id": 8300, "team": "New York Yankees"},
    "Jose Ramirez":       {"id": 4797, "team": "Cleveland Guardians"},
    "Kyle Manzardo":      {"id": 8227, "team": "Cleveland Guardians"},
    "David Fry":          {"id": 8255, "team": "Cleveland Guardians"},
    "Shohei Ohtani":      {"id": 4816, "team": "Los Angeles Dodgers"},
    "Freddie Freeman":    {"id": 4749, "team": "Los Angeles Dodgers"},
    "Mookie Betts":       {"id": 4700, "team": "Los Angeles Dodgers"},
    "Andy Pages":         {"id": 8274, "team": "Los Angeles Dodgers"},
    # Pitchers are in the map too — proving the POSITION filter (not absence) excludes them.
    "Emmet Sheehan":      {"id": 9120, "team": "Los Angeles Dodgers"},
    "Tanner Bibee":       {"id": 8265, "team": "Cleveland Guardians"},
    # A slugger on a team that is NOT in tonight's slate.
    "Kyle Schwarber":     {"id": 6000, "team": "Philadelphia Phillies"},
}


def make_hr_leaders():
    """
    Mimic the MLB Stats API season HR leaders, HR desc — the June-6-style scenario:
    a Yankees-heavy field, two pitchers, an off-slate slugger, an unmapped slugger,
    and a 1-HR Yankee that should never make a card full of real power hitters.
    """
    rows = [
        # (name, hr, team, position)
        ("Aaron Judge",       25, "New York Yankees",     "RF"),
        ("Shohei Ohtani",     21, "Los Angeles Dodgers",  "DH"),
        ("Munetaka Murakami", 20, "New York Yankees",     "3B"),  # NOT in contestant map
        ("Ben Rice",          18, "New York Yankees",     "1B"),
        ("José Ramírez",      16, "Cleveland Guardians",  "3B"),  # accented
        ("Jazz Chisholm",     14, "New York Yankees",     "2B"),
        ("Giancarlo Stanton", 13, "New York Yankees",     "DH"),  # will be marked OUT
        ("Freddie Freeman",   12, "Los Angeles Dodgers",  "1B"),
        ("Trent Grisham",     11, "New York Yankees",     "CF"),
        ("Mookie Betts",      10, "Los Angeles Dodgers",  "SS"),
        ("Anthony Volpe",      9, "New York Yankees",     "SS"),
        ("Andy Pages",         9, "Los Angeles Dodgers",  "CF"),
        ("Kyle Manzardo",      8, "Cleveland Guardians",  "1B"),
        ("Austin Wells",       8, "New York Yankees",     "C"),
        ("Jasson Dominguez",   7, "New York Yankees",     "LF"),
        ("David Fry",          6, "Cleveland Guardians",  "C"),
        ("Kyle Schwarber",    23, "Philadelphia Phillies", "DH"),  # off-slate
        ("Emmet Sheehan",      0, "Los Angeles Dodgers",  "SP"),   # pitcher
        ("Tanner Bibee",       0, "Cleveland Guardians",  "P"),    # pitcher
        ("Oswald Peraza",      1, "New York Yankees",     "SS"),   # weak hitter
    ]
    return [
        {"name": n, "hr": hr, "team": t, "position": p}
        for n, hr, t, p in rows
    ]


def run_generator(props, fixtures, contestant_map):
    """Invoke the generator exactly as run_job does and return the parsed __RESULT__."""
    with tempfile.TemporaryDirectory() as tmp:
        map_path = Path(tmp) / "map.json"
        map_path.write_text(json.dumps(contestant_map))
        payload = json.dumps({"fixtures": fixtures, "props": props})
        proc = subprocess.run(
            [sys.executable, str(GENERATOR_PATH),
             "--date", "2026-06-09",
             "--contestant-map", str(map_path),
             "--output", tmp],
            input=payload, capture_output=True, text=True,
        )
        assert proc.returncode == 0, f"generator failed:\n{proc.stderr}\n{proc.stdout[-500:]}"
        marker = "__RESULT__"
        assert marker in proc.stdout, f"no __RESULT__ in output:\n{proc.stdout[-500:]}"
        return json.loads(proc.stdout[proc.stdout.index(marker) + len(marker):].strip())


def team_of(name):
    return CONTESTANT_MAP.get(name, {}).get("team", "??")


# ── Injury / IL status filtering ─────────────────────────────────────────────────

class TestInjuryStatusFilter(unittest.TestCase):
    """The old filter only caught 'out'/'doubtful'; IL variants slipped through."""

    def test_excludes_out_and_doubtful(self):
        self.assertTrue(runner.is_unavailable("Out"))
        self.assertTrue(runner.is_unavailable("doubtful"))

    def test_excludes_real_opticodds_slugs(self):
        # These are the ACTUAL status strings returned by the live OpticOdds MLB
        # injury feed (verified against api.opticodds.com /v3/injuries).
        for status in ("out", "il_60-day", "il_15-day", "il_7-day", "il_10-day",
                       "il", "suspended"):
            self.assertTrue(runner.is_unavailable(status), f"{status!r} should be excluded")

    def test_excludes_human_readable_il_variants(self):
        # Tolerate prose variants too, in case the feed format ever changes.
        for status in ("10-Day IL", "15-Day IL", "60-Day IL", "7-Day IL",
                       "Injured List", "IL", "Day-To-Day", "day to day",
                       "Suspended", "Restricted List", "Paternity List"):
            self.assertTrue(runner.is_unavailable(status), f"{status!r} should be excluded")

    def test_keeps_available_players(self):
        for status in ("Active", "Probable", "Questionable", "Available",
                       "Game-Time Decision", "", None):
            self.assertFalse(runner.is_unavailable(status), f"{status!r} should be available")

    def test_available_substring_il_is_not_a_false_positive(self):
        # "available" contains the substring "il" — must NOT be treated as Injured List.
        self.assertFalse(runner.is_unavailable("Available"))


# ── Name resolution (accents, aliases, suffixes) ─────────────────────────────────

class TestNameResolution(unittest.TestCase):
    def setUp(self):
        self.index = runner.build_name_index(CONTESTANT_MAP)

    def test_exact_match(self):
        canonical, entry = runner.resolve_contestant("Aaron Judge", CONTESTANT_MAP, self.index)
        self.assertEqual(canonical, "Aaron Judge")
        self.assertEqual(entry["id"], 4838)

    def test_accented_name_resolves(self):
        canonical, entry = runner.resolve_contestant("José Ramírez", CONTESTANT_MAP, self.index)
        self.assertEqual(canonical, "Jose Ramirez")
        self.assertEqual(entry["id"], 4797)

    def test_alias_jr_suffix(self):
        canonical, _ = runner.resolve_contestant("Jazz Chisholm Jr.", CONTESTANT_MAP, self.index)
        self.assertEqual(canonical, "Jazz Chisholm")

    def test_unmapped_player_returns_none(self):
        canonical, entry = runner.resolve_contestant("Munetaka Murakami", CONTESTANT_MAP, self.index)
        self.assertIsNone(canonical)
        self.assertIsNone(entry)


# ── get_hr_leaders parsing ───────────────────────────────────────────────────────

class TestGetInjuriesPagination(unittest.TestCase):
    """get_injuries must follow pagination — page 1 alone misses injured hitters."""

    @patch("hr_derby_runner.requests.get")
    def test_follows_all_pages(self, mock_get):
        def page(players, has_more):
            resp = MagicMock()
            resp.json.return_value = {
                "data": [{"player": {"name": n}, "status": "il_60-day"} for n in players],
                "has_more": has_more,
            }
            resp.raise_for_status = MagicMock()
            return resp

        mock_get.side_effect = [
            page(["A", "B"], True),
            page(["C", "D"], True),
            page(["E"], False),
        ]
        injuries = runner.get_injuries()
        names = [i["player"]["name"] for i in injuries]
        self.assertEqual(names, ["A", "B", "C", "D", "E"])
        self.assertEqual(mock_get.call_count, 3)

    @patch("hr_derby_runner.requests.get")
    def test_stops_when_no_more(self, mock_get):
        resp = MagicMock()
        resp.json.return_value = {"data": [{"player": {"name": "A"}, "status": "out"}],
                                  "has_more": False}
        resp.raise_for_status = MagicMock()
        mock_get.return_value = resp
        injuries = runner.get_injuries()
        self.assertEqual(len(injuries), 1)
        self.assertEqual(mock_get.call_count, 1)


class TestGetHrLeadersParsing(unittest.TestCase):
    @patch("hr_derby_runner.requests.get")
    def test_parses_name_hr_team_position(self, mock_get):
        resp = MagicMock()
        resp.json.return_value = {"stats": [{"splits": [
            {"player": {"fullName": "Aaron Judge"}, "stat": {"homeRuns": 25},
             "team": {"name": "New York Yankees"}, "position": {"abbreviation": "RF"}},
            {"player": {"fullName": "Emmet Sheehan"}, "stat": {"homeRuns": 0},
             "team": {"name": "Los Angeles Dodgers"}, "position": {"abbreviation": "SP"}},
        ]}]}
        resp.raise_for_status = MagicMock()
        mock_get.return_value = resp

        leaders = runner.get_hr_leaders(2026)
        self.assertEqual(leaders[0], {"name": "Aaron Judge", "hr": 25,
                                      "team": "New York Yankees", "position": "RF"})
        self.assertEqual(leaders[1]["position"], "SP")


# ── build_props: the pool the card is built from ────────────────────────────────

class TestBuildPropsQuality(unittest.TestCase):
    def setUp(self):
        self.out_players = {"Giancarlo Stanton"}  # marked OUT on the IL
        self.props, self.missing = runner.build_props(
            SLATE_TEAMS, self.out_players, CONTESTANT_MAP, make_hr_leaders()
        )
        self.names = [p["name"] for p in self.props]
        self.teams = Counter(p["team"] for p in self.props)

    def test_pool_is_ranked_by_hr_desc(self):
        hrs = [p["hr"] for p in self.props]
        self.assertEqual(hrs, sorted(hrs, reverse=True))

    def test_best_hr_hitters_are_present(self):
        self.assertIn("Aaron Judge", self.names)
        self.assertIn("Shohei Ohtani", self.names)

    def test_per_team_cap_enforced(self):
        self.assertLessEqual(self.teams["New York Yankees"], runner.MAX_PER_TEAM)

    def test_pitchers_excluded(self):
        self.assertNotIn("Emmet Sheehan", self.names)
        self.assertNotIn("Tanner Bibee", self.names)

    def test_injured_player_excluded(self):
        self.assertNotIn("Giancarlo Stanton", self.names)

    def test_off_slate_players_excluded(self):
        self.assertNotIn("Kyle Schwarber", self.names)

    def test_accented_leader_resolves_to_canonical(self):
        self.assertIn("Jose Ramirez", self.names)

    def test_unmapped_slugger_surfaced_not_dropped_silently(self):
        joined = " ".join(self.missing)
        self.assertIn("Munetaka Murakami", joined)
        self.assertNotIn("Munetaka Murakami", self.names)

    def test_every_pooled_player_has_a_contestant_id(self):
        index = runner.build_name_index(CONTESTANT_MAP)
        for name in self.names:
            _, entry = runner.resolve_contestant(name, CONTESTANT_MAP, index)
            self.assertIsNotNone(entry, f"{name} has no contestant ID")

    def test_empty_when_no_slate_teams_match(self):
        props, _ = runner.build_props(
            {"Miami Marlins"}, set(), CONTESTANT_MAP, make_hr_leaders()
        )
        self.assertEqual(props, [])


# ── End-to-end: producing Tuesday's card ─────────────────────────────────────────

class TestLineupEndToEnd(unittest.TestCase):
    """build_props -> generator subprocess -> CSV, asserting every complaint is fixed."""

    def setUp(self):
        props, _ = runner.build_props(
            SLATE_TEAMS, {"Giancarlo Stanton"}, CONTESTANT_MAP, make_hr_leaders()
        )
        fixtures = [
            {"id": "1", "start_date": "2026-06-10T00:10:00Z", "away": "Cleveland Guardians", "home": "New York Yankees"},
            {"id": "2", "start_date": "2026-06-10T02:10:00Z", "away": "Los Angeles Dodgers", "home": "Los Angeles Dodgers"},
        ]
        self.result = run_generator(props, fixtures, CONTESTANT_MAP)
        self.rows = list(csv.DictReader(io.StringIO(self.result["csv_content"])))
        self.card_names = [r["Market Name"] for r in self.rows]
        self.card_teams = Counter(team_of(n) for n in self.card_names)

    def test_no_team_exceeds_card_cap(self):
        # The complaint was TEN Yankees. The generator caps at MAX_PER_TEAM.
        for team, count in self.card_teams.items():
            self.assertLessEqual(count, generator.MAX_PER_TEAM, f"{team} appears {count} times")

    def test_no_pitchers_on_card(self):
        self.assertNotIn("Emmet Sheehan", self.card_names)
        self.assertNotIn("Tanner Bibee", self.card_names)

    def test_no_injured_player_on_card(self):
        self.assertNotIn("Giancarlo Stanton", self.card_names)

    def test_weak_hitter_excluded_when_real_hitters_available(self):
        self.assertNotIn("Oswald Peraza", self.card_names)

    def test_top_hitter_is_first(self):
        self.assertEqual(self.card_names[0], "Aaron Judge")

    def test_every_card_row_has_contestant_id(self):
        for r in self.rows:
            self.assertNotEqual(r["Contestant"], "NULL", f"{r['Market Name']} -> NULL")

    def test_flat_bingo_odds(self):
        for r in self.rows:
            self.assertEqual(r["Odds"], generator.BINGO_ODDS)


# ── Sharing the results (Wed / Sun) ──────────────────────────────────────────────

class TestResultsSharing(unittest.TestCase):
    """The results path must classify WON/LOST correctly, incl. accented names."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.orig_script_dir = runner.SCRIPT_DIR
        runner.SCRIPT_DIR = Path(self.tmpdir)

    def tearDown(self):
        runner.SCRIPT_DIR = self.orig_script_dir
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_card(self, dt, names):
        path = Path(self.tmpdir) / f"HR Derby MLB {dt.strftime('%m-%d-%Y')}.csv"
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["Market Name", "Contestant", "Order", "Odds"])
            w.writeheader()
            for i, n in enumerate(names, 1):
                w.writerow({"Market Name": n, "Contestant": "1", "Order": i, "Odds": "11.0000"})
        return path

    @patch("hr_derby_runner.slack_post")
    @patch("hr_derby_runner.requests.get")
    def test_accented_winner_counts_as_won(self, mock_get, mock_slack):
        """Regression: 'Jose Ramirez' on the card, 'José Ramírez' in the box score -> WON."""
        day = date(2026, 5, 20)
        self._write_card(day, ["Jose Ramirez", "Aaron Judge"])

        stats = MagicMock()
        stats.json.return_value = {"stats": [{"splits": [
            {"player": {"fullName": "José Ramírez"}, "stat": {"homeRuns": 1},
             "team": {"abbreviation": "CLE"}},
        ]}]}
        stats.raise_for_status = MagicMock()
        schedule = MagicMock()
        schedule.json.return_value = {"totalGames": 9}
        schedule.raise_for_status = MagicMock()
        mock_get.side_effect = [stats, schedule]

        runner.result_yesterday(day)

        msg = mock_slack.call_args[0][0]
        self.assertIn("Jose Ramirez (CLE)", msg)        # correctly WON
        self.assertIn("WON (1 players hit a HR)", msg)
        self.assertIn("Aaron Judge", msg.split("LOST")[1])  # Judge listed under LOST

    @patch("hr_derby_runner.slack_post")
    @patch("hr_derby_runner.requests.get")
    def test_won_and_lost_split(self, mock_get, mock_slack):
        day = date(2026, 5, 20)
        self._write_card(day, ["Aaron Judge", "Ben Rice"])
        stats = MagicMock()
        stats.json.return_value = {"stats": [{"splits": [
            {"player": {"fullName": "Aaron Judge"}, "stat": {"homeRuns": 2},
             "team": {"abbreviation": "NYY"}},
        ]}]}
        stats.raise_for_status = MagicMock()
        schedule = MagicMock()
        schedule.json.return_value = {"totalGames": 8}
        schedule.raise_for_status = MagicMock()
        mock_get.side_effect = [stats, schedule]

        runner.result_yesterday(day)
        msg = mock_slack.call_args[0][0]
        self.assertIn("Aaron Judge (NYY)", msg)
        self.assertIn("Ben Rice", msg.split("LOST")[1])


if __name__ == "__main__":
    unittest.main()
