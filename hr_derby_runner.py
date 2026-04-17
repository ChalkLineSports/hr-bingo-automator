#!/usr/bin/env python3
"""
Standalone HR Derby runner for GitHub Actions.

Polls Slack for !run hr-derby trigger, then orchestrates the full job:
  - Checks OpticOdds for tomorrow's fixtures and injuries
  - Builds a tier-based player pool (live prop odds blocked server-side)
  - Runs hr_derby_generator.py and posts results to Slack
"""
import csv
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta, date as date_cls
from pathlib import Path

import requests

# ── Config ─────────────────────────────────────────────────────────────────────
SLACK_TOKEN = os.environ["SLACK_BOT_TOKEN"]
OPTICODDS_KEY = os.environ["OPTICODDS_API_KEY"]
CHANNEL_ID = "C0APGR57MLJ"
CT_OFFSET = timedelta(hours=-5)  # CDT April–October
SCRIPT_DIR = Path(__file__).parent
GENERATOR_SCRIPT = SCRIPT_DIR / "hr_derby_generator.py"
CONTESTANT_MAP = SCRIPT_DIR / "mlb_contestant_map.json"

# ── Tier lists (keep in sync with hr_derby_generator.py POWER_TIERS) ──────────
ELITE_PLAYERS = {
    "Aaron Judge", "Shohei Ohtani", "Giancarlo Stanton", "Pete Alonso",
    "Kyle Schwarber", "Matt Olson", "Yordan Alvarez", "Vladimir Guerrero Jr.",
    "Gunnar Henderson", "Bobby Witt Jr.", "Marcell Ozuna", "Freddie Freeman",
    "Fernando Tatis Jr.", "Jose Ramirez",
}
ABOVE_AVERAGE_PLAYERS = {
    "Riley Greene", "Junior Caminero", "Ben Rice", "Jonathan Aranda",
    "Jazz Chisholm", "Brent Rooker", "Nick Kurtz", "Trent Grisham",
    "Spencer Torkelson", "Elly de la Cruz", "Pete Crow-Armstrong", "Michael Busch",
    "Shea Langeliers", "Eugenio Suarez", "Agustin Ramirez", "Alex Bregman",
    "Ryan McMahon", "Colt Keith", "Kerry Carpenter", "Byron Buxton",
    "Jackson Chourio", "Sal Stewart", "Jackson Merrill", "Bryce Harper",
    "Manny Machado", "Trea Turner", "Paul Goldschmidt", "Nolan Arenado",
    "Corey Seager", "Adolis Garcia", "Teoscar Hernandez", "William Contreras",
}
# Aliases used by the generator's NAME_ALIASES — normalize to canonical form here
NAME_ALIASES = {
    "Jazz Chisholm Jr.": "Jazz Chisholm",
    "Elly De La Cruz": "Elly de la Cruz",
    "José Ramírez": "Jose Ramirez",
}


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
    """Return the oldest !run hr-derby message posted in the last 10 min, or None."""
    cutoff = time.time() - 600
    data = slack_get("conversations.history", {"channel": CHANNEL_ID, "limit": 20})
    for msg in reversed(data.get("messages", [])):  # oldest first
        if float(msg["ts"]) >= cutoff and "!run hr-derby" in msg.get("text", "").lower():
            return msg
    return None


def already_handled(trigger_ts):
    """True if this thread already has a job status reply (idempotency guard)."""
    data = slack_get("conversations.replies", {"channel": CHANNEL_ID, "ts": trigger_ts})
    sentinels = ("Starting HR Derby", "HR Derby job complete", "HR Derby job failed")
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


