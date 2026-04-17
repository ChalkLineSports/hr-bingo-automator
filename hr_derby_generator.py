"""
Chalkline HR Derby CSV Generator
Generates daily MLB home run derby files for Chalkline upload.

Usage:
    python hr_derby_generator.py --date 2026-04-09 --cutoff-hour 17 --output ~/Downloads
    python hr_derby_generator.py --run-tests
"""

import csv
import json
import os
import sys
import argparse
import traceback
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

# ─── Constants ────────────────────────────────────────────────────────────────

CONTESTANT_MAP_PATH = Path("/Users/joekustelski/Downloads/mlb_contestant_map.json")
OUTPUT_DIR = Path("/Users/joekustelski/Downloads")
TOP_N_PLAYERS = 25
BOOK_NAME = "Pick Your Home Run Hitters"
ODDS_TYPE = "Championship Outright"
MIN_GAMES_WARNING = 5  # Alert the team if fewer than this many evening games are found

# Tier-based estimated odds (American) when sportsbooks haven't posted yet.
# These represent a reasonable prior based on known power profiles.
POWER_TIERS = {
    "elite": {
        "players": [
            "Aaron Judge", "Shohei Ohtani", "Giancarlo Stanton", "Pete Alonso",
            "Kyle Schwarber", "Matt Olson", "Yordan Alvarez", "Vladimir Guerrero Jr.",
            "Gunnar Henderson", "Bobby Witt Jr.", "Marcell Ozuna", "Freddie Freeman",
            "Fernando Tatis Jr.", "José Ramírez", "Jose Ramirez",
        ],
        "odds": 300,   # +300 implied ~25%
    },
    "above_average": {
        "players": [
            "Riley Greene", "Junior Caminero", "Ben Rice", "Jonathan Aranda",
            "Jazz Chisholm", "Jazz Chisholm Jr.", "Brent Rooker", "Nick Kurtz",
            "Trent Grisham", "Spencer Torkelson", "Elly De La Cruz", "Elly de la Cruz",
            "Pete Crow-Armstrong", "Michael Busch", "Shea Langeliers",
            "Eugenio Suarez", "Agustin Ramirez", "Alex Bregman", "Ryan McMahon",
            "Colt Keith", "Kerry Carpenter", "Byron Buxton", "Jackson Chourio",
            "Sal Stewart", "Jackson Merrill", "Bryce Harper", "Manny Machado",
            "Trea Turner", "Paul Goldschmidt", "Nolan Arenado", "Corey Seager",
            "Adolis Garcia", "Teoscar Hernandez", "William Contreras",
        ],
        "odds": 450,   # +450 implied ~18%
    },
    "average": {
        "players": [],  # everyone else
        "odds": 650,   # +650 implied ~13%
    },
}

# Name aliases for Chalkline contestant map lookup
NAME_ALIASES = {
    "Jazz Chisholm Jr.": "Jazz Chisholm",
    "Elly De La Cruz":   "Elly de la Cruz",
    "José Ramírez":      "Jose Ramirez",
}

# ─── Core Functions ───────────────────────────────────────────────────────────

def load_contestant_map(path: Path = CONTESTANT_MAP_PATH) -> dict:
    """Load the Chalkline MLB contestant ID map from JSON."""
    if not path.exists():
        raise FileNotFoundError(
            f"Contestant map not found at {path}. "
            "Re-run the parsing script or update CONTESTANT_MAP_PATH."
        )
    with open(path) as f:
        return json.load(f)


def get_contestant_id(name: str, contestant_map: dict) -> str:
    """Return Chalkline contestant ID for a player name, or 'NULL'."""
    lookup = NAME_ALIASES.get(name, name)
    entry = contestant_map.get(lookup)
    return str(entry["id"]) if entry else "NULL"


def american_to_decimal(american: int) -> float:
    """Convert American odds to decimal, rounded to nearest 0.5."""
    if american > 0:
        dec = (american / 100) + 1
    else:
        dec = (100 / abs(american)) + 1
    return round(dec * 2) / 2


def estimate_odds(player_name: str) -> tuple[int, bool]:
    """
    Return (american_odds, is_estimated) for a player with no live line.
    Uses power tier lookup; always flags as estimated.
    """
    for tier in ("elite", "above_average"):
        if player_name in POWER_TIERS[tier]["players"]:
            return POWER_TIERS[tier]["odds"], True
    return POWER_TIERS["average"]["odds"], True


