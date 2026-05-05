---
name: mlb-hr-derby-results-wednesday
description: Posts Tuesday night HR Derby results — runs Wednesday at 7 AM CT
---

You are running the MLB HR Derby results job for Tuesday night's game.

**Goal**: Post the results of last night's (Tuesday) HR Derby game to Slack, and log the run.

---

## Step 1 — Determine yesterday's date in CT

```bash
TZ='America/Chicago' date -v-1d '+%Y-%m-%d'
TZ='America/Chicago' date -v-1d '+%m-%d-%Y'
TZ='America/Chicago' date -v-1d '+%m/%d/%Y'
```

---

## Step 2 — Load yesterday's player list

Check if the CSV exists at:
```
/Users/joekustelski/Downloads/HR Derby MLB MM-DD-YYYY.csv
```
(using yesterday's date as MM-DD-YYYY)

- If the file does **not** exist → log NO_GAME (Step 7) and exit silently. The game didn't run.
- If it exists → read it. Parse the `Market Name` column to extract the player names.

---

## Step 3 — Fetch yesterday's HR hitters from MLB Stats API

```
https://statsapi.mlb.com/api/v1/stats?stats=byDateRange&group=hitting&gameType=R&startDate=MM/DD/YYYY&endDate=MM/DD/YYYY&season=YYYY&sportId=1&limit=500&sortStat=homeRuns&order=desc
```

Filter to players where `homeRuns > 0`. Record each player's full name and exact HR count.

**Important**: Do NOT use any pre-computed totals from the response — manually sum individual `homeRuns` values yourself.

Also fetch total game count:
```
https://statsapi.mlb.com/api/v1/schedule?sportId=1&date=MM/DD/YYYY
```
Count completed games.

---

## Step 4 — Cross-reference results

For each player from the CSV:
- **WON** if their name appears in the HR hitters list (case-insensitive, handle accents)
- **LOST** if their name does not appear

---

## Step 5 — Idempotency check

Before posting, read recent messages in Slack channel C0APGR57MLJ. If any message posted today (CT) contains both "HR Derby Results" and yesterday's date string (e.g. "Apr 22") → exit silently to avoid double-posting.

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
echo "$(TZ='America/Chicago' date '+%Y-%m-%dT%H:%M:%S%z') | results-wednesday | game=YYYY-MM-DD | SUCCESS | won=X lost=Y total_hrs=Z" >> /Users/joekustelski/.claude/logs/hr-derby.log
```

Replace placeholders with actual values. If no CSV found, log:
```bash
echo "$(TZ='America/Chicago' date '+%Y-%m-%dT%H:%M:%S%z') | results-wednesday | game=YYYY-MM-DD | NO_GAME" >> /Users/joekustelski/.claude/logs/hr-derby.log
```

---

## Error handling

- If the MLB Stats API is unavailable: post a note to #sports-game-slates that results couldn't be fetched; log ERROR.
- If no CSV exists: log NO_GAME and exit silently.
- Never post partial or uncertain results.