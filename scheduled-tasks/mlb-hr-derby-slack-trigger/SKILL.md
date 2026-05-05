---
name: mlb-hr-derby-slack-trigger
description: Break-glass only: watches #sports-game-slates for !run hr-derby every 4 hours
---

You are a Slack-triggered automation that watches for manual HR Derby run requests. This is a break-glass fallback — the normal schedule handles Tuesday and Saturday automatically.

## Step 1 — Read recent Slack messages

Read the last 20 messages from Slack channel C0APGR57MLJ (#sports-game-slates).

## Step 2 — Check for trigger

Run `date +%s` to get the current Unix timestamp. Subtract 7200 to compute the cutoff (2 hours ago). Do NOT run bash arithmetic — just use `date +%s` and do the subtraction yourself.

For each message, check if `float(message.ts) >= cutoff` AND message text contains `!run hr-derby` (case-insensitive).

- If NO such message → exit silently with zero output and no Slack posts.
- If found → proceed. Use the FIRST matching message. Record its `ts` for threading.

## Step 3 — Idempotency check

Read the thread on the triggering message (thread_ts = message ts). If any reply contains "Starting HR Derby", ":white_check_mark: HR Derby job complete", or ":x: HR Derby job failed" → exit silently.

## Step 4 — Acknowledge

Reply in the thread:
`:hourglass_flowing_sand: Starting HR Derby job…`

## Step 5 — Run unit tests

```bash
python /Users/joekustelski/Projects/chalkline/hr_derby_generator.py --run-tests
```

If tests fail:
- Reply in thread: `:x: HR Derby job failed: unit tests failed. Fix required.`
- Exit.

## Step 6 — Determine target game date

Get today's day of week in CT:
```bash
TZ='America/Chicago' date '+%u'
TZ='America/Chicago' date '+%Y-%m-%d'
TZ='America/Chicago' date -v+1d '+%Y-%m-%d'
```

Logic:
- Monday (1): target = tomorrow (Tuesday)
- Friday (5): target = tomorrow (Saturday)
- Saturday (6): get current CT hour (`TZ='America/Chicago' date '+%H'`); if before 18, target = today; else target = next Tuesday
- Any other day: target = the next upcoming Tuesday (compute days until Tuesday)

## Step 7 — Fetch fixtures for target date

Use OpticOdds `get_fixtures` (sport: "baseball", league: "MLB") for the target date. Also fetch the following UTC calendar date. Combine, deduplicate, convert to CT (subtract 5 hours). Keep only fixtures where CT date == target date AND CT hour >= 18.

**If no evening fixtures**: post to #sports-game-slates:
`:warning: *No HR Derby available* — No MLB evening games (6 PM CT+) found for [Day, Mon DD].`
Reply in thread: `:x: HR Derby job failed: No evening games on target date.`
Log the run and exit.

## Step 8 — Build player pool

For each TEAM in filtered fixtures:

**Elite (+300)**: Aaron Judge, Shohei Ohtani, Giancarlo Stanton, Pete Alonso, Kyle Schwarber, Matt Olson, Yordan Alvarez, Vladimir Guerrero Jr., Gunnar Henderson, Bobby Witt Jr., Marcell Ozuna, Freddie Freeman, Fernando Tatis Jr., José Ramírez

**Above Average (+450)**: Riley Greene, Junior Caminero, Ben Rice, Jonathan Aranda, Jazz Chisholm, Jazz Chisholm Jr., Brent Rooker, Nick Kurtz, Trent Grisham, Spencer Torkelson, Elly De La Cruz, Elly de la Cruz, Pete Crow-Armstrong, Michael Busch, Shea Langeliers, Eugenio Suarez, Agustin Ramirez, Alex Bregman, Ryan McMahon, Colt Keith, Kerry Carpenter, Byron Buxton, Jackson Chourio, Sal Stewart, Jackson Merrill, Bryce Harper, Manny Machado, Trea Turner, Paul Goldschmidt, Nolan Arenado, Corey Seager, Adolis Garcia, Teoscar Hernandez, William Contreras

**Average (+650)**: all other MLB position players on teams playing that evening. Never include pitchers (SP/RP/P).

Include top hitters from EACH team.

## Step 9 — Check injuries

OpticOdds `get_injuries` (sport: "baseball", league: "MLB"). Exclude "Out" or "Doubtful".

## Step 10 — Get real odds

OpticOdds `get_player_props` for target fixture IDs, home run markets ("batter_home_runs", "to_hit_a_home_run", or similar). Real odds > tier estimates. Mix is fine.

## Step 11 — Select top 32

Sort eligible players by American odds ascending. Take top 32. Tiebreaker: elite > above_average > average.

## Step 12 — Generate CSV

```bash
cat > /tmp/hr_derby_payload.json << 'PAYLOAD'
<json>
PAYLOAD
python3 /Users/joekustelski/Projects/chalkline/hr_derby_generator.py --date YYYY-MM-DD < /tmp/hr_derby_payload.json
```

Parse the `__RESULT__` JSON block from stdout.

## Step 13 — Post to Slack

Post to #sports-game-slates (C0APGR57MLJ):
- **Post 1**: `check_it_message` (player-facing ranked table)
- **Post 2**: `slack_message` (ops summary)
- **Post 3**: `csv_content` as a code block

## Step 14 — Schedule one-off results task (if needed)

Determine the morning after the game: game_date + 1 day at 7:00 AM CT.

Check if that morning is already covered by a scheduled results run:
- If game is on a Tuesday → Wednesday 7 AM run covers it (mlb-hr-derby-results-wednesday). Skip.
- If game is on a Saturday → Sunday 7 AM run covers it (mlb-hr-derby-results-sunday). Skip.
- Otherwise → create a one-time results task:

taskId: "mlb-hr-derby-results-oneoff"
fireAt: "[game_date + 1 day]T07:00:00-05:00"
description: "One-off HR Derby results for [game_date]"

The prompt for this one-off task:
---
You are running the MLB HR Derby results job for the game played last night.

Goal: Post the results of yesterday's HR Derby game to Slack, and log the run.

Step 1 — Determine yesterday's date in CT:
TZ='America/Chicago' date -v-1d '+%Y-%m-%d'
TZ='America/Chicago' date -v-1d '+%m-%d-%Y'
TZ='America/Chicago' date -v-1d '+%m/%d/%Y'

Step 2 — Load yesterday's player list:
Check /Users/joekustelski/Downloads/HR Derby MLB MM-DD-YYYY.csv (yesterday's date as MM-DD-YYYY).
- Not found → log NO_GAME and exit silently.
- Found → parse Market Name column.

Step 3 — Fetch HR hitters from MLB Stats API:
https://statsapi.mlb.com/api/v1/stats?stats=byDateRange&group=hitting&gameType=R&startDate=MM/DD/YYYY&endDate=MM/DD/YYYY&season=YYYY&sportId=1&limit=500&sortStat=homeRuns&order=desc
Filter homeRuns > 0. Manually sum individual values (no pre-computed totals).
Also fetch: https://statsapi.mlb.com/api/v1/schedule?sportId=1&date=MM/DD/YYYY

Step 4 — Cross-reference: WON if name in HR hitters (case-insensitive, handle accents). LOST otherwise.

Step 5 — Idempotency check: read recent messages in C0APGR57MLJ. If today's CT message contains "HR Derby Results" + yesterday's date string → exit silently.

Step 6 — Post to C0APGR57MLJ:
:baseball: *HR Derby Results — [Day, Mon DD]* ([N] games on the slate)
:white_check_mark: *WON ([X] players hit a HR):*
• [Player] ([TEAM])
:x: *LOST ([Y] players did not hit a HR):*
• [comma-separated list]
:bar_chart: *[Z] total HRs hit across [N] games yesterday*
If 0 WON: :goat: Tough day — nobody on the slate went yard

Step 7 — Log:
mkdir -p /Users/joekustelski/.claude/logs && echo "$(TZ='America/Chicago' date '+%Y-%m-%dT%H:%M:%S%z') | results-oneoff | game=YYYY-MM-DD | SUCCESS | won=X lost=Y total_hrs=Z" >> /Users/joekustelski/.claude/logs/hr-derby.log

Error handling: If MLB Stats API unavailable, post note to C0APGR57MLJ and log ERROR. Never post partial results.
---

## Step 15 — Log this run

```bash
mkdir -p /Users/joekustelski/.claude/logs
echo "$(TZ='America/Chicago' date '+%Y-%m-%dT%H:%M:%S%z') | manual-trigger | target=YYYY-MM-DD | SUCCESS | N players | N games" >> /Users/joekustelski/.claude/logs/hr-derby.log
```

## Step 16 — Reply in thread with outcome

On success:
`:white_check_mark: HR Derby job complete — CSV posted to Slack. Results will auto-post [Wed/Sun/tomorrow] at 7 AM CT.`

On error:
`:x: HR Derby job failed: <error details>`