def filter_games_by_cutoff(
    fixtures: list[dict],
    cutoff_hour_ct: int,
    target_ct_date: "date | None" = None,
) -> list[dict]:
    """
    Return only fixtures that start at or after cutoff_hour_ct in Central Time,
    AND (if target_ct_date is provided) fall on that exact CT calendar date.

    The dual check is critical: early-morning UTC timestamps (e.g. 00:15Z, 01:40Z)
    belong to tonight CT (prior day) even though they fall on "tomorrow" UTC — without
    the date check they bleed into the wrong slate.

    CT = UTC-5 (CDT, April–October).
    """
    ct_offset = timedelta(hours=-5)
    results = []
    for fixture in fixtures:
        try:
            utc_dt = datetime.fromisoformat(fixture["start_date"].replace("Z", "+00:00"))
            ct_dt = utc_dt + ct_offset
            if ct_dt.hour >= cutoff_hour_ct:
                if target_ct_date is None or ct_dt.date() == target_ct_date:
                    results.append({**fixture, "_ct_start": ct_dt})
        except Exception as e:
            print(f"  Warning: could not parse start_date for fixture {fixture.get('id')}: {e}")
    return results


def build_player_rows(
    prop_data: list[dict],
    contestant_map: dict,
    top_n: int = TOP_N_PLAYERS,
) -> tuple[list[dict], list[str]]:
    """
    Build sorted player rows from raw prop data.

    prop_data: list of dicts with keys: name (str), american_odds (int), is_estimated (bool)
    Returns: (rows, warnings)
        rows: top_n players sorted by american_odds ascending
        warnings: list of warning strings (e.g. NULL contestant IDs)
    """
    if not prop_data:
        raise ValueError("No player prop data provided — cannot build rows.")

    # Sort by american odds ascending (most likely first)
    sorted_players = sorted(prop_data, key=lambda p: p["american_odds"])[:top_n]

    rows = []
    warnings = []
    for i, player in enumerate(sorted_players, start=1):
        name = player["name"]
        american = player["american_odds"]
        is_estimated = player.get("is_estimated", False)
        cid = get_contestant_id(name, contestant_map)

        if cid == "NULL":
            warnings.append(f"No contestant ID for: {name}")

        rows.append({
            "Odds Type":  ODDS_TYPE if i == 1 else "",
            "Book Name":  BOOK_NAME if i == 1 else "",
            "Market Name": name,
            "Contestant": cid,
            "Order":      i,
            "Odds":       f"{american_to_decimal(american):.4f}",
            "_estimated":      is_estimated,
            "_team":           player.get("team", ""),
            "_position":       player.get("position", ""),
            "_american_odds":  american,
        })

    return rows, warnings


def write_csv(rows: list[dict], output_path: Path) -> None:
    """Write player rows to CSV in Chalkline upload format."""
    fieldnames = ["Odds Type", "Book Name", "Market Name", "Contestant", "Order", "Odds"]
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def implied_pct(american: int) -> str:
    """Convert American odds to implied probability string (e.g. '27%')."""
    if american >= 0:
        pct = 100 / (american + 100)
    else:
        pct = abs(american) / (abs(american) + 100)
    return f"{round(pct * 100)}%"


def format_check_it_message(
    rows: list[dict],
    fixtures: list[dict],
    game_date: str,
) -> str:
    """Build the ranked HR candidate table for Slack."""
    # Build team → ("Away @ Home", "7:10 PM CT") lookup from fixtures
    team_to_game = {}
    team_to_time = {}
    for f in fixtures:
        away = f.get("away", "")
        home = f.get("home", "")
        label = f"{away} @ {home}"
        ct_start = f.get("_ct_start")
        if ct_start:
            try:
                time_str = ct_start.strftime("%-I:%M %p CT")
            except Exception:
                time_str = str(ct_start)
        else:
            time_str = "TBD"
        for team in (away, home):
            team_to_game[team] = label
            team_to_time[team] = time_str

    n_games = len(fixtures)
    game_word = "game" if n_games == 1 else "games"
    lines = [
        f"Here are the top {len(rows)} HR candidates across all {n_games} {game_word} tomorrow, ranked by likelihood:",
        "",
        "| Rank | Player | Game | Time | American | Implied % |",
        "|------|--------|------|------|----------|-----------|",
    ]

    for row in rows:
        rank = row["Order"]
        player = row["Market Name"]
        american = row.get("_american_odds", 0)
        team = row.get("_team", "")
        game = team_to_game.get(team, "—")
        time_str = team_to_time.get(team, "—")
        odds_str = f"+{american}" if american >= 0 else str(american)
        lines.append(f"| {rank} | {player} | {game} | {time_str} | {odds_str} | {implied_pct(american)} |")

    return "\n".join(lines)


