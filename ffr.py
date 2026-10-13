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
HISTORY = WEEKS / "history.json"
IMAGES = ROOT / "images"
PLAYERS = ROOT / ".cache" / "players.json"
API = "https://api.sleeper.app/v1"
SITE_NAME = "Red Vines Fantasy Football"  # browser tab and link-preview title on every page
RULE_BOOK_URL = "https://docs.google.com/document/d/104C7vp7KBJrH8BwWMcOG1W-5-xdedp_d_uX9PsyE7go/edit?tab=t.0"


# ---------------------------------------------------------------- Sleeper data

_seen = {}


def get(path):
    # Weekly matchups and stats are re-read for every later week, so reuse them within a run.
    if path in _seen:
        return _seen[path]
    req = urllib.request.Request(API + path, headers={"User-Agent": "ffr-weekly-review"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.load(r)
    if "/matchups/" in path or "/transactions/" in path or path.startswith("/stats/"):
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
    old_format = names and "fpos" not in next(iter(names.values()))
    if stale or old_format or any(i not in names for i in ids):
        print("Downloading Sleeper player list...")
        names = {}
        for pid, p in get("/players/nfl").items():
            pos = p.get("position") or ""
            if pos == "DEF":
                name = f"{p.get('first_name', '')} {p.get('last_name', '')} D/ST".strip()
            else:
                name = p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
            # fpos: every lineup position the player is eligible for.
            names[pid] = {"name": name, "pos": pos, "fpos": p.get("fantasy_positions") or [pos]}
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


SLOT_FITS = {"FLEX": {"RB", "WR", "TE"}, "SUPER_FLEX": {"QB", "RB", "WR", "TE"}, "REC_FLEX": {"WR", "TE"},
             "WRRB_FLEX": {"RB", "WR"}, "IDP_FLEX": {"DL", "LB", "DB"}}


def best_lineup(points, slots, players):
    """Highest-scoring legal lineup from a roster's points. Filling the most restrictive slots first is optimal
    because flex slots only ever widen eligibility."""
    fits = lambda slot: SLOT_FITS.get(slot, {slot})
    used, total = set(), 0.0
    for slot in sorted(slots, key=lambda s: len(fits(s))):
        options = [(p or 0, pid) for pid, p in points.items()
                   if pid not in used and fits(slot) & set(players.get(pid, {}).get("fpos") or [])]
        if options:
            p, pid = max(options)
            used.add(pid)
            total += p
    return total, used


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
    slots = [s for s in league.get("roster_positions") or [] if s not in ("BN", "IR", "TAXI")]
    players = load_players(set())

    teams, starter_pts, week_pts = {}, {}, {}
    for r in rosters:
        u = users.get(r.get("owner_id"), {})
        meta = u.get("metadata") or {}
        teams[r["roster_id"]] = {
            "name": meta.get("team_name") or u.get("display_name") or f"Team {r['roster_id']}",
            "manager": u.get("display_name", ""),
            "owner": r.get("owner_id"),
            "avatar": avatar_url(u) if u else None,
            "wins": 0, "losses": 0, "ties": 0, "pf": 0.0, "results": [],
            # All-play: record against every team each week. Luck: actual minus expected head-to-head wins.
            "ap_wins": 0, "ap_losses": 0, "h2h_wins": 0.0, "exp_wins": 0.0,
            # Bench: points the best possible lineup would have added.
            "bench": 0.0, "bench_week": 0.0, "bench_miss": None,
        }
        starter_pts[r["roster_id"]] = {}
        week_pts[r["roster_id"]] = {}

    this_week = []
    for w in range(1, week + 1):
        entries = get(f"/league/{league_id}/matchups/{w}")
        pts = {e["roster_id"]: float(e.get("custom_points") or e.get("points") or 0) for e in entries}
        regular = w < playoff_start

        for e in entries:
            rid = e["roster_id"]
            totals = starter_pts[rid]
            for pid, p in zip(e.get("starters") or [], e.get("starters_points") or []):
                if pid and pid != "0":
                    totals[pid] = totals.get(pid, 0) + (p or 0)
                    if w == week:
                        week_pts[rid][pid] = p or 0
            best, lineup = best_lineup(e.get("players_points") or {}, slots, players)
            left = max(best - pts[rid], 0)
            teams[rid]["bench"] += left
            if w == week:
                teams[rid]["bench_week"] = round(left, 2)
                missed = [(e["players_points"][pid], pid) for pid in lineup - set(e.get("starters") or [])]
                if left > 0 and missed:
                    p, pid = max(missed)
                    teams[rid]["bench_miss"] = {"player": pid, "pts": round(p, 2)}

        if regular and len(pts) > 1:
            for rid, p in pts.items():
                beat = sum(p > q for o, q in pts.items() if o != rid)
                tied = sum(p == q for o, q in pts.items() if o != rid)
                t = teams[rid]
                t["ap_wins"] += beat
                t["ap_losses"] += len(pts) - 1 - beat - tied
                t["exp_wins"] += (beat + tied / 2) / (len(pts) - 1)

        if regular and median_game and pts:
            mid = sorted(pts.values())[len(pts) // 2 - 1: len(pts) // 2 + 1]
            median = sum(mid) / len(mid)
            for rid, p in pts.items():
                record(teams[rid], p, median)

        games = {}
        for e in entries:
            if e.get("matchup_id"):
                games.setdefault(e["matchup_id"], []).append(e["roster_id"])
        for mid, (a, b) in ((m, g) for m, g in games.items() if len(g) == 2):
            for me, opp in ((a, b), (b, a)):
                t = teams[me]
                t["pf"] += pts[me]
                t["results"].append("W" if pts[me] > pts[opp] else "L" if pts[me] < pts[opp] else "T")
                if regular:
                    record(t, pts[me], pts[opp])
                    t["h2h_wins"] += 1 if pts[me] > pts[opp] else 0.5 if pts[me] == pts[opp] else 0
            if w == week:
                this_week.append({"a": str(a), "a_pts": round(pts[a], 2), "b": str(b), "b_pts": round(pts[b], 2)})

    top3 = lambda pts: sorted(pts.items(), key=lambda kv: -kv[1])[:3]
    top = {rid: top3(totals) for rid, totals in starter_pts.items()}
    top_week = {rid: top3(pts) for rid, pts in week_pts.items()}
    missed = {t["bench_miss"]["player"] for t in teams.values() if t["bench_miss"]}
    players = load_players({pid for picks in [*top.values(), *top_week.values()] for pid, _ in picks} | missed)
    ranks, week_ranks = position_ranks(league["season"], week, league.get("scoring_settings") or {}, players)
    who = lambda pid: {k: players.get(pid, {"name": pid, "pos": ""})[k] for k in ("name", "pos")}
    card = lambda pid, p, r: {**who(pid), "rank": r.get(pid, ""), "pts": round(p, 2)}
    for rid, t in teams.items():
        t["pf"] = round(t["pf"], 2)
        t["luck"] = round(t["h2h_wins"] - t["exp_wins"], 2)
        t["exp_wins"], t["bench"] = round(t["exp_wins"], 2), round(t["bench"], 2)
        if t["bench_miss"]:
            t["bench_miss"].update(who(t["bench_miss"].pop("player")))
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
        "matchups": this_week,
    }


def fetch_history(seasons):
    """Every regular-season game and final placing across all seasons, keyed by manager (Sleeper user id)."""
    managers, games, finishes, names = {}, [], [], {}
    for season, league_id in sorted(seasons.items()):
        league = get(f"/league/{league_id}")
        settings = league.get("settings") or {}
        users = {u["user_id"]: u for u in get(f"/league/{league_id}/users")}
        owner = {r["roster_id"]: r.get("owner_id") for r in get(f"/league/{league_id}/rosters")}
        names[season] = {}
        for uid in filter(None, owner.values()):
            u = users.get(uid, {})
            team = (u.get("metadata") or {}).get("team_name") or u.get("display_name", "?")
            names[season][uid] = team
            # Later seasons overwrite earlier ones, so each manager shows their latest team name.
            managers[uid] = {"name": team, "manager": u.get("display_name", ""), "avatar": avatar_url(u) if u else None}
        last_regular = (settings.get("playoff_week_start") or 15) - 1
        scored = settings.get("last_scored_leg") or 0
        for w in range(1, min(last_regular, scored) + 1):
            entries = get(f"/league/{league_id}/matchups/{w}")
            pts = {e["roster_id"]: float(e.get("custom_points") or e.get("points") or 0) for e in entries}
            pairs = {}
            for e in entries:
                if e.get("matchup_id"):
                    pairs.setdefault(e["matchup_id"], []).append(e["roster_id"])
            games += [{"season": season, "week": w, "a": owner[a], "a_pts": round(pts[a], 2),
                       "b": owner[b], "b_pts": round(pts[b], 2)} for a, b in (p for p in pairs.values() if len(p) == 2) if owner[a] and owner[b]]
        if league.get("status") == "complete":
            for m in get(f"/league/{league_id}/winners_bracket") or []:
                if m.get("p") == 1 and m.get("w"):
                    finishes.append({"season": season, "champion": owner[m["w"]], "runner_up": owner[m["l"]]})
    return {"managers": managers, "names": names, "games": games, "finishes": finishes}


def fetch_faab(league_id):
    """Every winning waiver claim this season, with the competing bids it beat, plus FAAB traded between teams."""
    league = get(f"/league/{league_id}")
    claims, lost, trades = [], {}, []
    for w in range(0, 19):
        for t in get(f"/league/{league_id}/transactions/{w}") or []:
            if t["status"] == "complete":
                trades += [{"week": t["leg"], "from": str(x["sender"]), "to": str(x["receiver"]), "amount": x["amount"]}
                           for x in t.get("waiver_budget") or []]
            if t.get("type") != "waiver" or not t.get("adds"):
                continue
            bid, rid = (t.get("settings") or {}).get("waiver_bid", 0), str(t["roster_ids"][0])
            for pid in t["adds"]:
                if t["status"] == "complete":
                    claims.append({"week": t["leg"], "player": pid, "roster": rid, "bid": bid, "at": t["status_updated"]})
                # Other failures (e.g. a full roster) weren't real competing bids.
                elif "claimed by another owner" in ((t.get("metadata") or {}).get("notes") or ""):
                    lost.setdefault((t["leg"], pid), []).append({"roster": rid, "bid": bid})
    players = load_players({c["player"] for c in claims})
    for c in claims:
        p = players.get(c["player"], {"name": c["player"], "pos": ""})
        c["name"], c["pos"] = p["name"], p["pos"]
        c["outbid"] = sorted(lost.get((c["week"], c["player"]), []), key=lambda o: -o["bid"])
    claims.sort(key=lambda c: (c["week"], -c["bid"], c["at"]))
    return {"budget": (league.get("settings") or {}).get("waiver_budget") or 100, "claims": claims, "trades": trades}


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
    faab = WEEKS / season / "faab.json"
    faab.write_text(json.dumps(fetch_faab(seasons[season]), indent=1, ensure_ascii=False), encoding="utf-8")
    HISTORY.write_text(json.dumps(fetch_history(seasons), indent=1, ensure_ascii=False), encoding="utf-8")
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


def sidebar(season, base, site, current=None):
    """Power rankings by season, plus league and history links. `current` marks a non-rankings page."""
    lw = site["latest_week"]
    years = "".join(f'<a href="{base}{s}/week-{lw[s]:02d}.html"{" class=cur" if s == season and not current else ""}>'
                    f'{s}</a>' for s in sorted(lw, reverse=True))
    link = lambda href, label, key: f'<a href="{href}"{" class=cur" if current == key else ""}>{label}</a>'
    league = f'<a href="{html.escape(RULE_BOOK_URL)}" target="_blank" rel="noopener">Rule Book ↗</a>'
    if season in site["faab_seasons"]:
        league += link(f"{base}{season}/faab.html", "FAAB Tracker", "faab")
    groups = [("Power Rankings", years), ("League", league)]
    if site["history"]:
        groups.append(("League History", link(f"{base}history/records.html", "Record Book", "records")
                       + link(f"{base}history/h2h.html", "Head-to-Head", "h2h")))
    return "".join(f'<div class="side-group"><div class="seasons-lbl">{label}</div>'
                   f'<div class="seasons-list">{links}</div></div>' for label, links in groups)


def week_nav(season, weeks, base, current=None):
    return "".join(f'<a href="{base}{season}/week-{w:02d}.html"{" class=cur" if w == current else ""}>Wk {w}</a>'
                   for w in weeks)


def page(data, base, side, nav, kicker, heading, body, footer, description):
    e = html.escape
    logo = f'<img class="logo" src="{e(data["league_avatar"])}" alt="">' if data.get("league_avatar") else ""
    return PAGE.format(site_name=e(SITE_NAME), description=e(description), side=side, logo=logo, kicker=kicker,
                       heading=heading, nav=nav, body=body, footer=footer, css=CSS)


def tiles(title, items):
    cells = "".join(f'<div class="award"><div class="lbl">{label}</div><div class="who">{who}</div>'
                    f'<div class="val">{val}</div></div>' for label, who, val in items)
    return f'<section><h2 class="sec">{title}</h2><div class="awards">{cells}</div></section>'


def awards(data):
    """This week's superlatives, plus season-long luck and lineup awards."""
    teams, ms, e = data["teams"], data.get("matchups") or [], html.escape
    if not ms:
        return ""
    nm = lambda rid: e(teams[rid]["name"])
    score = {m["a"]: m["a_pts"] for m in ms} | {m["b"]: m["b_pts"] for m in ms}
    games = [((m["a"], m["a_pts"]), (m["b"], m["b_pts"])) if m["a_pts"] >= m["b_pts"]
             else ((m["b"], m["b_pts"]), (m["a"], m["a_pts"])) for m in ms]  # (winner, loser)
    margin = lambda g: g[0][1] - g[1][1]
    hi, lo = max(score, key=score.get), min(score, key=score.get)
    blow, close = max(games, key=margin), min(games, key=margin)
    unlucky, lucky = max(games, key=lambda g: g[1][1]), min(games, key=lambda g: g[0][1])
    week = [
        ("Top score", nm(hi), f"{score[hi]:.2f} pts"),
        ("Basement", nm(lo), f"{score[lo]:.2f} pts"),
        ("Biggest blowout", f"{nm(blow[0][0])} over {nm(blow[1][0])}", f"by {margin(blow):.2f}"),
        ("Closest game", f"{nm(close[0][0])} over {nm(close[1][0])}", f"by {margin(close):.2f}"),
        ("Unluckiest loss", nm(unlucky[1][0]), f"{unlucky[1][1]:.2f} pts and still lost"),
        ("Luckiest win", nm(lucky[0][0]), f"won with {lucky[0][1]:.2f} pts"),
    ]
    if any("bench_week" in t for t in teams.values()):
        b = max(teams, key=lambda r: teams[r].get("bench_week", 0))
        miss = teams[b].get("bench_miss")
        week.append(("Benchwarmer", nm(b), f"{teams[b]['bench_week']:.1f} pts left on the bench"
                     + (f" ({e(miss['name'])} scored {miss['pts']:.1f})" if miss else "")))
    out = tiles(f"Week {data['week']} Awards", week)
    if any("luck" in t for t in teams.values()):
        luck = sorted(teams, key=lambda r: teams[r]["luck"])
        bench = sorted(teams, key=lambda r: teams[r]["bench"])
        out += tiles("Season So Far", [
            ("Luckiest team", nm(luck[-1]), f"{teams[luck[-1]]['luck']:+.1f} wins vs. expected"),
            ("Unluckiest team", nm(luck[0]), f"{teams[luck[0]]['luck']:+.1f} wins vs. expected"),
            ("Lineup genius", nm(bench[0]), f"only {teams[bench[0]]['bench']:.1f} pts left on the bench"),
            ("Lineup disaster", nm(bench[-1]), f"{teams[bench[-1]]['bench']:.1f} pts left on the bench"),
        ])
    return out


def render_week(data, content, prev_rank, all_weeks, site, base):
    """`site` describes what's been built (seasons, FAAB, history); `base` is the path back to the site root."""
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
        extra = []
        if "ap_wins" in t:
            extra.append(f"All-play {t['ap_wins']}-{t['ap_losses']}")
        if "luck" in t:
            cls = "up" if t["luck"] >= 0.5 else "down" if t["luck"] <= -0.5 else ""
            extra.append(f'Luck <span class="{cls}">{t["luck"]:+.1f}</span>')
        if "bench_week" in t:
            extra.append(f"{t['bench_week']:.1f} pts left on bench")
        rows.append(f"""
  <li class="rank">
    <div class="rank-num">{i}{move}</div>
    <div class="rank-body">
      <div class="team">{avatar(t)}<div><h2>{e(t['name'])}</h2>
        <div class="meta"><b>{rec(t)}</b> · {t['pf']:.2f} PF{' · ' + t['streak'] if t['streak'] else ''}</div>
        {f'<div class="meta">{" · ".join(extra)}</div>' if extra else ''}</div></div>
      {f'<div class="blurb">{fmt(blurb, base)}</div>' if blurb else ''}
      {f'<div class="tops">{tops}</div>' if tops else ''}
    </div>
  </li>""")

    note = "Records include a game against the league median each week. " if data.get("median_game") else ""
    intro = f'<section class="intro">{fmt(content["intro"], base)}</section>' if content["intro"] else ""
    footer = (f"{note}All-play is the record a team would have playing every other team every week. Luck is actual "
              "head-to-head wins minus the wins that all-play scoring would expect. Bench is how many more points the "
              "best possible lineup would have scored. Top performers are points scored as a starter for that team: "
              "overall is the season through that week, weekly is that week alone. QB1, RB3, etc. is the player's rank "
              "at his position across the NFL over the same span. Stats from Sleeper. Rankings are the commissioner's own.")
    return page(data, base, sidebar(season, base, site), week_nav(season, all_weeks, base, week),
                f"{e(data['league'])} · {e(season)} · Week {week}", e(content["title"]),
                f'{intro}<ol class="ranks">{"".join(rows)}</ol>{awards(data)}', footer,
                f"{season} Week {week} power rankings")


def history_totals(hist):
    """All-time regular-season record per manager, and head-to-head records between each pair."""
    tally = lambda: {"w": 0, "l": 0, "t": 0}
    totals = {uid: {**tally(), "pf": 0.0, "games": 0, "seasons": set(), "titles": 0} for uid in hist["managers"]}
    pairs = {}
    for g in hist["games"]:
        for me, mine, opp, theirs in ((g["a"], g["a_pts"], g["b"], g["b_pts"]), (g["b"], g["b_pts"], g["a"], g["a_pts"])):
            k = "w" if mine > theirs else "l" if mine < theirs else "t"
            totals[me][k] += 1
            totals[me]["pf"] += mine
            totals[me]["games"] += 1
            totals[me]["seasons"].add(g["season"])
            pairs.setdefault((me, opp), tally())[k] += 1
    for f in hist["finishes"]:
        totals[f["champion"]]["titles"] += 1
    pct = lambda r: (r["w"] + r["t"] / 2) / max(r["w"] + r["l"] + r["t"], 1)
    order = sorted(totals, key=lambda u: (-pct(totals[u]), -totals[u]["pf"]))
    return totals, pairs, order, pct


def wlt(r):
    return f"{r['w']}-{r['l']}" + (f"-{r['t']}" if r["t"] else "")


def history_page(hist, data, site, current, heading, body, footer, description):
    base = "../"
    nav = "".join(f'<a href="{base}history/{key}.html"{" class=cur" if key == current else ""}>{label}</a>'
                  for key, label in (("records", "Record Book"), ("h2h", "Head-to-Head")))
    seasons = sorted({g["season"] for g in hist["games"]})
    span = f"{seasons[0]}–{seasons[-1]}" if seasons else ""
    return page(data, base, sidebar(str(data["season"]), base, site, current), nav,
                f"{html.escape(data['league'])} · {span}", heading, body, footer, description)


def render_records(hist, data, site):
    e, mgr, names = html.escape, hist["managers"], hist["names"]
    totals, _, order, pct = history_totals(hist)
    who = lambda uid: (f'<span class="who">{avatar(mgr[uid])}<span>{e(mgr[uid]["name"])}'
                       f'<small>{e(mgr[uid]["manager"])}</small></span></span>')
    team = lambda season, uid: e(names[season].get(uid) or mgr[uid]["name"])

    standings = "".join(
        f'<tr><td class="tn">{who(u)}</td><td>{len(totals[u]["seasons"])}</td><td>{wlt(totals[u])}</td>'
        f'<td>{pct(totals[u]):.3f}</td><td>{totals[u]["pf"] / max(totals[u]["games"], 1):.1f}</td>'
        f'<td>{"🏆" * totals[u]["titles"] or "–"}</td></tr>' for u in order if totals[u]["games"])
    champs = "".join(
        f'<tr><td class="tn">{f["season"]}</td><td class="tn"><b>{team(f["season"], f["champion"])}</b> '
        f'<small>{e(mgr[f["champion"]]["manager"])}</small></td>'
        f'<td class="tn">{team(f["season"], f["runner_up"])} <small>{e(mgr[f["runner_up"]]["manager"])}</small></td></tr>'
        for f in sorted(hist["finishes"], key=lambda f: f["season"], reverse=True))

    scores = [(g["a_pts"], g["a"], g) for g in hist["games"]] + [(g["b_pts"], g["b"], g) for g in hist["games"]]
    when = lambda g: f'{g["season"]} Wk {g["week"]}'
    winner = lambda g: (g["a"], g["a_pts"], g["b"], g["b_pts"]) if g["a_pts"] >= g["b_pts"] else (g["b"], g["b_pts"], g["a"], g["a_pts"])
    margin = lambda g: abs(g["a_pts"] - g["b_pts"])
    game_line = lambda g: (lambda w, wp, l, lp: f'{team(g["season"], w)} over {team(g["season"], l)}')(*winner(g))

    def top5(title, rows):
        items = "".join(f'<li><span class="amt">{val}</span><div>{text}<div class="beat">{sub}</div></div></li>'
                        for val, text, sub in rows[:5])
        return f'<div><h3>{title}</h3><ul class="bids">{items}</ul></div>'

    season_pf, streaks = {}, {}
    for (season, uid) in {(g["season"], u) for g in hist["games"] for u in (g["a"], g["b"])}:
        mine = sorted((g["week"], g["a_pts"] if g["a"] == uid else g["b_pts"], g["b_pts"] if g["a"] == uid else g["a_pts"])
                      for g in hist["games"] if g["season"] == season and uid in (g["a"], g["b"]))
        season_pf[(season, uid)] = (sum(m for _, m, _ in mine), len(mine))
        best = {"W": (0, 0, 0), "L": (0, 0, 0)}
        run, kind, start = 0, None, 0
        for week, m, o in mine:
            res = "W" if m > o else "L" if m < o else "T"
            run, start = (run + 1, start) if res == kind else (1, week)
            kind = res
            if res in best and run > best[res][0]:
                best[res] = (run, start, week)
        streaks[(season, uid)] = best
    streak_rows = lambda k: sorted(((v[k], s) for s, v in streaks.items() if v[k][0]), reverse=True)

    lists = "".join([
        top5("Highest single-week scores", [(f"{p:.1f}", team(g["season"], u), when(g))
                                            for p, u, g in sorted(scores, key=lambda x: -x[0])]),
        top5("Lowest single-week scores", [(f"{p:.1f}", team(g["season"], u), when(g))
                                           for p, u, g in sorted(scores, key=lambda x: x[0])]),
        top5("Biggest blowouts", [(f"+{margin(g):.1f}", game_line(g), when(g))
                                  for g in sorted(hist["games"], key=lambda g: -margin(g))]),
        top5("Closest games", [(f"+{margin(g):.2f}", game_line(g), when(g))
                               for g in sorted(hist["games"], key=margin)]),
        top5("Most points in a season", [(f"{pf:.0f}", team(s, u), f"{s} · {n} games")
                                         for (s, u), (pf, n) in sorted(season_pf.items(), key=lambda kv: -kv[1][0])]),
        top5("Longest winning streaks", [(f"{n}", team(s, u), f"{s} · Wk {a}–{b}") for (n, a, b), (s, u) in streak_rows("W")]),
        top5("Longest losing streaks", [(f"{n}", team(s, u), f"{s} · Wk {a}–{b}") for (n, a, b), (s, u) in streak_rows("L")]),
    ])

    body = f"""<section><h2 class="sec">Champions</h2><div class="tbl"><table>
  <thead><tr><th class="tn">Season</th><th class="tn">Champion</th><th class="tn">Runner-up</th></tr></thead>
  <tbody>{champs or '<tr><td class="tn" colspan="3">No completed seasons yet.</td></tr>'}</tbody></table></div></section>
<section><h2 class="sec">All-Time Standings</h2><div class="tbl"><table>
  <thead><tr><th class="tn">Manager</th><th>Seasons</th><th>Record</th><th>Win %</th><th>PF / game</th><th>Titles</th></tr></thead>
  <tbody>{standings}</tbody></table></div></section>
<section><h2 class="sec">Records</h2><div class="records">{lists}</div></section>"""
    footer = ("Regular-season head-to-head games only; median games and playoffs aren't counted in records or streaks. "
              "Managers are tracked across seasons by their Sleeper account and shown with their latest team name; "
              "records show the team name from that season.")
    return history_page(hist, data, site, "records", "Record Book", body, footer, "League records and champions")


def render_h2h(hist, data, site):
    e, mgr = html.escape, hist["managers"]
    totals, pairs, order, pct = history_totals(hist)
    order = [u for u in order if totals[u]["games"]]
    head = "".join(f'<th class="col" title="{e(mgr[u]["name"])}">{avatar(mgr[u])}<span>{e(mgr[u]["manager"])}</span></th>'
                   for u in order)
    rows = []
    for me in order:
        cells = []
        for opp in order:
            r = pairs.get((me, opp))
            if me == opp:
                cells.append('<td class="self"></td>')
            elif not r:
                cells.append('<td class="none">–</td>')
            else:
                cls = "win" if r["w"] > r["l"] else "loss" if r["w"] < r["l"] else "even"
                cells.append(f'<td class="{cls}">{wlt(r)}</td>')
        rows.append(f'<tr><td class="tn"><span class="who">{avatar(mgr[me])}<span>{e(mgr[me]["name"])}'
                    f'<small>{e(mgr[me]["manager"])}</small></span></span></td>'
                    f'<td class="tot">{wlt(totals[me])}</td>{"".join(cells)}</tr>')
    body = f"""<section><div class="tbl"><table class="h2h">
  <thead><tr><th class="tn">Manager</th><th>All-time</th>{head}</tr></thead>
  <tbody>{"".join(rows)}</tbody></table></div></section>"""
    footer = ("Read across a row: that manager's regular-season record against each opponent, across every season. "
              "Green means they lead the series. Median games and playoffs aren't counted.")
    return history_page(hist, data, site, "h2h", "Head-to-Head", body, footer, "All-time head-to-head records")


def render_faab(faab, data, site):
    """Budget table and bid log for one season. `data` is the season's latest week, for team names and avatars."""
    teams, season, e, base = data["teams"], str(data["season"]), html.escape, "../"
    budget, claims, trades = faab["budget"], faab["claims"], faab.get("trades", [])
    name = lambda rid: teams.get(rid, {}).get("name", f"Team {rid}")
    money = lambda n: f"${n:,}"
    spent = {rid: sum(c["bid"] for c in claims if c["roster"] == rid) for rid in teams}
    traded = {rid: sum(x["amount"] for x in trades if x["to"] == rid) - sum(x["amount"] for x in trades if x["from"] == rid)
              for rid in teams}

    table = []
    for rid in sorted(teams, key=lambda r: (-spent[r], name(r).lower())):
        mine = [c for c in claims if c["roster"] == rid]
        left = max(budget + traded[rid] - spent[rid], 0)
        big = max(mine, key=lambda c: (c["bid"], -c["week"]), default=None)
        big_txt = f'{e(big["name"])}, {money(big["bid"])}, Wk {big["week"]}' if big and big["bid"] else "–"
        tr = traded[rid]
        tr_txt = f'<span class="up">+{money(tr)}</span>' if tr > 0 else f'<span class="down">−{money(-tr)}</span>' if tr < 0 else "–"
        table.append(f"""<tr><td class="tn"><span class="who">{avatar(teams[rid])}<span>{e(name(rid))}</span></span></td>
      <td>{money(spent[rid])}</td>{f"<td>{tr_txt}</td>" if trades else ""}
      <td><div class="left"><span>{money(left)}</span><span class="bar"><span style="width:{min(100, 100 * left / budget):.0f}%"></span></span></div></td>
      <td>{len(mine)}</td><td class="tn">{big_txt}</td></tr>""")

    log = []
    for w in sorted({c["week"] for c in claims} | {x["week"] for x in trades}, reverse=True):
        items = [f"""<li class="trade"><span class="amt">{money(x['amount'])}</span>
        <div><b>{e(name(x['from']))}</b> sent {money(x['amount'])} FAAB to <b>{e(name(x['to']))}</b> in a trade</div></li>"""
                 for x in trades if x["week"] == w]
        for c in (c for c in claims if c["week"] == w):
            beat = ""
            if c["outbid"]:
                others = ", ".join(f'{e(name(o["roster"]))}’s {money(o["bid"])}' for o in c["outbid"])
                margin = c["bid"] - c["outbid"][0]["bid"]
                beat = (f'<div class="beat">outbidding {others} · '
                        + (f'by {money(margin)}' if margin > 0 else 'won the tiebreaker') + '</div>')
            items.append(f"""<li{' class="zero"' if not c['bid'] else ''}><span class="amt">{money(c['bid'])}</span>
        <div><b>{e(name(c['roster']))}</b> won <b>{e(c['name'])}</b> <span class="pos">{e(c['pos'])}</span>{beat}</div></li>""")
        log.append(f'<h3>{f"Week {w}" if w else "Preseason"}</h3><ul class="bids">{"".join(items)}</ul>')

    body = f"""<section><h2 class="sec">Budget</h2><div class="tbl"><table>
  <thead><tr><th class="tn">Team</th><th>Spent</th>{"<th>Traded</th>" if trades else ""}<th>Remaining</th><th>Claims won</th><th class="tn">Biggest buy</th></tr></thead>
  <tbody>{"".join(table)}</tbody></table></div></section>
<section><h2 class="sec">Bid log</h2>{"".join(log) or "<p>No waiver claims yet.</p>"}</section>"""
    footer = (f"Each team starts with {money(budget)} of FAAB; remaining includes FAAB traded between teams. Winning "
              "waiver claims from Sleeper, newest week first. “Outbidding” lists the other teams that bid on the same "
              "player that week.")
    seasons_nav = "".join(f'<a href="{base}{s}/faab.html"{" class=cur" if s == season else ""}>{s}</a>'
                          for s in sorted(site["faab_seasons"], reverse=True))
    return page(data, base, sidebar(season, base, site, "faab"), seasons_nav,
                f"{e(data['league'])} · {e(season)}", "FAAB Tracker", body, footer, f"{season} FAAB spending")


def build():
    seasons = sorted(d.name for d in WEEKS.iterdir() if d.is_dir() and weeks_written(d.name)) if WEEKS.exists() else []
    if not seasons:
        sys.exit("No weeks yet. Run: py ffr.py new <week>")
    site = {"latest_week": {s: weeks_written(s)[-1] for s in seasons},
            "faab_seasons": {s for s in seasons if (WEEKS / s / "faab.json").exists()},
            "history": HISTORY.exists()}
    faab_seasons = site["faab_seasons"]
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
            out = render_week(data, content, prev_rank, weeks, site, "../")
            (SITE / s / f"week-{w:02d}.html").write_text(out, encoding="utf-8")
            if w == weeks[-1]:
                (SITE / s / "index.html").write_text(out, encoding="utf-8")
                if s == seasons[-1]:
                    root = render_week(data, content, prev_rank, weeks, site, "")
                    (SITE / "index.html").write_text(root, encoding="utf-8")
            prev_rank = [r for r, _ in content["rankings"]]
        if s in faab_seasons:
            faab = json.loads((WEEKS / s / "faab.json").read_text(encoding="utf-8"))
            (SITE / s / "faab.html").write_text(render_faab(faab, data, site),
                                                encoding="utf-8")
        print(f"Built {s}: {len(weeks)} week(s)" + (" + FAAB" if s in faab_seasons else ""))
    if site["history"]:
        hist = json.loads(HISTORY.read_text(encoding="utf-8"))
        (SITE / "history").mkdir(exist_ok=True)
        (SITE / "history" / "records.html").write_text(render_records(hist, data, site), encoding="utf-8")
        (SITE / "history" / "h2h.html").write_text(render_h2h(hist, data, site), encoding="utf-8")
        print("Built league history")
    print("Open docs/index.html to preview.")


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{site_name}</title>
<meta property="og:title" content="{site_name}">
<meta property="og:site_name" content="{site_name}">
<meta property="og:description" content="{description}">
<style>{css}</style></head>
<body><div class="layout">
<aside class="seasons">{side}</aside>
<main class="wrap">
<header>{logo}<div><div class="kicker">{kicker}</div><h1>{heading}</h1></div></header>
<nav>{nav}</nav>
{body}
<footer>{footer}</footer>
</main></div></body></html>
"""

CSS = """
:root{--bg:#f6f5f2;--card:#fff;--ink:#1b1d21;--muted:#6b6f78;--line:#e4e2dc;--chip:#f0eee9;--up:#1f8a4c;--down:#c8402f;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#121417;--card:#1b1e23;--ink:#eceef1;--muted:#9aa0aa;--line:#2b2f36;--chip:#252930;--up:#4cc27e;--down:#ff7a66;color-scheme:dark}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.layout{display:flex;max-width:1180px;margin:0 auto}
.seasons{flex:none;width:172px;padding:40px 16px 0;position:sticky;top:0;align-self:flex-start}
.side-group+.side-group{margin-top:28px}
.seasons-lbl{color:var(--muted);font-size:.85rem;font-weight:600;margin:0 0 6px 10px}
.seasons-list{display:flex;flex-direction:column;gap:2px}
.seasons a{white-space:nowrap;padding:7px 10px;border-radius:8px;color:var(--muted);text-decoration:none;font-weight:600;font-variant-numeric:tabular-nums}
.seasons a:hover{background:var(--chip);color:var(--ink)}
.seasons a.cur{background:var(--card);color:var(--ink);box-shadow:inset 3px 0 0 var(--ink),0 0 0 1px var(--line)}
.wrap{flex:1;min-width:0;max-width:1000px;padding:32px 16px 64px}
@media (max-width:760px){
  .layout{display:block}
  .seasons{position:static;width:auto;padding:16px 16px 0;display:flex;flex-wrap:wrap;gap:10px 24px}
  .side-group{display:flex;align-items:center;gap:10px}
  .side-group+.side-group{margin-top:0}
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
.sec{font-size:1.2rem;margin:8px 0 12px}
section+section .sec{margin-top:36px}
.tbl{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:14px}
table{border-collapse:collapse;width:100%;font-size:.92rem;font-variant-numeric:tabular-nums}
th,td{padding:10px 14px;text-align:right;white-space:nowrap;border-bottom:1px solid var(--line)}
tbody tr:last-child td{border-bottom:0}
th{color:var(--muted);font-weight:600;font-size:.75rem;text-transform:uppercase;letter-spacing:.04em}
.tn{text-align:left}
.who{display:flex;align-items:center;gap:10px;font-weight:600}.who .av{width:28px;height:28px;font-size:.75rem}
.left{display:flex;align-items:center;gap:10px;justify-content:flex-end}
.bar{width:90px;height:8px;border-radius:99px;background:var(--chip);overflow:hidden}
.bar span{display:block;height:100%;background:var(--up);border-radius:99px}
h3{font-size:.95rem;margin:22px 0 8px;color:var(--muted)}
.bids{list-style:none;padding:0;margin:0;background:var(--card);border:1px solid var(--line);border-radius:14px}
.bids li{display:flex;gap:14px;align-items:baseline;padding:10px 16px;border-bottom:1px solid var(--line)}
.bids li:last-child{border-bottom:0}
.bids .amt{flex:none;min-width:3.2em;text-align:right;font-weight:800;font-variant-numeric:tabular-nums}
.bids .zero .amt{color:var(--muted);font-weight:600}
.bids .pos{color:var(--muted);font-size:.72rem;font-weight:700}
.bids .beat{color:var(--muted);font-size:.85rem}
.awards{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:10px}
.award{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.award .lbl{color:var(--muted);font-size:.72rem;text-transform:uppercase;letter-spacing:.05em}
.award .who{font-weight:700;margin-top:2px}
.award .val{color:var(--muted);font-size:.88rem}
section+section .sec,.ranks+section .sec{margin-top:36px}
.who small,.tn small{display:block;color:var(--muted);font-weight:400;font-size:.75rem}
.tn small{display:inline}
.tn .who small{display:block}
.records{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:4px 16px}
.records h3{margin-top:14px}
.records .amt{min-width:3.6em}
.h2h td,.h2h th{padding:8px 10px;text-align:center}
.h2h th.col{vertical-align:bottom}
.h2h th.col .av{width:26px;height:26px;display:flex;margin:0 auto 4px;font-size:.7rem}
.h2h th.col span{display:block;font-size:.7rem;text-transform:none;letter-spacing:0;max-width:72px;overflow:hidden;text-overflow:ellipsis}
.h2h td.tn,.h2h th.tn{text-align:left}
.h2h .tot{font-weight:700}
.h2h .win{background:color-mix(in srgb,var(--up) 16%,transparent);color:var(--up);font-weight:600}
.h2h .loss{background:color-mix(in srgb,var(--down) 13%,transparent);color:var(--down)}
.h2h .even{color:var(--muted)}
.h2h .none,.h2h .self{color:var(--line)}
.h2h .self{background:var(--chip)}
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
