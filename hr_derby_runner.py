#!/usr/bin/env python3
"""
Standalone HR Derby runner for GitHub Actions.

Modes:
  --lineup      Generate lineup for tomorrow and post to Slack (Mon/Fri scheduled)
  --results-only  Post yesterday's results (Wed/Sun scheduled)
  default         Poll Slack for !run hr-derby trigger (hourly break-glass)
"""
import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from datetime import datetime, timezone, timedelta, date as date_cls
from pathlib import Path

import requests

# ── Config ─────────────────────────────────────────────────────────────────────
SLACK_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
OPTICODDS_KEY = os.environ.get("OPTICODDS_API_KEY", "")
CHANNEL_ID = "C0APGR57MLJ"
CT_OFFSET = timedelta(hours=-5)  # CDT April–October
SCRIPT_DIR = Path(__file__).parent
GENERATOR_SCRIPT = SCRIPT_DIR / "hr_derby_generator.py"
CONTESTANT_MAP = SCRIPT_DIR / "mlb_contestant_map.json"

DRY_RUN = False

# Player selection is driven entirely by actual season home-run totals from the
# MLB Stats API (see get_hr_leaders / build_props). Hard-coded power tiers were
# removed: they let retired/injured players and weak hitters onto the card.
PITCHER_POSITIONS = {"P", "SP", "RP"}
MIN_HR_FOR_MAP_WARNING = 8   # Flag missing-from-map sluggers at/above this HR total
MAX_PER_TEAM = 4             # Pool cap per team (generator applies the final 3-per-team card cap)
POOL_SIZE = 60               # How many ranked hitters to hand the generator

NAME_ALIASES = {
    "Jazz Chisholm Jr.": "Jazz Chisholm",
    "Elly De La Cruz": "Elly de la Cruz",
    "José Ramírez": "Jose Ramirez",
}


# ── Name / status normalization ──────────────────────────────────────────────────
def normalize_name(name):
    """Lowercase, strip accents, collapse whitespace — for tolerant matching."""
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.lower().split())


def normalize_team(team):
    """Normalize a team name for comparison (accent/case/punctuation tolerant)."""
    return normalize_name(team).replace(".", "")


def build_name_index(contestant_map):
    """Map normalized player name -> (canonical_name, entry) for tolerant lookup."""
    index = {}
    for canonical, entry in contestant_map.items():
        index[normalize_name(canonical)] = (canonical, entry)
    for alias, target in NAME_ALIASES.items():
        if target in contestant_map:
            index.setdefault(normalize_name(alias), (target, contestant_map[target]))
    return index


def resolve_contestant(name, contestant_map, name_index):
    """Resolve a StatsAPI player name to (canonical_name, entry) or (None, None)."""
    if name in contestant_map:
        return name, contestant_map[name]
    alias = NAME_ALIASES.get(name)
    if alias and alias in contestant_map:
        return alias, contestant_map[alias]
    norm = normalize_name(name)
    if norm in name_index:
        return name_index[norm]
    stripped = re.sub(r"\b(jr|sr|ii|iii|iv)\.?$", "", norm).strip()
    if stripped and stripped in name_index:
        return name_index[stripped]
    return None, None


# Statuses that mean a player is NOT available tonight. The old code only caught
# "out"/"doubtful" and let every Injured List variant slip through.
_OUT_KEYWORDS = ("out", "doubtful", "suspended", "restricted", "paternity", "bereavement")
_AVAILABLE_STATUSES = {
    "active", "probable", "available", "questionable",
    "game time decision", "game-time decision", "",
}


