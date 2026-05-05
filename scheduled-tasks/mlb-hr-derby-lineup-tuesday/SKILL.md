---
name: mlb-hr-derby-lineup-tuesday
description: Generates Tuesday night HR Derby lineup — runs Monday at noon CT
---

You are running the MLB HR Derby lineup generation job for Tuesday night's games.

**Goal**: Generate a ready-to-upload Chalkline CSV of the top 32 likely home run hitters for TOMORROW's (Tuesday) evening MLB games, post it to Slack, and log the run.

---

## Step 0 — Run unit tests

Before anything else, validate the generator script:

```bash
python /Users/joekustelski/Projects/chalkline/hr_derby_generator.py --run-tests
```

If tests fail, post to #sports-game-slates (C0APGR57MLJ):
`:x: *HR Derby lineup aborted* — generator unit tests failed. Fix required before next run.`
Then exit. Do NOT generate a CSV with a broken script.

---

## Step 1 — Determine tomorrow's date in CT

```bash
TZ='America/Chicago' date -v+1d '+%Y-%m-%d'
TZ='America/Chicago' date -v+1d '+%m-%d-%Y'
```

---

## Step 2 — Fetch tomorrow's MLB fixtures

Use OpticOdds `get_fixtures`:
- sport: "baseball"
- league: "MLB"
- date: tomorrow's CT date (YYYY-MM-DD)

Also fetch the following UTC calendar date (some late CT games have UTC timestamps rolling to the next UTC day). Combine both results, deduplicate by fixture ID.

Convert each fixture's `start_date` (UTC ISO string) to CT by subtracting 5 hours. Keep only fixtures where:
- CT date == tomorrow's CT date, AND
- CT start hour >= 18 (6:00 PM CT or later)

**If no evening fixtures exist**: post an alert to #sports-game-slates:
`:warning: *No HR Derby tomorrow (Tue)* — No MLB evening games (6 PM CT+) found for [Day, Mon DD]. Slate not generated.`
Then log the run (Step 9) with outcome=NO_GAMES and exit. Do NOT generate a CSV.

If fewer than 5 games: continue — the script will embed a thin-slate warning automatically.

---

## Step 3 — Build the player pool

For each TEAM in the filtered fixtures, identify their top home run hitters using this tier system:

**Elite (+300)**: Aaron Judge, Shohei Ohtani, Giancarlo Stanton, Pete Alonso, Kyle Schwarber, Matt Olson, Yordan Alvarez, Vladimir Guerrero Jr., Gunnar Henderson, Bobby Witt Jr., Marcell Ozuna, Freddie Freeman, Fernando Tatis Jr., José Ramírez

**Above Average (+450)**: Riley Greene, Junior Caminero, Ben Rice, Jonathan Aranda, Jazz Chisholm, Jazz Chisholm Jr., Brent Rooker, Nick Kurtz, Trent Grisham, Spencer Torkelson, Elly De La Cruz, Elly de la Cruz, Pete Crow-Armstrong, Michael Busch, Shea Langeliers, Eugenio Suarez, Agustin Ramirez, Alex Bregman, Ryan McMahon, Colt Keith, Kerry Carpenter, Byron Buxton, Jackson Chourio, Sal Stewart, Jackson Merrill, Bryce Harper, Manny Machado, Trea Turner, Paul Goldschmidt, Nolan Arenado, Corey Seager, Adolis Garcia, Teoscar Hernandez, William Contreras

**Average (+650)**: all other MLB position players on teams playing tomorrow evening

Include top hitters from EACH team in tomorrow's slate — don't just pick the best players across the whole league. Never include pitchers (SP/RP/P).

---

## Step 4 — Check injuries

Use OpticOdds `get_injuries` (sport: "baseball", league: "MLB").

Exclude any player listed as "Out" or "Doubtful".

---

## Step 5 — Get real odds (if available)