def get_injuries():
    r = requests.get(
        "https://api.opticodds.com/api/v3/injuries",
        headers={"x-api-key": OPTICODDS_KEY},
        params={"sport": "baseball", "league": "MLB"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json().get("data", [])


def normalize_fixture(f):
    """Map REST API fixture fields to the shape hr_derby_generator.py expects."""
    return {
        "id": f["id"],
        "start_date": f["start_date"],
        "home": f.get("home_team_display") or f.get("home", ""),
        "away": f.get("away_team_display") or f.get("away", ""),
    }


def filter_evening(fixtures, target_ct_date):
    """Keep fixtures where CT start >= 18:00 on target_ct_date."""
    result = []
    for f in fixtures:
        utc_dt = datetime.fromisoformat(f["start_date"].replace("Z", "+00:00"))
        ct_dt = utc_dt + CT_OFFSET
        if ct_dt.date() == target_ct_date and ct_dt.hour >= 18:
            result.append(f)
    return result


# ── Player pool ────────────────────────────────────────────────────────────────
def build_props(teams_in_slate, out_player_names, contestant_map):
    """
    Build props for tier players on teams in the slate, excluding injured players.
    Always uses tier estimates (live prop-lines blocked server-side by Cloudflare).
    Falls back to average-tier (+650) from the contestant map to fill 25 slots.
    """
    out_lower = {n.lower() for n in out_player_names}
    seen = set()
    props = []

    for player_set, odds in ((ELITE_PLAYERS, 300), (ABOVE_AVERAGE_PLAYERS, 450)):
        for name in player_set:
            canonical = NAME_ALIASES.get(name, name)
            if canonical in seen or canonical.lower() in out_lower:
                continue
            entry = contestant_map.get(canonical)
            if not entry or entry.get("team") not in teams_in_slate:
                continue
            seen.add(canonical)
            props.append({"name": canonical, "american_odds": odds, "is_estimated": True, "team": entry["team"]})

    # Fill remaining slots with average-tier players from the contestant map
    if len(props) < 35:
        added = {p["name"] for p in props}
        for name, entry in contestant_map.items():
            if len(props) >= 50:
                break
            if entry.get("team") not in teams_in_slate:
                continue
            if name in added or name.lower() in out_lower:
                continue
            props.append({"name": name, "american_odds": 650, "is_estimated": True, "team": entry["team"]})
            added.add(name)

    return props


# ── Yesterday's results ────────────────────────────────────────────────────────
def result_yesterday(yesterday_ct):
    """
    Post yesterday's HR Derby results if a CSV from that date exists in the repo.
    The CSV is committed after each successful run (see GitHub Actions workflow).
    """
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
                hr_hitters[name.lower()] = (hrs, team)
                total_hrs += hrs

    r2 = requests.get(
        "https://statsapi.mlb.com/api/v1/schedule",
        params={"sportId": 1, "date": date_api},
        timeout=15,
    )
    game_count = r2.json().get("totalGames", 0)

    won, lost = [], []
    for player in players:
        if player.lower() in hr_hitters:
            _, team = hr_hitters[player.lower()]
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
def suggest_cutoff(fixtures, target_ct_date):
    """
    Given all fixtures for a date, find the earliest CT hour cutoff that
    yields >= 5 games. Returns (cutoff_hour, game_count) or None if even
    all-day games don't reach 5.
    """
    for cutoff in (13, 12, 11, 10, 0):
        games = [
            f for f in fixtures
            if (datetime.fromisoformat(f["start_date"].replace("Z", "+00:00")) + CT_OFFSET).date() == target_ct_date
            and (datetime.fromisoformat(f["start_date"].replace("Z", "+00:00")) + CT_OFFSET).hour >= cutoff
        ]
        if len(games) >= 5:
            label = f"{cutoff}:00 PM CT" if cutoff >= 12 else f"{cutoff}:00 AM CT"
            if cutoff == 13:
                label = "1:00 PM CT"
            elif cutoff == 12:
                label = "12:00 PM CT"
            elif cutoff == 11:
                label = "11:00 AM CT"
            elif cutoff == 10:
                label = "10:00 AM CT"
            elif cutoff == 0:
                label = "all day"
            return label, len(games)
    return None


def check_thin_slates(today_ct):
    """Alert if any of days +2 through +5 has fewer than 5 evening games,
    and suggest an earlier start time that would reach 5+ games."""
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
                    cutoff_label, count = suggestion
                    line += f" — move start time to *{cutoff_label}* for {count} games"
                else:
                    line += " — not enough games even all day, consider skipping"
                thin.append(line)
        except Exception:
            pass
    if thin:
        slack_post(":calendar: *Heads up — thin MLB slates ahead:*\n" + "\n".join(thin))


# ── Main job ───────────────────────────────────────────────────────────────────
def run_job(trigger_ts):
    now_utc = datetime.now(timezone.utc)
    now_ct = now_utc + CT_OFFSET
    yesterday_ct = (now_ct - timedelta(days=1)).date()
    tomorrow_ct = (now_ct + timedelta(days=1)).date()
    day_after_ct = (now_ct + timedelta(days=2)).date()

    # 5a — result yesterday's game
    try:
        result_yesterday(yesterday_ct)
    except Exception as e:
        print(f"Warning: result_yesterday failed: {e}", file=sys.stderr)

    # 5c — fetch fixtures for tomorrow + next UTC day (catches late-night bleed-over)
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
    fallback = len(evening_fixtures) < 5
    if fallback:
        evening_fixtures = all_fixtures

    if not evening_fixtures:
        slack_post(f":warning: No MLB games found for {tomorrow_ct.isoformat()} — skipping HR Derby.")
        slack_post(":white_check_mark: HR Derby job complete — no games to run.", thread_ts=trigger_ts)
        return

    # 5d — injuries
    out_players = set()
    try:
        for inj in get_injuries():
            if inj.get("status", "").lower() in ("out", "doubtful"):
                out_players.add(inj["player"]["name"])
    except Exception as e:
        print(f"Warning: get_injuries failed: {e}", file=sys.stderr)

    # 5e — no live odds available server-side (Cloudflare blocks prop-lines endpoint)
    # Always use tier estimates

    # Teams on tomorrow's slate
    teams_in_slate = {f["home"] for f in evening_fixtures} | {f["away"] for f in evening_fixtures}

    with open(CONTESTANT_MAP) as fh:
        contestant_map = json.load(fh)

    # 5f — build props
    props = build_props(teams_in_slate, out_players, contestant_map)
    if not props:
        raise RuntimeError("Could not build a player pool — no tier players matched tomorrow's teams.")

    # 5g — run generator
    input_data = json.dumps({"fixtures": evening_fixtures, "props": props})
    proc = subprocess.run(
        [
            sys.executable, str(GENERATOR_SCRIPT),
            "--date", tomorrow_ct.isoformat(),
            "--contestant-map", str(CONTESTANT_MAP),
            "--output", "/tmp",
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

    # 5h — post to Slack
    fallback_note = (
        f"\n_Fallback: fewer than 5 evening games on {tomorrow_ct.isoformat()} CT — using all day's games._"
        if fallback else ""
    )
    slack_post(result_data["check_it_message"] + fallback_note)
    slack_post(result_data["slack_message"])

    csv_content = result_data.get("csv_content", "")
    if csv_content:
        date_fmt = tomorrow_ct.strftime("%m-%d-%Y")
        slack_post(f":paperclip: _HR Derby MLB {date_fmt}.csv_ — upload-ready:\n```{csv_content}```")

    # Commit CSV to repo so tomorrow's run can post results
    # (handled by the GitHub Actions workflow post-step)

    # 5i — look-ahead thin slates
    try:
        check_thin_slates(now_ct.date())
    except Exception as e:
        print(f"Warning: thin-slate check failed: {e}", file=sys.stderr)


def main():
    trigger = find_trigger()
    if not trigger:
        sys.exit(0)

    trigger_ts = trigger["ts"]

    if already_handled(trigger_ts):
        sys.exit(0)

    slack_post(":hourglass_flowing_sand: Starting HR Derby job\u2026", thread_ts=trigger_ts)

    try:
        run_job(trigger_ts)
        slack_post(":white_check_mark: HR Derby job complete \u2014 CSV posted to Slack.", thread_ts=trigger_ts)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        slack_post(f":x: HR Derby job failed: {e}\n```{tb[-800:]}```", thread_ts=trigger_ts)


if __name__ == "__main__":
    main()