def is_unavailable(status):
    """
    True if an injury status means the player should be excluded from the card.

    The live OpticOdds MLB feed uses machine slugs, not prose:
        "out", "il_60-day", "il_15-day", "il_7-day", "suspended"
    We match those explicitly (not by coincidence), and also tolerate the
    human-readable variants in case the feed format ever changes.
    """
    s = (status or "").strip().lower()
    if s in _AVAILABLE_STATUSES:
        return False
    # OpticOdds slug form: il_60-day, il_15-day, il_7-day, il, il_10-day ...
    if s == "il" or s.startswith("il_") or s.startswith("il-"):
        return True
    # Human-readable / other feeds: "10-Day IL", "Injured List", "Day-To-Day"
    if "injured list" in s or "-day" in s or re.search(r"\bil\b", s) or re.search(r"\bir\b", s):
        return True
    if "day-to-day" in s or "day to day" in s:
        return True
    return any(k in s for k in _OUT_KEYWORDS)


def hr_to_display_odds(hr):
    """Estimated American odds purely for the player-facing table (CSV odds are flat)."""
    if hr >= 20:
        return 250
    if hr >= 15:
        return 325
    if hr >= 10:
        return 425
    if hr >= 6:
        return 550
    return 700


# ── Slack helpers ──────────────────────────────────────────────────────────────
def slack_get(endpoint, params):
    r = requests.get(
        f"https://slack.com/api/{endpoint}",
        headers={"Authorization": f"Bearer {SLACK_TOKEN}"},
        params=params,
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def slack_post(text, thread_ts=None):
    if DRY_RUN:
        prefix = "[DRY-RUN] "
        if thread_ts:
            prefix += f"(thread {thread_ts}) "
        print(f"{prefix}{text}")
        return {"ok": True, "ts": "0000000000.000000"}
    payload = {"channel": CHANNEL_ID, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    r = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {SLACK_TOKEN}"},
        json=payload,
        timeout=10,
    )
    r.raise_for_status()
    resp = r.json()
    if not resp.get("ok"):
        raise RuntimeError(f"Slack post failed: {resp.get('error')}")
    return resp


# ── Trigger detection ──────────────────────────────────────────────────────────
def find_trigger():
    """Return the oldest !run hr-derby message posted in the last 65 min, or None."""
    cutoff = time.time() - 3900
    data = slack_get("conversations.history", {"channel": CHANNEL_ID, "limit": 20})
    for msg in reversed(data.get("messages", [])):
        if float(msg["ts"]) >= cutoff and "!run hr-derby" in msg.get("text", "").lower():
            return msg
    return None


def find_results_trigger():
    """Return the oldest !results message in the last 65 min, or None."""
    import re
    cutoff = time.time() - 3900
    data = slack_get("conversations.history", {"channel": CHANNEL_ID, "limit": 20})
    for msg in reversed(data.get("messages", [])):
        text = msg.get("text", "").strip()
        if float(msg["ts"]) < cutoff:
            continue
        match = re.match(r"!results(?:\s+(\d{4}-\d{2}-\d{2}))?\s*$", text, re.IGNORECASE)
        if match:
            msg["_results_date"] = match.group(1)
            return msg
    return None


def already_handled(trigger_ts):
    """True if this thread already has a job status reply."""
    data = slack_get("conversations.replies", {"channel": CHANNEL_ID, "ts": trigger_ts})
    sentinels = ("Starting HR Derby", "HR Derby job complete", "HR Derby job failed",
                 "Posting HR Derby results", "Results posted", "Results failed")
    return any(
        any(s in r.get("text", "") for s in sentinels)
        for r in data.get("messages", [])
    )


# ── OpticOdds REST API ─────────────────────────────────────────────────────────
def get_fixtures(date_str):
    r = requests.get(
        "https://api.opticodds.com/api/v3/fixtures",
        headers={"x-api-key": OPTICODDS_KEY},
        params={"sport": "baseball", "league": "MLB", "start_date": date_str},
        timeout=15,
    )
    r.raise_for_status()
    return r.json().get("data", [])


def get_injuries(max_pages=25):
    """
    Fetch the FULL MLB injury report, following pagination.

    The OpticOdds feed is paged (`has_more: true`); fetching only page 1 (the old
    behavior) silently missed injured hitters on later pages, so they could still
    land on the card. We walk every page until has_more is false.
    """
    injuries = []
    page = 1
    while page <= max_pages:
        r = requests.get(
            "https://api.opticodds.com/api/v3/injuries",
            headers={"x-api-key": OPTICODDS_KEY},
            params={"sport": "baseball", "league": "MLB", "page": page},
            timeout=15,
        )
        r.raise_for_status()
        body = r.json()
        data = body.get("data", [])
        injuries.extend(data)
        if not body.get("has_more") or not data:
            break
        page += 1
    return injuries


def get_hr_leaders(season):
    """
    Fetch every hitter's season home-run total from the MLB Stats API.
    Returns a list of {name, hr, team, position} dicts, HR desc.

    This is the authoritative, key-free source that drives selection. The
    'hitting' group naturally excludes pitchers, and players with no 2026 plate
    appearances (retired/released, e.g. Anthony Rendon) simply never appear.
    """
    r = requests.get(
        "https://statsapi.mlb.com/api/v1/stats",
        params={
            "stats": "season", "group": "hitting", "gameType": "R",
            "season": str(season), "sportId": 1,
            "limit": 500, "sortStat": "homeRuns", "order": "desc",
        },
        timeout=15,
    )
    r.raise_for_status()
    leaders = []
    for group in r.json().get("stats", []):
        for split in group.get("splits", []):
            leaders.append({
                "name": split.get("player", {}).get("fullName", ""),
                "hr": split.get("stat", {}).get("homeRuns", 0) or 0,
                "team": split.get("team", {}).get("name", ""),
                "position": split.get("position", {}).get("abbreviation", ""),
            })
    return leaders


def normalize_fixture(f):
    return {
        "id": f["id"],
        "start_date": f["start_date"],
        "home": f.get("home_team_display") or f.get("home", ""),
        "away": f.get("away_team_display") or f.get("away", ""),
    }


def filter_evening(fixtures, target_ct_date):
    result = []
    for f in fixtures:
        utc_dt = datetime.fromisoformat(f["start_date"].replace("Z", "+00:00"))
        ct_dt = utc_dt + CT_OFFSET
        if ct_dt.date() == target_ct_date and ct_dt.hour >= 18:
            result.append(f)
    return result


# ── Player pool ────────────────────────────────────────────────────────────────
def build_props(teams_in_slate, out_player_names, contestant_map, hr_leaders,
                name_index=None, pool_size=POOL_SIZE, max_per_team=MAX_PER_TEAM):
    """
    Build the candidate pool from real season HR leaders.

    Filters, in order: must be on a slate team, must not be a pitcher, must not be
    injured/IL, must resolve to a Chalkline contestant ID. Ranks by HR desc and
    caps per team so a single roster cannot flood the pool.

    Returns (props, missing_from_map):
        props: list of dicts {name, hr, team, position, american_odds, is_estimated}
        missing_from_map: notable sluggers with no contestant ID (so the map gets fixed)
    """
    if name_index is None:
        name_index = build_name_index(contestant_map)

    slate_norm = {normalize_team(t) for t in teams_in_slate}
    out_norm = {normalize_name(n) for n in out_player_names}

    props = []
    missing_from_map = []
    seen_ids = set()
    team_counts = {}

    for entry in hr_leaders:  # already HR desc from the API
        if len(props) >= pool_size:
            break
        name = entry.get("name", "")
        team = entry.get("team", "")
        hr = entry.get("hr", 0)
        position = entry.get("position", "")

        if normalize_team(team) not in slate_norm:
            continue
        if position in PITCHER_POSITIONS:
            continue
        if normalize_name(name) in out_norm:
            continue

        canonical, cmap_entry = resolve_contestant(name, contestant_map, name_index)
        if not cmap_entry:
            if hr >= MIN_HR_FOR_MAP_WARNING:
                missing_from_map.append(f"{name} ({team}, {hr} HR)")
            continue

        cid = str(cmap_entry["id"])
        if cid in seen_ids:
            continue
        if max_per_team and team_counts.get(team, 0) >= max_per_team:
            continue

        seen_ids.add(cid)
        team_counts[team] = team_counts.get(team, 0) + 1
        props.append({
            "name": canonical,
            "hr": hr,
            "team": team,
            "position": position,
            "american_odds": hr_to_display_odds(hr),
            "is_estimated": True,
        })

    return props, missing_from_map


# ── Yesterday's results ────────────────────────────────────────────────────────
def result_yesterday(yesterday_ct):
    mm_dd_yyyy = yesterday_ct.strftime("%m-%d-%Y")
    csv_path = SCRIPT_DIR / f"HR Derby MLB {mm_dd_yyyy}.csv"
    if not csv_path.exists():
        return

    players = []
    with open(csv_path, newline="") as f:
        for row in csv.DictReader(f):
            name = row.get("Market Name", "").strip()
            if name:
                players.append(name)
    if not players:
        return

    date_api = yesterday_ct.strftime("%m/%d/%Y")
    r = requests.get(
        "https://statsapi.mlb.com/api/v1/stats",
        params={
            "stats": "byDateRange", "group": "hitting", "gameType": "R",
            "startDate": date_api, "endDate": date_api,
            "season": str(yesterday_ct.year), "sportId": 1,
            "limit": 500, "sortStat": "homeRuns", "order": "desc",
        },
        timeout=15,
    )
    hr_hitters = {}
    total_hrs = 0
    for group in r.json().get("stats", []):
        for split in group.get("splits", []):
            hrs = split.get("stat", {}).get("homeRuns", 0)
            if hrs > 0:
                name = split["player"]["fullName"]
                team = split.get("team", {}).get("abbreviation", "")
                # Key on a normalized name so accented StatsAPI names (e.g.
                # "José Ramírez") still match the ASCII names on our card.
                hr_hitters[normalize_name(name)] = (hrs, team)
                total_hrs += hrs

    r2 = requests.get(
        "https://statsapi.mlb.com/api/v1/schedule",
        params={"sportId": 1, "date": date_api},
        timeout=15,
    )
    game_count = r2.json().get("totalGames", 0)

    won, lost = [], []
    for player in players:
        key = normalize_name(player)
        if key in hr_hitters:
            _, team = hr_hitters[key]
            won.append(f"{player} ({team})")
        else:
            lost.append(player)

    day_label = yesterday_ct.strftime("%a, %b %-d")
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
    slack_post("\n".join(lines))


# ── Thin-slate look-ahead ──────────────────────────────────────────────────────
_CUTOFF_LABELS = {13: "1:00 PM CT", 12: "12:00 PM CT", 11: "11:00 AM CT", 10: "10:00 AM CT", 0: "all day"}


def suggest_cutoff(fixtures, target_ct_date):
    for cutoff in (13, 12, 11, 10, 0):
        games = [
            f for f in fixtures
            if (datetime.fromisoformat(f["start_date"].replace("Z", "+00:00")) + CT_OFFSET).date() == target_ct_date
            and (datetime.fromisoformat(f["start_date"].replace("Z", "+00:00")) + CT_OFFSET).hour >= cutoff
        ]
        if len(games) >= 5:
            return _CUTOFF_LABELS[cutoff], games
    return None


def check_thin_slates(today_ct):
    thin = []
    for delta in range(2, 6):
        check_date = today_ct + timedelta(days=delta)
        try:
            raw = get_fixtures(check_date.isoformat())
            fixtures = [normalize_fixture(f) for f in raw]
            evening = filter_evening(fixtures, check_date)
            if len(evening) < 5:
                word = "game" if len(evening) == 1 else "games"
                line = f"• {check_date.strftime('%a %b %-d')}: only {len(evening)} evening {word}"
                suggestion = suggest_cutoff(fixtures, check_date)
                if suggestion:
                    cutoff_label, games = suggestion
                    line += f" — move start time to *{cutoff_label}* for {len(games)} games"
                else:
                    line += " — not enough games even all day, consider skipping"
                thin.append(line)
        except Exception:
            pass
    if thin:
        slack_post(":calendar: *Heads up — thin MLB slates ahead:*\n" + "\n".join(thin))


# ── Main job ───────────────────────────────────────────────────────────────────
def run_job(trigger_ts=None):
    now_utc = datetime.now(timezone.utc)
    now_ct = now_utc + CT_OFFSET
    yesterday_ct = (now_ct - timedelta(days=1)).date()
    tomorrow_ct = (now_ct + timedelta(days=1)).date()
    day_after_ct = (now_ct + timedelta(days=2)).date()

    try:
        result_yesterday(yesterday_ct)
    except Exception as e:
        print(f"Warning: result_yesterday failed: {e}", file=sys.stderr)

    raw_fixtures = []
    for d in (tomorrow_ct, day_after_ct):
        try:
            raw_fixtures.extend(get_fixtures(d.isoformat()))
        except Exception as e:
            print(f"Warning: get_fixtures({d}) failed: {e}", file=sys.stderr)

    seen_ids = set()
    all_fixtures = []
    for f in raw_fixtures:
        if f["id"] not in seen_ids:
            seen_ids.add(f["id"])
            all_fixtures.append(normalize_fixture(f))

    evening_fixtures = filter_evening(all_fixtures, tomorrow_ct)
    cutoff_label = None
    if len(evening_fixtures) < 5:
        suggestion = suggest_cutoff(all_fixtures, tomorrow_ct)
        if suggestion:
            cutoff_label, evening_fixtures = suggestion
        else:
            evening_fixtures = all_fixtures

    if not evening_fixtures:
        slack_post(f":warning: No MLB games found for {tomorrow_ct.isoformat()} — skipping HR Derby.")
        if trigger_ts:
            slack_post(":white_check_mark: HR Derby job complete — no games to run.", thread_ts=trigger_ts)
        return

    out_players = set()
    try:
        for inj in get_injuries():
            if is_unavailable(inj.get("status", "")):
                out_players.add(inj["player"]["name"])
    except Exception as e:
        print(f"Warning: get_injuries failed: {e}", file=sys.stderr)

    teams_in_slate = {f["home"] for f in evening_fixtures} | {f["away"] for f in evening_fixtures}

    with open(CONTESTANT_MAP) as fh:
        contestant_map = json.load(fh)

    try:
        hr_leaders = get_hr_leaders(tomorrow_ct.year)
    except Exception as e:
        raise RuntimeError(f"Could not fetch MLB HR leaders — refusing to post a non-data-driven card: {e}")

    props, missing_from_map = build_props(teams_in_slate, out_players, contestant_map, hr_leaders)
    if not props:
        raise RuntimeError(
            "Could not build a player pool — no HR-ranked hitters matched tomorrow's "
            "teams (after pitcher/injury/contestant-map filtering)."
        )

    input_data = json.dumps({"fixtures": evening_fixtures, "props": props})
    proc = subprocess.run(
        [
            sys.executable, str(GENERATOR_SCRIPT),
            "--date", tomorrow_ct.isoformat(),
            "--contestant-map", str(CONTESTANT_MAP),
            "--output", str(SCRIPT_DIR),
        ],
        input=input_data,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"Generator failed:\n{proc.stderr}\n{proc.stdout[-500:]}")

    result_data = None
    stdout = proc.stdout
    marker = "__RESULT__"
    if marker in stdout:
        result_json = stdout[stdout.index(marker) + len(marker):].strip()
        result_data = json.loads(result_json)

    if not result_data:
        raise RuntimeError(f"No __RESULT__ in generator output:\n{stdout[-500:]}")

    fallback_note = f"\n_Thin slate: using games from *{cutoff_label}* onward ({len(evening_fixtures)} games)._" if cutoff_label else ""
    slack_post(result_data["check_it_message"] + fallback_note)
    slack_post(result_data["slack_message"])

    csv_content = result_data.get("csv_content", "")
    if csv_content:
        date_fmt = tomorrow_ct.strftime("%m-%d-%Y")
        slack_post(f":paperclip: _HR Derby MLB {date_fmt}.csv_ — upload-ready:\n```{csv_content}```")

    if missing_from_map:
        slack_post(
            ":red_circle: *HR hitters missing from the contestant map* — add their IDs so "
            "they can be used on future cards:\n" + "\n".join(f"• {m}" for m in missing_from_map)
        )

    try:
        check_thin_slates(now_ct.date())
    except Exception as e:
        print(f"Warning: thin-slate check failed: {e}", file=sys.stderr)


def run_results_only(target_date=None):
    if target_date:
        try:
            results_date = datetime.strptime(target_date, "%Y-%m-%d").date()
        except ValueError:
            print(f"ERROR: Invalid date format '{target_date}'. Use YYYY-MM-DD.", file=sys.stderr)
            sys.exit(1)
    else:
        now_utc = datetime.now(timezone.utc)
        now_ct = now_utc + CT_OFFSET
        results_date = (now_ct - timedelta(days=1)).date()
    print(f"Checking results for {results_date.strftime('%m-%d-%Y')}...")

    mm_dd_yyyy = results_date.strftime("%m-%d-%Y")
    csv_path = SCRIPT_DIR / f"HR Derby MLB {mm_dd_yyyy}.csv"
    if not csv_path.exists():
        msg = f":baseball: No HR Derby CSV found for {results_date.strftime('%a, %b %-d')} — no derby was running."
        slack_post(msg)
        print(f"No CSV found at {csv_path}")
        return

    try:
        result_yesterday(results_date)
        print("Results posted successfully.")
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"Error posting results: {e}", file=sys.stderr)
        print(tb, file=sys.stderr)
        sys.exit(1)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="HR Derby Runner")
    parser.add_argument("--lineup", action="store_true",
                        help="Generate lineup for tomorrow and post to Slack (no Slack trigger needed)")
    parser.add_argument("--results-only", action="store_true",
                        help="Post yesterday's results and exit")
    parser.add_argument("--date", default=None,
                        help="Target date YYYY-MM-DD. With --results-only: date to post results for. "
                             "With --lineup: override tomorrow's date.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print to stdout instead of posting to Slack")
    return parser.parse_args(argv)