def format_slack_message(
    rows: list[dict],
    warnings: list[str],
    game_date: str,
    games_included: list[str],
    has_estimated: bool,
) -> str:
    """Build a Slack message summarizing the daily HR Derby file."""
    lines = [
        f"*:baseball: HR Derby MLB — {game_date}*",
        f"File: `HR Derby MLB {game_date}.csv` — saved to Downloads",
        "",
    ]

    if len(games_included) < MIN_GAMES_WARNING:
        lines.append(
            f":rotating_light: *Thin slate alert — only {len(games_included)} game(s) tomorrow evening.* "
            "Player pool may be too small for a full HR Derby. Consider skipping or adjusting the game."
        )
        lines.append("")

    if has_estimated:
        lines.append(":warning: *Some odds are ESTIMATED* (sportsbooks haven't posted lines yet). Review before uploading.")
        lines.append("")

    lines.append("*Games included:*")
    for g in games_included:
        lines.append(f"  • {g}")
    lines.append("")

    lines.append(f"*Top {len(rows)} players:*")
    lines.append("```")
    lines.append(f"{'#':>2}  {'Player':<26} {'ID':>6}  {'Decimal':>7}")
    lines.append("-" * 51)
    for row in rows:
        lines.append(
            f"{row['Order']:>2}. {row['Market Name']:<26} {row['Contestant']:>6}  "
            f"{row['Odds']:>7}"
        )
    lines.append("```")

    if warnings:
        lines.append("")
        lines.append(":red_circle: *Needs attention:*")
        for w in warnings:
            lines.append(f"  • {w}")

    return "\n".join(lines)


# ─── Unit Tests ───────────────────────────────────────────────────────────────

