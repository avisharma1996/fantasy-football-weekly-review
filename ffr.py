"""Fantasy football power rankings: pull Sleeper data, set your rankings, build a web page.

Usage:
  py ffr.py setup <sleeper-username>   Pick this season's league and save it to config.json (once per year)
  py ffr.py new <week> [season]        Fetch data through that week and create weeks/<season>/week-NN.txt
  py ffr.py new <first>-<last>         Same, for a range of weeks (e.g. new 1-4 to backfill)
  py ffr.py build                      Turn every season's week files into the website in docs/

The season defaults to the newest one in config.json.
"""

import html
import json
import re
import shutil
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
CONFIG = ROOT / "config.json"
WEEKS = ROOT / "weeks"
SITE = ROOT / "docs"
IMAGES = ROOT / "images"
PLAYERS = ROOT / ".cache" / "players.json"
API = "https://api.sleeper.app/v1"


# ---------------------------------------------------------------- Sleeper data

_seen = {}


def get(path):
    # Weekly matchups and stats are re-read for every later week, so reuse them within a run.
    if path in _seen:
        return _seen[path]
    req = urllib.request.Request(API + path, headers={"User-Agent": "ffr-weekly-review"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.load(r)
    if "/matchups/" in path or path.startswith("/stats/"):
        _seen[path] = data
    return data


def load_seasons():
    """{season: league_id}. Sleeper gives each season's league its own id."""
    if not CONFIG.exists():
        sys.exit("No config.json yet. Run: py ffr.py setup <your-sleeper-username>")
    return json.loads(CONFIG.read_text(encoding="utf-8"))["seasons"]


def setup(username):
    user = get(f"/user/{username}")
    if not user:
        sys.exit(f"No Sleeper user named '{username}'.")
    season = get("/state/nfl")["league_season"]
    leagues = get(f"/user/{user['user_id']}/leagues/nfl/{season}")
    if not leagues:
        sys.exit(f"'{username}' has no {season} NFL leagues.")
    for i, lg in enumerate(leagues, 1):
        print(f"  {i}. {lg['name']}  ({lg['total_rosters']} teams)")
    choice = 1
    if len(leagues) > 1:
        choice = int(input("Which league? ").strip().lstrip("﻿") or 1)
    league = leagues[choice - 1]
    seasons = load_seasons() if CONFIG.exists() else {}
    seasons[season] = league["league_id"]
    CONFIG.write_text(json.dumps({"seasons": dict(sorted(seasons.items()))}, indent=2) + "\n", encoding="utf-8")
    print(f"Saved '{league['name']}' as the {season} league in config.json")


def load_players(ids):
    """Map every player id to name and position. Sleeper's player list is large, so keep a slim local copy."""
    names = json.loads(PLAYERS.read_text(encoding="utf-8")) if PLAYERS.exists() else {}
    stale = not PLAYERS.exists() or time.time() - PLAYERS.stat().st_mtime > 7 * 86400
    if stale or any(i not in names for i in ids):
        print("Downloading Sleeper player list...")
        names = {}
        for pid, p in get("/players/nfl").items():
            if p.get("position") == "DEF":
                names[pid] = {"name": f"{p.get('first_name', '')} {p.get('last_name', '')} D/ST".strip(), "pos": "DEF"}
            else:
                names[pid] = {"name": p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip(),
                              "pos": p.get("position") or ""}
        PLAYERS.parent.mkdir(exist_ok=True)
        PLAYERS.write_text(json.dumps(names), encoding="utf-8")
    return names


def position_ranks(season, week, scoring, players):
    """Rank every NFL player at his position by fantasy points (league scoring).

    Returns (season ranks through `week`, ranks for `week` alone)."""
    totals, this_week = {}, {}
    for w in range(1, week + 1):
        for pid, stats in get(f"/stats/nfl/regular/{season}/{w}").items():
            pts = sum(stats[k] * v for k, v in scoring.items() if isinstance(stats.get(k), (int, float)))
            totals[pid] = totals.get(pid, 0) + pts
            if w == week:
                this_week[pid] = pts

    def rank(points):
        by_pos = {}
        for pid, pts in points.items():
            pos = players.get(pid, {}).get("pos")
            if pos and pts > 0:
                by_pos.setdefault(pos, []).append((pts, pid))
        return {pid: f"{pos}{i}" for pos, ranked in by_pos.items()
                for i, (_, pid) in enumerate(sorted(ranked, reverse=True), 1)}

    return rank(totals), rank(this_week)


def avatar_url(user):
    meta = user.get("metadata") or {}
    if meta.get("avatar"):
        return meta["avatar"]
    if user.get("avatar"):
        return f"https://sleepercdn.com/avatars/thumbs/{user['avatar']}"
    return None


def fetch_week(league_id, week):
    """Snapshot each team's record and top starters for the season through `week`."""
    league = get(f"/league/{league_id}")
    users = {u["user_id"]: u for u in get(f"/league/{league_id}/users")}
    rosters = get(f"/league/{league_id}/rosters")
    settings = league.get("settings") or {}
    playoff_start = settings.get("playoff_week_start") or 99
    median_game = settings.get("league_average_match") == 1

    teams, starter_pts, week_pts = {}, {}, {}
    for r in rosters:
        u = users.get(r.get("owner_id"), {})
        meta = u.get("metadata") or {}
        teams[r["roster_id"]] = {
            "name": meta.get("team_name") or u.get("display_name") or f"Team {r['roster_id']}",
            "manager": u.get("display_name", ""),
            "avatar": avatar_url(u) if u else None,
            "wins": 0, "losses": 0, "ties": 0, "pf": 0.0, "results": [],
        }
        starter_pts[r["roster_id"]] = {}
        week_pts[r["roster_id"]] = {}

    for w in range(1, week + 1):
        entries = get(f"/league/{league_id}/matchups/{w}")
        pts = {e["roster_id"]: float(e.get("custom_points") or e.get("points") or 0) for e in entries}
        regular = w < playoff_start

        for e in entries:
            totals = starter_pts[e["roster_id"]]
            for pid, p in zip(e.get("starters") or [], e.get("starters_points") or []):
                if pid and pid != "0":
                    totals[pid] = totals.get(pid, 0) + (p or 0)
                    if w == week:
                        week_pts[e["roster_id"]][pid] = p or 0

        if regular and median_game and pts:
            mid = sorted(pts.values())[len(pts) // 2 - 1: len(pts) // 2 + 1]
            median = sum(mid) / len(mid)
            for rid, p in pts.items():
                record(teams[rid], p, median)

        games = {}
        for e in entries:
            if e.get("matchup_id"):
                games.setdefault(e["matchup_id"], []).append(e["roster_id"])
        for a, b in (g for g in games.values() if len(g) == 2):
            for me, opp in ((a, b), (b, a)):
                t = teams[me]
                t["pf"] += pts[me]
                t["results"].append("W" if pts[me] > pts[opp] else "L" if pts[me] < pts[opp] else "T")
                if regular:
                    record(t, pts[me], pts[opp])

    top3 = lambda pts: sorted(pts.items(), key=lambda kv: -kv[1])[:3]
    top = {rid: top3(totals) for rid, totals in starter_pts.items()}
    top_week = {rid: top3(pts) for rid, pts in week_pts.items()}
    players = load_players({pid for picks in [*top.values(), *top_week.values()] for pid, _ in picks})
    ranks, week_ranks = position_ranks(league["season"], week, league.get("scoring_settings") or {}, players)
    card = lambda pid, p, r: {**players.get(pid, {"name": pid, "pos": ""}), "rank": r.get(pid, ""), "pts": round(p, 2)}
    for rid, t in teams.items():
        t["pf"] = round(t["pf"], 2)
        streak = ""
        for res in reversed(t["results"]):
            if streak and res != streak[0]:
                break
            streak = res + str(int(streak[1:] or 0) + 1)
        t["streak"] = streak
        del t["results"]
        t["top"] = [card(pid, p, ranks) for pid, p in top[rid]]
        t["top_week"] = [card(pid, p, week_ranks) for pid, p in top_week[rid]]

    return {
        "league": league["name"],
        "league_avatar": f"https://sleepercdn.com/avatars/thumbs/{league['avatar']}" if league.get("avatar") else None,
        "season": league["season"],
        "week": week,
        "median_game": median_game,
        "teams": {str(k): v for k, v in teams.items()},
    }


def record(t, mine, theirs):
    if mine > theirs:
        t["wins"] += 1
    elif mine < theirs:
        t["losses"] += 1
    else:
        t["ties"] += 1


# ---------------------------------------------------------------- week files

def week_paths(season, week):
    return WEEKS / season / f"week-{week:02d}.txt", WEEKS / season / f"week-{week:02d}.json"


def weeks_written(season):
    return sorted(int(p.stem[5:]) for p in (WEEKS / season).glob("week-*.txt")
                  if week_paths(season, int(p.stem[5:]))[1].exists())


def rec(t):
    return f"{t['wins']}-{t['losses']}" + (f"-{t['ties']}" if t["ties"] else "")


def standings_order(data):
    return sorted(data["teams"], key=lambda r: (-data["teams"][r]["wins"], -data["teams"][r]["pf"]))


def new_week(week, season=None):
    seasons = load_seasons()
    season = season or max(seasons)
    if season not in seasons:
        sys.exit(f"No {season} league in config.json. Seasons set up: {', '.join(seasons)}")
    (WEEKS / season).mkdir(parents=True, exist_ok=True)
    txt, js = week_paths(season, week)
    print(f"Fetching {season} week {week} from Sleeper...")
    data = fetch_week(seasons[season], week)
    js.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Saved stats to {js.relative_to(ROOT)}")
    if txt.exists():
        print(f"{txt.relative_to(ROOT)} already exists. Kept your rankings and refreshed only the stats.")
        return

    teams = data["teams"]
    prev = previous_ranking(season, week)
    lines = [
        "// Lines starting with // are notes for you and won't show on the page.",
        "// Reorder the team lines into your ranking. The numbers don't matter; order does.",
        "// Anything you type under a team shows as its blurb (optional). **bold** and *italic* work.",
        "// Pictures: save the file in the images folder, then write ![](file-name.png) on its own line.",
        "// Starting order is " + ("last week's ranking." if prev else "the standings."),
        "",
        f"TITLE: Week {week} Power Rankings",
        "// Optional intro: type it here, above the first team.",
        "",
        "",
    ]
    for i, rid in enumerate(prev or standings_order(data), 1):
        t = teams[rid]
        top = lambda picks: ", ".join(f"{p['name']} ({p.get('rank') or p['pos']}) {p['pts']:.1f}" for p in picks)
        lines += [f"{i}. {t['name']}", f"// {rec(t)}, {t['pf']:.2f} PF, streak {t['streak'] or '-'} | {top(t['top'])}",
                  f"// this week: {top(t['top_week'])}", "", ""]
    txt.write_text("\n".join(lines), encoding="utf-8")
    print(f"Created {txt.relative_to(ROOT)}. Open it and set your rankings.")


def parse_week(txt, data):
    """Read a week file into {title, intro, rankings: [(roster_id, blurb)]}. Text above the first team is the intro."""
    out = {"title": f"Week {data['week']} Power Rankings", "intro": "", "rankings": []}
    by_name = {t["name"].strip().lower(): rid for rid, t in data["teams"].items()}
    current, buf = None, []

    def flush():
        if current:
            out["rankings"].append((current, "\n".join(buf).strip()))
        else:
            out["intro"] = "\n".join(buf).strip()
        buf.clear()

    for line in txt.read_text(encoding="utf-8-sig").splitlines():
        if line.lstrip().startswith("//"):
            continue
        if line.startswith("TITLE:"):
            out["title"] = line[6:].strip() or out["title"]
        elif m := re.match(r"^\s*\d+[.)]\s+(.+?)\s*$", line):
            flush()
            if m.group(1).lower() not in by_name:
                sys.exit(f"{txt.name}: '{m.group(1)}' isn't a team name. Teams: "
                         + ", ".join(t["name"] for t in data["teams"].values()))
            current = by_name[m.group(1).lower()]
        else:
            buf.append(line)
    flush()

    ranked = [rid for rid, _ in out["rankings"]]
    if sorted(ranked) != sorted(data["teams"]):
        missing = [data["teams"][r]["name"] for r in data["teams"] if r not in ranked]
        dupes = {data["teams"][r]["name"] for r in ranked if ranked.count(r) > 1}
        sys.exit(f"{txt.name}: the rankings need every team exactly once. "
                 f"Missing: {missing or 'none'}. Listed twice: {sorted(dupes) or 'none'}.")
    return out


def previous_ranking(season, week):
    for w in range(week - 1, 0, -1):
        txt, js = week_paths(season, w)
        if txt.exists() and js.exists():
            return [rid for rid, _ in parse_week(txt, json.loads(js.read_text(encoding="utf-8")))["rankings"]]
    return None


# ---------------------------------------------------------------- website

def fmt(text, base):
    """Escape, then allow **bold**, *italic*, ![](image.png) from images/, and blank-line paragraphs."""
    def image(m):
        src = m.group(2)
        if not src.startswith(("http://", "https://")):
            if not (IMAGES / src).exists():
                print(f"  warning: images/{src} not found")
            src = f"{base}images/{src}"
        if src.lower().endswith((".mp4", ".webm", ".mov")):
            # Silent looping video behaves like a gif at a fraction of the size.
            return f'<video class="pic" src="{src}" autoplay loop muted playsinline preload="metadata"></video>'
        return f'<img class="pic" src="{src}" alt="{m.group(1)}" loading="lazy">'

    out = []
    for para in re.split(r"\n\s*\n", text.strip()):
        p = html.escape(para.strip())
        p = re.sub(r"!\[([^\]]*)\]\(([^)\s]+)\)", image, p)
        p = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", p)
        p = re.sub(r"\*(.+?)\*", r"<em>\1</em>", p)
        out.append(f"<p>{p.replace(chr(10), '<br>')}</p>")
    return "".join(out) if text.strip() else ""


def avatar(t):
    if t.get("avatar"):
        return f'<img class="av" src="{html.escape(t["avatar"])}" alt="" loading="lazy">'
    return f'<span class="av ph">{html.escape(t["name"][:1].upper())}</span>'


def render_week(data, content, prev_rank, all_weeks, latest_week, base):
    """`latest_week` maps each built season to its newest week; `base` is the path back to the site root."""
    teams, week, season, e = data["teams"], data["week"], str(data["season"]), html.escape
    rows = []
    for i, (rid, blurb) in enumerate(content["rankings"], 1):
        t = teams[rid]
        move = ""
        if prev_rank and rid in prev_rank:
            d = prev_rank.index(rid) + 1 - i
            move = (f'<span class="mv up">▲{d}</span>' if d > 0 else
                    f'<span class="mv down">▼{-d}</span>' if d < 0 else '<span class="mv same">–</span>')
        chips = lambda picks: "".join(f'<li><span class="pos">{e(p.get("rank") or p["pos"])}</span>{e(p["name"])}'
                                      f'<span class="pp">{p["pts"]:.1f}</span></li>' for p in picks)
        tops = "".join(f'<div class="lbl">{label}</div><ul class="top">{chips(picks)}</ul>'
                       for label, picks in (("Overall top performers", t["top"]),
                                            ("Weekly top performers", t.get("top_week") or [])) if picks)
        rows.append(f"""
  <li class="rank">
    <div class="rank-num">{i}{move}</div>
    <div class="rank-body">
      <div class="team">{avatar(t)}<div><h2>{e(t['name'])}</h2>
        <div class="meta"><b>{rec(t)}</b> · {t['pf']:.2f} PF{' · ' + t['streak'] if t['streak'] else ''}</div></div></div>
      {f'<div class="blurb">{fmt(blurb, base)}</div>' if blurb else ''}
      {f'<div class="tops">{tops}</div>' if tops else ''}
    </div>
  </li>""")

    nav = "".join(f'<a href="{base}{season}/week-{w:02d}.html"{" class=cur" if w == week else ""}>Wk {w}</a>'
                  for w in all_weeks)
    years = "".join(f'<a href="{base}{s}/week-{latest_week[s]:02d}.html"{" class=cur" if s == season else ""}>{s}</a>'
                    for s in sorted(latest_week, reverse=True))
    logo = f'<img class="logo" src="{e(data["league_avatar"])}" alt="">' if data.get("league_avatar") else ""
    note = "Records include a game against the league median each week. " if data.get("median_game") else ""
    intro = f'<section class="intro">{fmt(content["intro"], base)}</section>' if content["intro"] else ""
    return PAGE.format(title=e(content["title"]), league=e(data["league"]), season=e(season), week=week,
                       logo=logo, nav=nav, years=years, intro=intro, rows="".join(rows), note=note, css=CSS)


def build():
    seasons = sorted(d.name for d in WEEKS.iterdir() if d.is_dir() and weeks_written(d.name)) if WEEKS.exists() else []
    if not seasons:
        sys.exit("No weeks yet. Run: py ffr.py new <week>")
    latest_week = {s: weeks_written(s)[-1] for s in seasons}
    if IMAGES.exists():
        shutil.copytree(IMAGES, SITE / "images", dirs_exist_ok=True)
    for s in seasons:
        weeks = weeks_written(s)
        (SITE / s).mkdir(parents=True, exist_ok=True)
        prev_rank = None
        for w in weeks:
            txt, js = week_paths(s, w)
            data = json.loads(js.read_text(encoding="utf-8"))
            content = parse_week(txt, data)
            page = render_week(data, content, prev_rank, weeks, latest_week, "../")
            (SITE / s / f"week-{w:02d}.html").write_text(page, encoding="utf-8")
            if w == weeks[-1]:
                (SITE / s / "index.html").write_text(page, encoding="utf-8")
                if s == seasons[-1]:
                    root = render_week(data, content, prev_rank, weeks, latest_week, "")
                    (SITE / "index.html").write_text(root, encoding="utf-8")
            prev_rank = [r for r, _ in content["rankings"]]
        print(f"Built {s}: {len(weeks)} week(s)")
    print("Open docs/index.html to preview.")


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · {league}</title>
<meta property="og:title" content="{title} · {league}">
<meta property="og:description" content="Week {week} power rankings">
<style>{css}</style></head>
<body><div class="layout">
<aside class="seasons"><div class="seasons-lbl">Season</div><div class="seasons-list">{years}</div></aside>
<main class="wrap">
<header>{logo}<div><div class="kicker">{league} · {season} · Week {week}</div><h1>{title}</h1></div></header>
<nav>{nav}</nav>
{intro}
<ol class="ranks">{rows}</ol>
<footer>{note}Top performers are points scored as a starter for that team: overall is the season through that week, weekly is that week alone. QB1, RB3, etc. is the player's rank at his position across the NFL over the same span. Stats from Sleeper. Rankings are the commissioner's own.</footer>
</main></div></body></html>
"""

CSS = """
:root{--bg:#f6f5f2;--card:#fff;--ink:#1b1d21;--muted:#6b6f78;--line:#e4e2dc;--chip:#f0eee9;--up:#1f8a4c;--down:#c8402f;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#121417;--card:#1b1e23;--ink:#eceef1;--muted:#9aa0aa;--line:#2b2f36;--chip:#252930;--up:#4cc27e;--down:#ff7a66;color-scheme:dark}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.layout{display:flex;max-width:1000px;margin:0 auto}
.seasons{flex:none;width:150px;padding:40px 16px 0;position:sticky;top:0;align-self:flex-start}
.seasons-lbl{color:var(--muted);font-size:.72rem;text-transform:uppercase;letter-spacing:.06em;margin:0 0 8px 10px}
.seasons-list{display:flex;flex-direction:column;gap:2px}
.seasons a{padding:7px 10px;border-radius:8px;color:var(--muted);text-decoration:none;font-weight:600;font-variant-numeric:tabular-nums}
.seasons a:hover{background:var(--chip);color:var(--ink)}
.seasons a.cur{background:var(--card);color:var(--ink);box-shadow:inset 3px 0 0 var(--ink),0 0 0 1px var(--line)}
.wrap{flex:1;min-width:0;max-width:780px;padding:32px 16px 64px}
@media (max-width:760px){
  .layout{display:block}
  .seasons{position:static;width:auto;padding:16px 16px 0;display:flex;align-items:center;gap:10px}
  .seasons-lbl{margin:0}
  .seasons-list{flex-direction:row;flex-wrap:wrap;gap:6px}
  .seasons a{padding:4px 12px}
  .seasons a.cur{box-shadow:inset 0 -2px 0 var(--ink),0 0 0 1px var(--line)}
  .wrap{padding-top:16px}
}
header{display:flex;gap:16px;align-items:center}
.logo{width:64px;height:64px;border-radius:14px}
.kicker{color:var(--muted);font-size:.85rem;letter-spacing:.04em;text-transform:uppercase}
h1{margin:.1em 0 0;font-size:clamp(1.7rem,5vw,2.4rem);line-height:1.15}
h2{margin:0;font-size:1.1rem}
nav{display:flex;flex-wrap:wrap;gap:6px;margin:20px 0 28px}
nav a{padding:4px 10px;border-radius:999px;border:1px solid var(--line);color:var(--muted);text-decoration:none;font-size:.85rem}
nav a.cur{background:var(--ink);color:var(--bg);border-color:var(--ink)}
.intro{font-size:1.05rem;margin:-8px 0 24px}.intro p{margin:0 0 .7em}
.ranks{list-style:none;padding:0;margin:0;display:grid;gap:12px}
.rank{display:flex;gap:14px;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px}
.rank-num{font-size:1.9rem;font-weight:800;min-width:2.2ch;line-height:1.1;display:flex;flex-direction:column;align-items:center}
.mv{font-size:.72rem;font-weight:700;margin-top:4px}.up{color:var(--up)}.down{color:var(--down)}.same{color:var(--muted)}
.rank-body{flex:1;min-width:0}
.team{display:flex;gap:12px;align-items:center}
.meta{color:var(--muted);font-size:.88rem}.meta b{color:var(--ink)}
.av{width:44px;height:44px;border-radius:50%;object-fit:cover;flex:none;background:var(--line);display:inline-flex;align-items:center;justify-content:center;font-weight:700;color:var(--muted)}
.pic{display:block;max-width:100%;max-height:260px;height:auto;border-radius:8px;margin:6px 0}
video.pic{max-height:360px}
.blurb{margin-top:10px}.blurb p{margin:.5em 0 0}.blurb p:first-child{margin-top:0}
.tops{display:grid;grid-template-columns:auto 1fr;gap:8px 12px;align-items:center;margin-top:12px}
.tops .lbl{color:var(--muted);font-size:.72rem;text-transform:uppercase;letter-spacing:.05em}
.top{list-style:none;padding:0;margin:0;display:flex;flex-wrap:wrap;gap:6px;align-items:center;font-size:.85rem}
.top li{background:var(--chip);border-radius:8px;padding:3px 9px;display:flex;gap:6px;align-items:baseline}
@media (max-width:560px){.tops{grid-template-columns:1fr;gap:4px}.tops .top{margin-bottom:6px}}
.top .pos{color:var(--muted);font-size:.7rem;font-weight:700}
.top .pp{font-variant-numeric:tabular-nums;color:var(--muted)}
footer{margin-top:40px;color:var(--muted);font-size:.8rem;text-align:center}
"""


if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) == 2 and args[0] == "setup":
        setup(args[1])
    elif len(args) in (2, 3) and args[0] == "new" and (m := re.fullmatch(r"(\d+)(?:-(\d+))?", args[1])):
        for w in range(int(m.group(1)), int(m.group(2) or m.group(1)) + 1):
            new_week(w, args[2] if len(args) == 3 else None)
    elif args == ["build"]:
        build()
    else:
        print(__doc__)