def main(argv=None):
    global DRY_RUN
    args = parse_args(argv)

    if args.dry_run:
        DRY_RUN = True

    if args.results_only:
        run_results_only(target_date=args.date)
        return

    if args.lineup:
        try:
            run_job(trigger_ts=None)
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            print(tb, file=sys.stderr)
            slack_post(f":x: HR Derby lineup failed: {e}\n```{tb[-800:]}```")
            sys.exit(1)
        return

    # Hourly break-glass: poll Slack for triggers
    results_trigger = find_results_trigger()
    if results_trigger:
        trigger_ts = results_trigger["ts"]
        if not already_handled(trigger_ts):
            slack_post(":hourglass_flowing_sand: Posting HR Derby results…", thread_ts=trigger_ts)
            try:
                run_results_only(target_date=results_trigger.get("_results_date"))
                slack_post(":white_check_mark: Results posted.", thread_ts=trigger_ts)
            except Exception as e:
                import traceback
                tb = traceback.format_exc()
                slack_post(f":x: Results failed: {e}\n```{tb[-800:]}```", thread_ts=trigger_ts)
        return

    trigger = find_trigger()
    if not trigger:
        sys.exit(0)

    trigger_ts = trigger["ts"]
    if already_handled(trigger_ts):
        sys.exit(0)

    slack_post(":hourglass_flowing_sand: Starting HR Derby job…", thread_ts=trigger_ts)
    try:
        run_job(trigger_ts)
        slack_post(":white_check_mark: HR Derby job complete — CSV posted to Slack.", thread_ts=trigger_ts)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        slack_post(f":x: HR Derby job failed: {e}\n```{tb[-800:]}```", thread_ts=trigger_ts)


if __name__ == "__main__":
    main()