Use OpticOdds `get_player_props` for tomorrow's fixture IDs, looking for home run markets ("batter_home_runs", "to_hit_a_home_run", or similar).

- Real lines posted → use them (American odds)
- Not posted yet → use tier estimates from Step 3

Priority: **real odds > tier estimates**. Mix is fine.

---

## Step 6 — Select top 32

Sort eligible (non-injured, non-pitcher) players by American odds ascending. Take top 32.

Tiebreaker: elite > above_average > average tier.

---

## Step 7 — Generate the CSV

Build the JSON payload. Pass ALL fetched fixtures (not pre-filtered) in `fixtures` — the script applies the 6 PM CT + date filter internally. Include `team` and `position` on each prop:

```json
{
  "fixtures": [
    {"id": "...", "start_date": "...", "away": "Team A", "home": "Team B"}
  ],
  "props": [
    {"name": "Aaron Judge", "american_odds": 300, "is_estimated": false, "team": "New York Yankees", "position": "OF"}
  ]
}
```

Write the payload to a temp file, then run:
```bash
cat > /tmp/hr_derby_payload.json << 'PAYLOAD'
<json>
PAYLOAD
python3 /Users/joekustelski/Projects/chalkline/hr_derby_generator.py --date YYYY-MM-DD < /tmp/hr_derby_payload.json
```

Parse the `__RESULT__` JSON block from stdout. The CSV is written to `/Users/joekustelski/Downloads/HR Derby MLB MM-DD-YYYY.csv`. Do not modify it.

---

## Step 8 — Post to Slack

Post to #sports-game-slates (ID: C0APGR57MLJ):

**Post 1** — `check_it_message` from the script output (player-facing ranked table)

**Post 2** — `slack_message` from the script output (ops summary with contestant IDs, NULL warnings, estimated-odds flags)

**Post 3** — `csv_content` from the script output, as a code block:
```
📎 *HR Derby MLB MM-DD-YYYY.csv* — upload-ready:

```csv
[csv_content here]
` ``
```

---

## Step 9 — Log the run

```bash
mkdir -p /Users/joekustelski/.claude/logs
echo "$(TZ='America/Chicago' date '+%Y-%m-%dT%H:%M:%S%z') | lineup-tuesday | target=YYYY-MM-DD | SUCCESS | N players | N games | estimated=true/false | warnings=N" >> /Users/joekustelski/.claude/logs/hr-derby.log
```

Replace placeholders with actual values from the `__RESULT__` block.

If the run ended early due to NO_GAMES or ERROR, log that outcome instead:
```bash
echo "$(TZ='America/Chicago' date '+%Y-%m-%dT%H:%M:%S%z') | lineup-tuesday | target=YYYY-MM-DD | NO_GAMES" >> /Users/joekustelski/.claude/logs/hr-derby.log
```

---

## Step 10 — Look-ahead: flag upcoming thin slates

After posting, check the next 4 days (+2 through +5 from today in CT) for thin evening schedules.

For each date:
- Fetch fixtures with OpticOdds `get_fixtures` (baseball, MLB, that CT date)
- Convert each start_date to CT (subtract 5 hours)
- Count games where CT date == target date AND CT hour >= 18

If any date has fewer than 5 such games, post a single alert to #sports-game-slates:
```
:calendar: *Heads up — thin MLB slates ahead:*
  • Fri Apr 11: only 2 evening games — HR Derby may not run
```

If all dates look normal (5+ games), skip this post entirely.

---

## Error handling

- **Unit tests fail**: post alert to Slack, exit without generating CSV, log ERROR
- **OpticOdds unavailable**: use tier estimates for all players from tomorrow's teams; note in Slack
- **No evening fixtures**: post alert, log NO_GAMES, exit — never generate a CSV for an empty slate
- **Script fails**: post error traceback to #sports-game-slates, log ERROR, do not silently fail
- **Injured star excluded**: note exclusion in the Slack message