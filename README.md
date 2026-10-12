# Fantasy Football Power Rankings

A weekly power rankings page for a Sleeper league. Team names, records, and each team's top 3 performers come from Sleeper automatically; the ranking order (and optional blurbs) are the commissioner's.

## One-time setup

1. `py ffr.py setup <your-sleeper-username>` saves your league to `config.json`.
2. Push this folder to a GitHub repo, then in the repo go to **Settings → Pages → Deploy from a branch → `main` / `/docs`**.
   Your league's link will be `https://<github-username>.github.io/<repo-name>/`.

## Every week

1. `py ffr.py new 5` pulls data through week 5 and creates `weeks/week-05.txt`.
2. Open that file and cut/paste the team lines into your ranking order. It starts in last week's order, and the numbers don't matter.
   - The `//` lines under each team show its record, PF, streak, and top performers. They're notes for you only.
   - Anything you type under a team appears as its blurb. This is optional; `**bold**` and `*italic*` work.
   - Change the `TITLE:` line if you want a different headline.
3. `py ffr.py build` regenerates `docs/`. Open `docs/index.html` to preview.
4. When you're happy: `git add -A`, `git commit -m "Week 5"`, `git push`, then post the link.

To backfill earlier weeks, run `py ffr.py new 1-4`. Each file gets the records and top performers as they stood after that week.

Re-running `new` for a week refreshes the stats but never touches your rankings.
Movement arrows are calculated from the previous week's ranking.
Top performers are season points scored while in that team's starting lineup.