def run_unit_tests() -> bool:
    """Run all unit tests. Returns True if all pass."""
    failures = []

    def assert_eq(label, actual, expected):
        if actual != expected:
            failures.append(f"FAIL [{label}]: expected {expected!r}, got {actual!r}")
        else:
            print(f"  PASS [{label}]")

    def assert_approx(label, actual, expected, tol=0.001):
        if abs(actual - expected) > tol:
            failures.append(f"FAIL [{label}]: expected ~{expected}, got {actual}")
        else:
            print(f"  PASS [{label}]")

    print("\n=== Running Unit Tests ===\n")

    # american_to_decimal
    print("-- american_to_decimal --")
    assert_eq("positive +267", american_to_decimal(267), 3.5)
    assert_eq("positive +332", american_to_decimal(332), 4.5)
    assert_eq("positive +525", american_to_decimal(525), 6.0)  # 6.25 rounds to 6.0 (banker's rounding)
    assert_eq("negative -149", american_to_decimal(-149), 1.5)
    assert_eq("negative -200", american_to_decimal(-200), 1.5)
    assert_eq("even +100", american_to_decimal(100), 2.0)
    assert_eq("rounds to nearest .5 +276", american_to_decimal(276), 4.0)
    assert_eq("rounds to nearest .5 +339", american_to_decimal(339), 4.5)

    # estimate_odds
    print("\n-- estimate_odds --")
    odds, est = estimate_odds("Aaron Judge")
    assert_eq("Judge tier odds", odds, 300)
    assert_eq("Judge is estimated", est, True)
    odds, est = estimate_odds("Brent Rooker")
    assert_eq("Rooker tier odds", odds, 450)
    odds, est = estimate_odds("Unknown Player XYZ")
    assert_eq("Unknown player falls to average", odds, 650)

    # get_contestant_id
    print("\n-- get_contestant_id --")
    mock_map = {
        "Aaron Judge": {"id": 4838, "team": "New York Yankees"},
        "Jazz Chisholm": {"id": 5045, "team": "New York Yankees"},
        "Elly de la Cruz": {"id": 6369, "team": "Cincinnati Reds"},
    }
    assert_eq("Direct match", get_contestant_id("Aaron Judge", mock_map), "4838")
    assert_eq("Alias Jr. suffix", get_contestant_id("Jazz Chisholm Jr.", mock_map), "5045")
    assert_eq("Alias case", get_contestant_id("Elly De La Cruz", mock_map), "6369")
    assert_eq("Missing player", get_contestant_id("Unknown Player", mock_map), "NULL")

    # filter_games_by_cutoff
    print("\n-- filter_games_by_cutoff --")
    from datetime import date as date_cls
    fixtures = [
        {"id": "A", "start_date": "2026-04-09T16:10:00Z"},  # 11:10 AM CT Apr 9 — before cutoff
        {"id": "B", "start_date": "2026-04-09T22:10:00Z"},  # 5:10 PM CT Apr 9  — before cutoff hour
        {"id": "C", "start_date": "2026-04-10T00:10:00Z"},  # 7:10 PM CT Apr 9  — right date? no: CT date=Apr 9, UTC date=Apr 10
        {"id": "D", "start_date": "2026-04-10T01:40:00Z"},  # 8:40 PM CT Apr 9  — same bleed-over
        {"id": "E", "start_date": "2026-04-10T23:10:00Z"},  # 6:10 PM CT Apr 10 — correct target
        {"id": "F", "start_date": "2026-04-10T23:45:00Z"},  # 6:45 PM CT Apr 10 — correct target
    ]
    # Without date filter: C and D bleed in (wrong CT date but hour >= 18)
    filtered_no_date = filter_games_by_cutoff(fixtures, cutoff_hour_ct=18)
    ids_no_date = [f["id"] for f in filtered_no_date]
    assert_eq("no date filter: C,D,E,F all pass hour check", ids_no_date, ["C", "D", "E", "F"])

    # With date filter for Apr 10 CT: C and D are excluded (they are Apr 9 CT)
    target = date_cls(2026, 4, 10)
    filtered = filter_games_by_cutoff(fixtures, cutoff_hour_ct=18, target_ct_date=target)
    ids = [f["id"] for f in filtered]
    assert_eq("date filter Apr 10 CT: keeps E,F only", ids, ["E", "F"])

    filtered_all = filter_games_by_cutoff(fixtures, cutoff_hour_ct=0)
    assert_eq("cutoff=0, no date: keeps all", len(filtered_all), 6)

    # build_player_rows
    print("\n-- build_player_rows --")
    mock_props = [
        {"name": "Aaron Judge",    "american_odds": 267, "is_estimated": False},
        {"name": "Riley Greene",   "american_odds": 339, "is_estimated": False},
        {"name": "Jazz Chisholm Jr.", "american_odds": 470, "is_estimated": True},
        {"name": "Unknown Player", "american_odds": 500, "is_estimated": True},
    ]
    rows, warnings = build_player_rows(mock_props, mock_map, top_n=3)
    assert_eq("top_n=3 returns 3 rows", len(rows), 3)
    assert_eq("row 1 is Judge", rows[0]["Market Name"], "Aaron Judge")
    assert_eq("row 1 odds", rows[0]["Odds"], "3.5000")
    assert_eq("alias resolved in ID", rows[2]["Contestant"], "5045")
    assert_eq("NULL warning raised", len(warnings) >= 1, True)

    # write_csv / read back
    print("\n-- write_csv --")
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="w") as tmp:
        tmp_path = Path(tmp.name)
    write_csv(rows, tmp_path)
    with open(tmp_path) as f:
        content = f.read()
    assert_eq("CSV contains header", "Market Name" in content, True)
    assert_eq("CSV contains Aaron Judge", "Aaron Judge" in content, True)
    assert_eq("CSV contains Championship Outright", "Championship Outright" in content, True)
    tmp_path.unlink()

    # build_player_rows — empty input raises
    print("\n-- exception handling --")
    try:
        build_player_rows([], mock_map)
        failures.append("FAIL [empty props]: should have raised ValueError")
    except ValueError:
        print("  PASS [empty props raises ValueError]")

    print(f"\n{'='*40}")
    if failures:
        print(f"FAILED: {len(failures)} test(s)\n")
        for f in failures:
            print(f"  {f}")
        return False
    else:
        print(f"ALL TESTS PASSED\n")
        return True


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Chalkline HR Derby CSV Generator")
    parser.add_argument("--date", default=None, help="Target date YYYY-MM-DD (default: tomorrow)")
    parser.add_argument("--cutoff-hour", type=int, default=18, help="CT hour cutoff (default 18 = 6 PM)")
    parser.add_argument("--output", default=str(OUTPUT_DIR), help="Output directory")
    parser.add_argument("--contestant-map", default=str(CONTESTANT_MAP_PATH), help="Path to contestant map JSON")
    parser.add_argument("--run-tests", action="store_true", help="Run unit tests and exit")
    args = parser.parse_args()

    if args.run_tests:
        success = run_unit_tests()
        sys.exit(0 if success else 1)

    target_date = args.date or (date.today() + timedelta(days=1)).isoformat()
    target_ct_date = datetime.strptime(target_date, "%Y-%m-%d").date()
    output_dir = Path(args.output)
    date_formatted = datetime.strptime(target_date, "%Y-%m-%d").strftime("%m-%d-%Y")
    output_path = output_dir / f"HR Derby MLB {date_formatted}.csv"

    print(f"HR Derby Generator — target date: {target_date}")
    print(f"Output: {output_path}\n")

    try:
        contestant_map = load_contestant_map(Path(args.contestant_map))
        print(f"Loaded {len(contestant_map)} players from contestant map.")
    except FileNotFoundError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    # NOTE: In production, fixture + prop data is injected by the Claude orchestrator
    # (which calls OpticOdds via MCP). This script receives it as JSON on stdin
    # or via --data-file argument when running in automated mode.
    #
    # For manual/test runs, use --run-tests or pass data via stdin:
    # echo '{"fixtures": [...], "props": [...]}' | python hr_derby_generator.py

    if not sys.stdin.isatty():
        try:
            data = json.load(sys.stdin)
            props = data.get("props", [])
            fixtures = data.get("fixtures", [])
            evening_fixtures = filter_games_by_cutoff(fixtures, args.cutoff_hour, target_ct_date)
            games_included = [
                f"{f.get('away')} @ {f.get('home')} — {f.get('_ct_start', f.get('start_date', ''))}"
                for f in evening_fixtures
            ]
        except json.JSONDecodeError as e:
            print(f"ERROR: Could not parse stdin JSON: {e}")
            sys.exit(1)
    else:
        print("No data provided via stdin. Run with --run-tests or pipe in fixture/prop JSON.")
        print("In automated mode, the Claude orchestrator injects data via stdin.")
        sys.exit(0)

    if not props:
        print("WARNING: No props in input data — nothing to generate.")
        sys.exit(0)

    has_estimated = any(p.get("is_estimated") for p in props)
    rows, warnings = build_player_rows(props, contestant_map)
    write_csv(rows, output_path)

    print(f"CSV written: {output_path}")
    print(f"Players: {len(rows)} | Warnings: {len(warnings)} | Estimated odds: {has_estimated}")
    for w in warnings:
        print(f"  ! {w}")

    slack_msg = format_slack_message(rows, warnings, date_formatted, games_included, has_estimated)
    check_it_msg = format_check_it_message(rows, evening_fixtures, date_formatted)
    print("\n--- Slack Message Preview ---")
    print(slack_msg)
    print("--- Check It Preview ---")
    print(check_it_msg)
    print("--- End Preview ---")

    with open(output_path) as f:
        csv_content = f.read()

    # Output for Claude orchestrator to pick up and post to Slack
    result = {
        "output_path": str(output_path),
        "csv_content": csv_content,
        "slack_message": slack_msg,
        "check_it_message": check_it_msg,
        "player_count": len(rows),
        "game_count": len(games_included),
        "low_game_count": len(games_included) < MIN_GAMES_WARNING,
        "warnings": warnings,
        "has_estimated_odds": has_estimated,
    }
    print("\n__RESULT__")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
