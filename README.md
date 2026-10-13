# Fantasy Football Power Rankings

A weekly power rankings site for a Sleeper league, live at https://redvinesfantasyfootball.com. Team names, records, and each team's top 3 performers come from Sleeper automatically; the ranking order (and optional blurbs) are the commissioner's.

## What's on the site

Everything except the ranking order, blurbs and pictures is pulled from Sleeper automatically.

- **Weekly rankings:** record, PF, streak and top performers (season and week) for each team. Each week also has a playoff odds table, a rankings-over-time chart, and weekly awards.
- **League pages (per season):** FAAB Tracker (budget, bid log and waiver ROI), Trade Grades and Draft Report Card.
- **League History (all seasons):** Record Book (champions, all-time standings, records) and Head-to-Head.

## Every week

1. `py ffr.py new 5` pulls data through week 5 and creates `weeks/2026/week-05.txt`.
2. Open that file and cut/paste the team lines into your ranking order. It starts in last week's order, and the numbers don't matter.
   - The `//` lines under each team show its record, PF, streak, and top performers. They're notes for you only.
   - Anything you type under a team appears as its blurb. This is optional; `**bold**` and `*italic*` work.
   - Change the `TITLE:` line if you want a different headline.
3. `py ffr.py build` regenerates `docs/`. Open `docs/index.html` to preview.
4. When you're happy: `git pull`, `git add -A`, `git commit -m "Week 5"`, `git push`, then post the link.

## Each new season

Sleeper gives the league a new ID every year. Once the new season's league exists, run:

    py ffr.py setup <your-sleeper-username>

This adds that year to `config.json`. After that, `new` uses the newest season automatically, and the site's sidebar lists every season.
To work on an older season, add the year: `py ffr.py new 14 2026`.

## Notes

- To backfill weeks, run `py ffr.py new 1-4`. Each file gets the records and top performers as they stood after that week.
- Re-running `new` for a week refreshes the stats but never touches your rankings.
- Movement arrows compare with the previous week's ranking in the same season.
- Top performers are season points scored while in that team's starting lineup. QB1, RB3, etc. is the player's rank at his position across the NFL, in league scoring, through that week.
