---
name: mlb-hr-derby-results-sunday
description: Posts Saturday night HR Derby results — runs Sunday at 7 AM CT
---

You are running the MLB HR Derby results job for Saturday night's game.

**Goal**: Post the results of last night's (Saturday) HR Derby game to Slack, and log the run.

**Implementation rules** (required — no exceptions):
- Use `curl -s` + `jq` for all API calls and JSON processing. Never use `python3` or write temp scripts.
- Use the `Read` tool to read CSV files.
- Use `awk`/`grep`/`bash` for text processing.

---

## Step 1 — Determine yesterday's date in CT

```bash
TZ='America/Chicago' date -v-1d '+%Y-%m-%d'
TZ='America/Chicago' date -v-1d '+%m-%d-%Y'
TZ='America/Chicago' date -v-1d '+%m/%d/%Y'
```

---

## Step 2 — Load yesterday's player list

Check if the CSV exists:
```bash
ls "/Users/joekustelski/Downloads/HR Derby MLB MM-DD-YYYY.csv"
```

- If the file does **not** exist → log NO_GAME (Step 7) and exit silently. The game didn't run.
- If it exists → use the `Read` tool to read it. Parse the `Market Name` column (3rd column) to extract player names.

---

## Step 3 — Fetch yesterday's HR hitters from MLB Stats API

```bash
curl -s "https://statsapi.mlb.com/api/v1/stats?stats=byDateRange&group=hitting&gameType=R&startDate=MM/DD/YYYY&endDate=MM/DD/YYYY&season=YYYY&sportId=1&limit=500&sortStat=homeRuns&order=desc" \
  | jq -r '.stats[0].splits[] | select((.stat.homeRuns | tonumber) > 0) | "\(.player.fullName)|\(.team.abbreviation // .team.name)|\(.stat.homeRuns)"'
```

Each output line is `PlayerName|TEAM|HRcount`. Collect all lines — these are your HR hitters.

Sum total HRs:
```bash
curl -s "https://statsapi.mlb.com/api/v1/stats?..." | jq '[.stats[0].splits[].stat.homeRuns | tonumber] | add'
```

Fetch completed game count:
```bash
curl -s "https://statsapi.mlb.com/api/v1/schedule?sportId=1&date=MM/DD/YYYY" \
  | jq '[.dates[0].games[] | select(.status.abstractGameState == "Final")] | length'
```

---

## Step 4 — Cross-reference results

For each player name from the CSV, check case-insensitively whether their name appears in the HR hitters list. Handle accented characters by normalizing (e.g. "Elly De La Cruz" matches "Elly De La Cruz").

Build two lists:
- **WON**: players whose name matched an HR hitter (include team abbreviation from the API)
- **LOST**: players whose name did not match

---

## Step 5 — Idempotency check

Before posting, read recent messages in Slack channel C0APGR57MLJ using the `mcp__203da36a-44f2-4773-baf0-11fb29a09ca5__slack_read_channel` tool. If any message posted today (CT) contains both "HR Derby Results" and yesterday's date string (e.g. "Apr 25") → exit silently to avoid double-posting.

---

## Step 6 — Post results to Slack

Post to #sports-game-slates (ID: C0APGR57MLJ):

```
:baseball: *HR Derby Results — [Day, Mon DD]* ([N] games on the slate)

:white_check_mark: *WON ([X] players hit a HR):*
• Shohei Ohtani (LAD)
• Yordan Alvarez (HOU)
...

:x: *LOST ([Y] players did not hit a HR):*
• Marcell Ozuna, Matt Olson, Freddie Freeman ...

:bar_chart: *[Z] total HRs hit across [N] games yesterday*
```

- List WON players one per line with team abbreviation
- List LOST players as a comma-separated inline string (saves space)
- If 0 WON: `:goat: Tough day — nobody on the slate went yard`

---

## Step 7 — Log the run

```bash
mkdir -p /Users/joekustelski/.claude/logs
echo "$(TZ='America/Chicago' date '+%Y-%m-%dT%H:%M:%S%z') | results-sunday | game=YYYY-MM-DD | SUCCESS | won=X lost=Y total_hrs=Z" >> /Users/joekustelski/.claude/logs/hr-derby.log
```

Replace placeholders with actual values. If no CSV found, log:
```bash
echo "$(TZ='America/Chicago' date '+%Y-%m-%dT%H:%M:%S%z') | results-sunday | game=YYYY-MM-DD | NO_GAME" >> /Users/joekustelski/.claude/logs/hr-derby.log
```

---

## Error handling

- If the MLB Stats API is unavailable: post a note to #sports-game-slates that results couldn't be fetched; log ERROR.
- If no CSV exists: log NO_GAME and exit silently.
- Never post partial or uncertain results.
