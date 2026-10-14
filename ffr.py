"""Fantasy football power rankings: pull Sleeper data, set your rankings, build a web page.

Usage:
  py ffr.py setup <sleeper-username>   Pick this season's league and save it to config.json (once per year)
  py ffr.py new <week> [season]        Fetch data through that week and create weeks/<season>/week-NN.txt
  py ffr.py new <first>-<last>         Same, for a range of weeks (e.g. new 1-4 to backfill)
  py ffr.py build                      Turn every season's week files into the website in docs/
  py ffr.py refresh                    Re-pull stats for the latest ranked week and rebuild (rankings untouched)

The season defaults to the newest one in config.json.
"""

import datetime
import html
import json
import random
import re
import shutil
import statistics
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
            "owner": r.get("owner_id"),
            "avatar": avatar_url(u) if u else None,
            "wins": 0, "losses": 0, "ties": 0, "pf": 0.0, "results": [],
        }
        starter_pts[r["roster_id"]] = {}
        week_pts[r["roster_id"]] = {}

    this_week, scores = [], {r: [] for r in teams}
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

        if regular and len(pts) > 1:
            for rid, p in pts.items():
                scores[rid].append(p)

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
            if w == week:
                this_week.append({"a": str(a), "a_pts": round(pts[a], 2), "b": str(b), "b_pts": round(pts[b], 2)})

    if week < playoff_start:
        remaining = []
        for w in range(week + 1, playoff_start):
            pairs = {}
            for e in get(f"/league/{league_id}/matchups/{w}") or []:
                if e.get("matchup_id"):
                    pairs.setdefault(e["matchup_id"], []).append(e["roster_id"])
            remaining.append([tuple(p) for p in pairs.values() if len(p) == 2])
        odds = playoff_odds(league, teams, scores, remaining, seed=int(league["season"]) * 100 + week)
        for rid, o in odds.items():
            teams[rid]["odds"] = o

    top3 = lambda pts: sorted(pts.items(), key=lambda kv: -kv[1])[:3]
    top = {rid: top3(totals) for rid, totals in starter_pts.items()}
    top_week = {rid: top3(pts) for rid, pts in week_pts.items()}
    players = load_players({pid for picks in [*top.values(), *top_week.values()] for pid, _ in picks})
    ranks, week_ranks = position_ranks(league["season"], week, league.get("scoring_settings") or {}, players)
    who = lambda pid: {k: players.get(pid, {"name": pid, "pos": ""})[k] for k in ("name", "pos")}
    card = lambda pid, p, r: {**who(pid), "rank": r.get(pid, ""), "pts": round(p, 2)}
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
        "matchups": this_week,
    }


def fetch_history(seasons):
    """Every regular-season game and final placing across all seasons, keyed by manager (Sleeper user id)."""
    managers, games, finishes, names, playoffs = {}, [], [], {}, {}
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
            bracket = get(f"/league/{league_id}/winners_bracket") or []
            for m in bracket:
                if m.get("p") == 1 and m.get("w"):
                    finishes.append({"season": season, "champion": owner[m["w"]], "runner_up": owner[m["l"]]})
            playoffs[season] = playoff_results(bracket, owner)
    return {"managers": managers, "names": names, "games": games, "finishes": finishes, "playoffs": playoffs}


def playoff_results(bracket, owner):
    """{manager: "🏆 Champion" | "Runner-up" | "3rd · lost semifinal" ...} for every team in a finished bracket."""
    if not bracket:
        return {}
    last = max(m["r"] for m in bracket)
    stage = {last: "final", last - 1: "semifinal", last - 2: "quarterfinal"}
    place = {}
    for m in bracket:
        if m.get("p") and m.get("w"):
            place[m["w"]], place[m["l"]] = m["p"], m["p"] + 1
    lost = {m["l"]: stage.get(m["r"], f"round {m['r']}") for m in bracket if not m.get("p") and m.get("l")}
    out = {}
    for rid in {t for m in bracket for t in (m.get("t1"), m.get("t2")) if t}:
        n = place.get(rid)
        out[owner.get(rid)] = ("🏆 Champion" if n == 1 else "Runner-up" if n == 2 else
                               " · ".join(filter(None, [n and ordinal(n), rid in lost and f"lost {lost[rid]}"])))
    out.pop(None, None)
    return out


def season_opener(season):
    """Thursday of NFL week 1: the Thursday after Labor Day (first Monday of September)."""
    first = datetime.date(int(season), 9, 1)
    labor_day = first + datetime.timedelta(days=(7 - first.weekday()) % 7)
    return labor_day + datetime.timedelta(days=3)


def faab_week(season, leg, at):
    """The week a transaction counts toward. Sleeper keeps calling it week N until the waivers after week N's games,
    so anything after that week's Monday night game counts toward week N + 1."""
    tuesday = season_opener(season) + datetime.timedelta(days=7 * (leg - 1) + 5)
    done = datetime.datetime.combine(tuesday, datetime.time(10), datetime.timezone.utc)  # well after MNF ends
    return leg + 1 if leg and at >= done.timestamp() * 1000 else leg


def fetch_faab(league_id):
    """Every winning waiver claim this season, with the competing bids it beat, plus FAAB traded between teams."""
    league = get(f"/league/{league_id}")
    claims, lost, trades = [], {}, []
    for w in range(0, 19):
        for t in get(f"/league/{league_id}/transactions/{w}") or []:
            week = faab_week(league["season"], t["leg"], t["status_updated"])
            if t["status"] == "complete":
                trades += [{"week": week, "from": str(x["sender"]), "to": str(x["receiver"]), "amount": x["amount"]}
                           for x in t.get("waiver_budget") or []]
            if t.get("type") != "waiver" or not t.get("adds"):
                continue
            bid, rid = (t.get("settings") or {}).get("waiver_bid", 0), str(t["roster_ids"][0])
            for pid in t["adds"]:
                if t["status"] == "complete":
                    claims.append({"week": week, "player": pid, "roster": rid, "bid": bid, "at": t["status_updated"]})
                # Other failures (e.g. a full roster) weren't real competing bids.
                elif "claimed by another owner" in ((t.get("metadata") or {}).get("notes") or ""):
                    lost.setdefault((week, pid), []).append({"roster": rid, "bid": bid})
    players = load_players({c["player"] for c in claims})
    rostered, last = rostered_points(league_id)
    for c in claims:
        c.update(tenure(rostered, last, c["roster"], c["player"], c["week"]))
        p = players.get(c["player"], {"name": c["player"], "pos": ""})
        c["name"], c["pos"] = p["name"], p["pos"]
        c["outbid"] = sorted(lost.get((c["week"], c["player"]), []), key=lambda o: -o["bid"])
    claims.sort(key=lambda c: (c["week"], -c["bid"], c["at"]))
    return {"budget": (league.get("settings") or {}).get("waiver_budget") or 100, "claims": claims, "trades": trades}


def started_points(league_id):
    """{(roster_id, player_id): {week: points}} for every player in a starting lineup, all scored weeks."""
    league = get(f"/league/{league_id}")
    last = min((league.get("settings") or {}).get("last_scored_leg") or 0, 18)
    out = {}
    for w in range(1, last + 1):
        for e in get(f"/league/{league_id}/matchups/{w}") or []:
            for pid, p in zip(e.get("starters") or [], e.get("starters_points") or []):
                if pid and pid != "0":
                    out.setdefault((str(e["roster_id"]), pid), {})[w] = p or 0
    return out


def rostered_points(league_id):
    """{(roster_id, player_id): {week: (points, points if started else 0)}} for everyone on a roster each week."""
    league = get(f"/league/{league_id}")
    last = min((league.get("settings") or {}).get("last_scored_leg") or 0, 18)
    out = {}
    for w in range(1, last + 1):
        for e in get(f"/league/{league_id}/matchups/{w}") or []:
            starters, pts = set(e.get("starters") or []), e.get("players_points") or {}
            for pid in e.get("players") or []:
                p = pts.get(pid) or 0
                out.setdefault((str(e["roster_id"]), pid), {})[w] = (p, p if pid in starters else 0)
    return out, last


def tenure(rostered, last, rid, pid, week):
    """What a player did for `rid` after joining in `week`: from his first game there until the first week he's
    gone, so a player who's dropped and re-claimed isn't counted twice."""
    weeks = rostered.get((rid, pid), {})
    total = started = 0.0
    played, active = 0, False
    for w in range(week, last + 1):
        if w not in weeks:
            if played or w > week + 1:  # gone, or never showed up
                break
            continue
        played += 1
        total += weeks[w][0]
        started += weeks[w][1]
        active = w == last
    return {"pts": round(total, 2), "started": round(started, 2), "weeks": played, "active": active}


def points_after(started, rid, pid, week):
    """Points a player scored in `rid`'s starting lineup from `week` on."""
    return round(sum(p for w, p in started.get((rid, pid), {}).items() if w >= week), 2)


def fetch_trades(league_id):
    """Every completed trade, with what each side received and how much those players scored for them afterward."""
    started = started_points(league_id)
    trades, ids = [], set()
    for w in range(0, 19):
        for t in get(f"/league/{league_id}/transactions/{w}") or []:
            if t.get("type") != "trade" or t["status"] != "complete":
                continue
            sides = []
            for rid in map(str, t["roster_ids"]):
                got = [pid for pid, r in (t.get("adds") or {}).items() if str(r) == rid]
                ids.update(got)
                sides.append({
                    "roster": rid,
                    "players": [{"player": pid, "pts": points_after(started, rid, pid, t["leg"])} for pid in got],
                    "picks": [{"season": p["season"], "round": p["round"], "from": str(p["roster_id"])}
                              for p in t.get("draft_picks") or [] if str(p["owner_id"]) == rid],
                    "faab": sum(x["amount"] for x in t.get("waiver_budget") or [] if str(x["receiver"]) == rid),
                })
            trades.append({"week": t["leg"], "at": t["status_updated"], "sides": sides})
    players = load_players(ids)
    for t in trades:
        for s in t["sides"]:
            for p in s["players"]:
                info = players.get(p["player"], {"name": p["player"], "pos": ""})
                p["name"], p["pos"] = info["name"], info["pos"]
            s["total"] = round(sum(p["pts"] for p in s["players"]), 2)
    return sorted(trades, key=lambda t: t["at"])


def fetch_draft(league_id):
    """Each draft pick's season finish at his position compared with where he went at that position."""
    league = get(f"/league/{league_id}")
    settings = league.get("settings") or {}
    drafts = [d for d in get(f"/league/{league_id}/drafts") or [] if d.get("status") == "complete"]
    if not drafts:
        return None
    picks = sorted(get(f"/draft/{drafts[0]['draft_id']}/picks") or [], key=lambda p: p["pick_no"])
    last = min((settings.get("playoff_week_start") or 15) - 1, settings.get("last_scored_leg") or 0)
    scoring = league.get("scoring_settings") or {}
    totals = {}
    for w in range(1, last + 1):
        for pid, stats in get(f"/stats/nfl/regular/{league['season']}/{w}").items():
            totals[pid] = totals.get(pid, 0) + sum(stats[k] * v for k, v in scoring.items()
                                                   if isinstance(stats.get(k), (int, float)))
    players = load_players({p["player_id"] for p in picks})
    pos_of = lambda pid: players.get(pid, {}).get("pos") or ""
    by_pos = {}
    for pid, pts in totals.items():
        if pts > 0 and pos_of(pid):
            by_pos.setdefault(pos_of(pid), []).append((pts, pid))
    finish = {pid: i for ranked in by_pos.values() for i, (_, pid) in enumerate(sorted(ranked, reverse=True), 1)}
    rostered, _ = rostered_points(league_id)
    taken, out = {}, []
    for p in picks:
        pid, pos = p["player_id"], pos_of(p["player_id"])
        taken[pos] = taken.get(pos, 0) + 1
        # Someone who never scored finishes behind everyone who did.
        fin = finish.get(pid, len(by_pos.get(pos, [])) + 1)
        stay = tenure(rostered, last, str(p["roster_id"]), pid, 1)
        out.append({"kept": stay["active"], "team_pts": stay["pts"], "team_weeks": stay["weeks"],"round": p["round"], "pick": p["pick_no"], "roster": str(p["roster_id"]), "owner": p.get("picked_by"),
                    "name": players.get(pid, {}).get("name", pid), "pos": pos, "pts": round(totals.get(pid, 0), 2),
                    "drafted": taken[pos], "finish": fin, "value": taken[pos] - fin, "keeper": bool(p.get("is_keeper"))})
    return {"through_week": last, "complete": league.get("status") == "complete", "picks": out}


ODDS_SHRINK = 6   # games of league-average scoring blended into each team's average
ODDS_DRIFT = 12   # points a team's true strength can differ from its average so far, per simulated season


def playoff_odds(league, teams, scores, remaining, sims=4000, seed=0, shrink=ODDS_SHRINK, drift=ODDS_DRIFT):
    """Simulate the rest of the regular season and the bracket. `scores` maps roster id to its weekly scores so far;
    `remaining` is a list of weeks, each a list of (a, b) pairings. Returns {roster_id: {round: % of sims reached}}
    for playoffs, bye, semis, final and champ."""
    settings = league.get("settings") or {}
    n_playoff = settings.get("playoff_teams") or 6
    median_game = settings.get("league_average_match") == 1
    played = [s for v in scores.values() for s in v]
    if len(played) < 2 or n_playoff not in (4, 6, 8):
        return {}
    avg, sd = statistics.mean(played), statistics.pstdev(played) or 1
    # A few weeks of scores say little about true strength: pull each team toward the league average, and let its
    # strength wander in every simulated season (injuries, trades, slumps).
    base = {r: (sum(v) + shrink * avg) / (len(v) + shrink) for r, v in scores.items()}
    rng = random.Random(seed)
    mean = dict(base)
    reached = {r: dict.fromkeys(("playoffs", "bye", "semis", "final", "champ"), 0) for r in teams}

    def game(a, b):
        return a if rng.gauss(mean[a], sd) >= rng.gauss(mean[b], sd) else b

    for _ in range(sims):
        mean.update({r: base[r] + rng.gauss(0, drift) for r in base})
        wins = {r: teams[r]["wins"] + teams[r]["ties"] / 2 for r in teams}
        pf = {r: teams[r]["pf"] for r in teams}
        for pairs in remaining:
            week = {r: rng.gauss(mean[r], sd) for r in teams}
            for a, b in pairs:
                wins[a if week[a] > week[b] else b] += 1
            for r in teams:
                pf[r] += week[r]
            if median_game:
                for r in sorted(week, key=week.get, reverse=True)[:len(week) // 2]:
                    wins[r] += 1
        s = sorted(teams, key=lambda r: (-wins[r], -pf[r]))[:n_playoff]
        for r in s:
            reached[r]["playoffs"] += 1
        # Brackets aren't reseeded: 1 meets the 4/5 winner, 2 meets the 3/6 winner.
        if n_playoff == 4:
            semis = s
        elif n_playoff == 6:
            for r in s[:2]:
                reached[r]["bye"] += 1
            semis = [s[0], game(s[3], s[4]), s[1], game(s[2], s[5])]
        else:
            semis = [game(s[0], s[7]), game(s[3], s[4]), game(s[1], s[6]), game(s[2], s[5])]
        final = [game(semis[0], semis[1]), game(semis[2], semis[3])]
        for r in semis:
            reached[r]["semis"] += 1
        for r in final:
            reached[r]["final"] += 1
        reached[game(*final)]["champ"] += 1
    # Only call a race settled when no regular-season games remain; a 4,000-sim sweep isn't a clinch.
    return {r: {**{k: round(100 * v / sims, 2) for k, v in counts.items()}, "settled": not remaining}
            for r, counts in reached.items()}


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
    for name, fetch in (("trades", fetch_trades), ("draft", fetch_draft)):
        (WEEKS / season / f"{name}.json").write_text(json.dumps(fetch(seasons[season]), indent=1, ensure_ascii=False),
                                                     encoding="utf-8")
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
    for key, label, have in (("faab", "FAAB Tracker", "faab_seasons"), ("trades", "Trade Grades", "trade_seasons"),
                             ("draft", "Draft Report Card", "draft_seasons")):
        if season in site.get(have, ()):
            league += link(f"{base}{season}/{key}.html", label, key)
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
    """This week's superlatives."""
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
    return tiles(f"Week {data['week']} Awards", week)


def render_week(data, content, prev_rank, all_weeks, site, base, rank_history=()):
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

    note = "Records include a game against the league median each week. " if data.get("median_game") else ""
    intro = f'<section class="intro">{fmt(content["intro"], base)}</section>' if content["intro"] else ""
    footer = (f"{note}Top performers are points scored as a starter for that team: "
              "overall is the season through that week, weekly is that week alone. QB1, RB3, etc. is the player's rank "
              "at his position across the NFL over the same span. Stats from Sleeper. Rankings are the commissioner's own.")
    return page(data, base, sidebar(season, base, site), week_nav(season, all_weeks, base, week),
                f"{e(data['league'])} · {e(season)} · Week {week}", e(content["title"]),
                f'{intro}<ol class="ranks">{"".join(rows)}</ol>{odds_table(data)}{rank_chart(rank_history, teams)}{awards(data)}', footer,
                f"{season} Week {week} power rankings")


def rank_chart(history, teams):
    """Bump chart of power rankings by week. All lines are neutral; hovering or tapping a team highlights it."""
    if len(history) < 2:
        return ""
    e, n, weeks = html.escape, len(teams), [w for w, _ in history]
    left, right, top, row = 44, 210, 18, 30
    width, height = 960, top + (n - 1) * row + 44
    step = (width - left - right) / (len(weeks) - 1)
    x = lambda i: left + i * step
    y = lambda rank: top + (rank - 1) * row
    grid = "".join(f'<text class="ax" x="{left - 14}" y="{y(r) + 4}" text-anchor="end">{r}</text>' for r in range(1, n + 1))
    grid += "".join(f'<line class="gl" x1="{x(i):.1f}" x2="{x(i):.1f}" y1="{top - 8}" y2="{y(n) + 8}"/>'
                    f'<text class="ax" x="{x(i):.1f}" y="{y(n) + 30}" text-anchor="middle">Wk {w}</text>'
                    for i, w in enumerate(weeks))
    series = []
    for rid, t in teams.items():
        ranks = [(i, order.index(rid) + 1) for i, (_, order) in enumerate(history) if rid in order]
        if not ranks:
            continue
        pts = " ".join(f"{x(i):.1f},{y(r)}" for i, r in ranks)
        dots = "".join(f'<circle cx="{x(i):.1f}" cy="{y(r)}" r="4" data-tip="{e(t["name"])} · Wk {weeks[i]}: #{r}"/>'
                       for i, r in ranks)
        li, lr = ranks[-1]
        series.append(f'<g class="series" tabindex="0"><polyline class="hit" points="{pts}"/>'
                      f'<polyline class="ln" points="{pts}"/>{dots}'
                      f'<text class="nm" x="{x(li) + 12:.1f}" y="{y(lr) + 4}">{e(t["name"])}</text></g>')
    table_head = "".join(f"<th>Wk {w}</th>" for w in weeks)
    table_rows = "".join(
        f'<tr><td class="tn">{e(t["name"])}</td>'
        + "".join(f"<td>{order.index(rid) + 1 if rid in order else '–'}</td>" for _, order in history) + "</tr>"
        for rid, t in sorted(teams.items(), key=lambda kv: history[-1][1].index(kv[0]) if kv[0] in history[-1][1] else 99))
    return f"""<section class="trend"><h2 class="sec">Rankings Over Time</h2>
<p class="hint">Hover or tap a team to follow its line.</p>
<div class="chart"><svg class="bump" viewBox="0 0 {width} {height}" role="img" aria-label="Power ranking of each team by week">{grid}{"".join(series)}</svg>
<div class="tip" hidden></div></div>
<details class="tview"><summary>Show as table</summary><div class="tbl"><table><thead><tr><th class="tn">Team</th>{table_head}</tr></thead>
<tbody>{table_rows}</tbody></table></div></details></section>
<script>
document.querySelectorAll('.chart').forEach(function (chart) {{
  var svg = chart.querySelector('svg'), tip = chart.querySelector('.tip'), pinned = null;
  function focus(g) {{
    svg.querySelectorAll('.series').forEach(function (s) {{ s.classList.toggle('on', s === g); }});
    svg.classList.toggle('has-on', !!g);
    if (g) g.parentNode.appendChild(g);
  }}
  svg.querySelectorAll('.series').forEach(function (g) {{
    g.addEventListener('mouseenter', function () {{ if (!pinned) focus(g); }});
    g.addEventListener('click', function () {{ pinned = pinned === g ? null : g; focus(pinned || g); }});
    g.addEventListener('focus', function () {{ focus(g); }});
  }});
  svg.addEventListener('mouseleave', function () {{ if (!pinned) focus(null); tip.hidden = true; }});
  svg.addEventListener('mousemove', function (ev) {{
    var c = ev.target.closest('circle');
    if (!c || !c.parentNode.classList.contains('on')) {{ tip.hidden = true; return; }}
    var r = chart.getBoundingClientRect(), b = c.getBoundingClientRect();
    tip.textContent = c.dataset.tip; tip.hidden = false;
    tip.style.left = (b.left - r.left + b.width / 2) + 'px'; tip.style.top = (b.top - r.top - 8) + 'px';
  }});
}});
</script>"""


def team_label(teams, rid):
    t = teams.get(rid, {"name": f"Team {rid}"})
    return f'<span class="who">{avatar(t) if "avatar" in t else ""}<span>{html.escape(t["name"])}</span></span>'


def render_trades(trades, data, site):
    teams, season, e, base = data["teams"], str(data["season"]), html.escape, "../"
    cards = []
    for t in reversed(trades):
        best = max(t["sides"], key=lambda s: s["total"])
        sides = []
        for s in t["sides"]:
            items = "".join(f'<li><span class="pos">{e(p["pos"])}</span>{e(p["name"])}<span class="pp">{p["pts"]:.1f}</span></li>'
                            for p in s["players"])
            items += "".join(f'<li class="note">{p["season"]} round {p["round"]} pick'
                             + (f' (from {e(teams.get(p["from"], {}).get("name", "?"))})' if p["from"] != s["roster"] else "")
                             + "</li>" for p in s["picks"])
            if s["faab"]:
                items += f'<li class="note">${s["faab"]} FAAB</li>'
            win = s is best and best["total"] > 0 and len(t["sides"]) > 1
            sides.append(f'<div class="side{" lead" if win else ""}">{team_label(teams, s["roster"])}'
                         f'<div class="lbl">Received</div><ul class="got">{items or "<li class=note>Nothing</li>"}</ul>'
                         f'<div class="tot">{s["total"]:.1f} pts</div></div>')
        others = sorted((s["total"] for s in t["sides"] if s is not best), reverse=True)
        if best["total"] <= 0:
            verdict = "No points scored yet by anyone involved"
        else:
            verdict = (f'<b>{e(teams.get(best["roster"], {}).get("name", "?"))}</b> '
                       f'{"is winning" if not site.get("complete", {}).get(season) else "won"} '
                       f'by {best["total"] - (others[0] if others else 0):.1f} pts')
        cards.append(f'<article class="deal"><div class="when">Week {t["week"]}</div>'
                     f'<div class="sides">{"".join(sides)}</div><div class="verdict">{verdict}</div></article>')
    body = "".join(cards) or "<p>No trades yet this season.</p>"
    footer = ("Points are what each player scored in the receiving team's starting lineup after the trade. Draft picks "
              "and FAAB are listed but not scored.")
    nav = "".join(f'<a href="{base}{s}/trades.html"{" class=cur" if s == season else ""}>{s}</a>'
                  for s in sorted(site["trade_seasons"], reverse=True))
    return page(data, base, sidebar(season, base, site, "trades"), nav, f"{e(data['league'])} · {e(season)}",
                "Trade Grades", body, footer, f"{season} trade grades")


DRAFT_CAP = 25  # a 13th-round pick finishing 40 spots high shouldn't outweigh everything else


def draft_value(p):
    """A player the drafting team dropped or traded can't earn them credit for what he did elsewhere."""
    return p["value"] if p.get("kept", True) else min(p["value"], 0)


def draft_grade(picks):
    vals = [max(-DRAFT_CAP, min(DRAFT_CAP, draft_value(p))) for p in picks]
    return sum(vals) / len(vals) if vals else 0


def render_draft(draft, data, site, all_drafts, managers):
    teams, season, e, base = data["teams"], str(data["season"]), html.escape, "../"
    picks = [p for p in draft["picks"] if not p["keeper"]]
    tname = lambda rid: e(teams.get(rid, {}).get("name", f"Team {rid}"))
    move = lambda p: f'{p["pos"]}{p["drafted"]} → {p["pos"]}{p["finish"]}'
    def line(p):
        stayed = (" · kept all season" if draft["complete"] else " · still on roster") if p.get("kept", True) else             f' · gone after {p.get("team_weeks", 0)} wk{"s" if p.get("team_weeks", 0) != 1 else ""}'
        return (f'<li><span class="amt {"up" if p["value"] > 0 else "down" if p["value"] < 0 else ""}">'
                f'{p["value"]:+d}</span><div><b>{e(p["name"])}</b> <span class="pos">{e(p["pos"])}</span> · {tname(p["roster"])}'
                f'<div class="beat">Round {p["round"]}, pick {p["pick"]} · drafted {move(p)} · '
                f'{p.get("team_pts", p["pts"]):.1f} pts for them{stayed}</div></div></li>')
    steals = sorted((p for p in picks if p.get("kept", True) and p["value"] > 0), key=lambda p: (-p["value"], p["pick"]))[:8]
    busts = sorted((p for p in picks if p["round"] <= 5), key=lambda p: (p["value"], p["pick"]))[:8]

    rounds = []
    for r in sorted({p["round"] for p in picks}):
        rp = [p for p in picks if p["round"] == r]
        b, w = max(rp, key=draft_value), min(rp, key=draft_value)
        cell = lambda p: f'{e(p["name"])} <small>{tname(p["roster"])} · {move(p)}</small>'
        rounds.append(f'<tr><td>{r}</td><td class="tn">{cell(b)}</td><td class="tn">{cell(w)}</td></tr>')

    grades = []
    for rid in sorted(teams, key=lambda r: -draft_grade([p for p in picks if p["roster"] == r])):
        mine = [p for p in picks if p["roster"] == rid]
        if not mine:
            continue
        top = max(mine, key=draft_value)
        grades.append(f'<tr><td class="tn">{team_label(teams, rid)}</td><td>{draft_grade(mine):+.1f}</td>'
                      f'<td>{sum(p.get("team_pts", p["pts"]) for p in mine):,.0f}</td><td class="tn">{e(top["name"])} <small>{move(top)}</small></td></tr>')

    alltime = ""
    if len(all_drafts) > 1:
        by_owner = {}
        for d in all_drafts.values():
            for p in d["picks"]:
                if p["owner"] and not p["keeper"]:
                    by_owner.setdefault(p["owner"], []).append(p)
        rows = "".join(
            f'<tr><td class="tn">{team_label({"m": managers[o]}, "m") if o in managers else e(o)}</td>'
            f'<td>{len({s for s, d in all_drafts.items() if any(p["owner"] == o for p in d["picks"])})}</td>'
            f'<td>{draft_grade(ps):+.1f}</td></tr>'
            for o, ps in sorted(by_owner.items(), key=lambda kv: -draft_grade(kv[1])))
        alltime = f"""<section><h2 class="sec">Best Drafters, All Seasons</h2><div class="tbl"><table>
  <thead><tr><th class="tn">Manager</th><th>Drafts</th><th>Avg value per pick</th></tr></thead><tbody>{rows}</tbody></table></div></section>"""

    span = "the full regular season" if draft["complete"] else f"weeks 1–{draft['through_week']}"
    body = f"""<section><h2 class="sec">Draft Grades</h2><div class="tbl"><table>
  <thead><tr><th class="tn">Team</th><th>Avg value per pick</th><th>Points from picks</th><th class="tn">Best pick</th></tr></thead>
  <tbody>{"".join(grades)}</tbody></table></div></section>
<section><div class="records">
  <div><h3>Biggest steals</h3><ul class="bids">{"".join(line(p) for p in steals)}</ul></div>
  <div><h3>Biggest busts (rounds 1–5)</h3><ul class="bids">{"".join(line(p) for p in busts)}</ul></div>
</div></section>
<section><h2 class="sec">Round by Round</h2><div class="tbl"><table>
  <thead><tr><th>Round</th><th class="tn">Best pick</th><th class="tn">Worst pick</th></tr></thead><tbody>{"".join(rounds)}</tbody></table></div></section>
{alltime}"""
    footer = (f"Value compares where a player was drafted at his position (RB5 = the fifth running back taken) with "
              f"where he finished at that position across the NFL in league scoring, over {span}. +10 means he finished "
              f"10 spots better than drafted. Steals only count players the drafting team kept; a player they dropped or traded "
              f"gets no credit for what he did afterward. Points from picks are what drafted players scored while on that "
              f"team. Grades average each pick's value, capped at ±{DRAFT_CAP}.")
    nav = "".join(f'<a href="{base}{s}/draft.html"{" class=cur" if s == season else ""}>{s}</a>'
                  for s in sorted(site["draft_seasons"], reverse=True))
    return page(data, base, sidebar(season, base, site, "draft"), nav, f"{e(data['league'])} · {e(season)}",
                "Draft Report Card", body, footer, f"{season} draft report card")


def pct(v, settled=True):
    if not settled and v >= 99:
        return ">99%"
    if not settled and v < 1:
        return "<1%"
    return "<1%" if 0 < v < 1 else ">99%" if 99 < v < 100 else f"{v:.0f}%"


def odds_table(data):
    """Postseason chances by round, in the style of playoffstatus.com."""
    teams, e = data["teams"], html.escape
    odds = {r: t["odds"] for r, t in teams.items() if t.get("odds")}
    if not odds:
        return ""
    cols = [("champ", "Champion"), ("final", "Final"), ("semis", "Semifinals"), ("bye", "Bye"), ("playoffs", "Playoffs")]
    cols = [c for c in cols if c[0] != "bye" or any(o["bye"] for o in odds.values())]
    rows = []
    for rid in sorted(odds, key=lambda r: tuple(-odds[r][k] for k, _ in cols)):
        o, t = odds[rid], teams[rid]
        settled = o.get("settled", True)
        mark = "" if not settled else (' <span class="flag up">clinched</span>' if o["playoffs"] == 100 else
                                       ' <span class="flag down">eliminated</span>' if o["playoffs"] == 0 else "")
        cells = "".join(f'<td class="{"zero" if not o[k] else ""}">{pct(o[k], settled)}</td>' for k, _ in cols)
        rows.append(f'<tr><td class="tn">{team_label(teams, rid)}{mark}</td><td>{rec(t)}</td>{cells}</tr>')
    head = "".join(f"<th>{label}</th>" for _, label in cols)
    return f"""<section><h2 class="sec">Playoff Odds</h2><div class="tbl"><table class="odds">
  <thead><tr><th class="tn">Team</th><th>Record</th>{head}</tr></thead><tbody>{"".join(rows)}</tbody></table></div>
</section>"""

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


def ordinal(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def season_breakdown(hist, extras, latest, uid):
    """One row per season a manager played: record, finish, playoffs, FAAB, trades and draft."""
    e, rows = html.escape, []
    for s in sorted({g["season"] for g in hist["games"] if uid in (g["a"], g["b"])}, reverse=True):
        data, x = latest.get(s), extras.get(s) or {}
        mine = [(g["a_pts"], g["b_pts"]) if g["a"] == uid else (g["b_pts"], g["a_pts"])
                for g in hist["games"] if g["season"] == s and uid in (g["a"], g["b"])]
        r = {"w": sum(m > o for m, o in mine), "l": sum(m < o for m, o in mine), "t": sum(m == o for m, o in mine)}
        rids = {rid for rid, t in (data or {}).get("teams", {}).items() if t.get("owner") == uid}
        done = s in {f["season"] for f in hist["finishes"]}
        finish = "–"
        if data and rids:
            place = next(i for i, rid in enumerate(standings_order(data), 1) if rid in rids)
            finish = f"{ordinal(place)} of {len(data['teams'])}"
        if s in hist.get("playoffs", {}):
            result = hist["playoffs"][s].get(uid, "Missed")
        else:  # older history.json without brackets
            result = next(("🏆 Champion" if f["champion"] == uid else "Runner-up" for f in hist["finishes"]
                           if f["season"] == s and uid in (f["champion"], f["runner_up"])), "–" if done else "In progress")
        median, overall = "–", dict(r)
        if data and data.get("median_game"):
            # Each week the top half of scores gets a median win.
            mr = {"w": 0, "l": 0, "t": 0}
            for w in {g["week"] for g in hist["games"] if g["season"] == s}:
                week = [g for g in hist["games"] if g["season"] == s and g["week"] == w]
                scores = sorted((p for g in week for p in (g["a_pts"], g["b_pts"])), reverse=True)
                mine_w = next((g["a_pts"] if g["a"] == uid else g["b_pts"] for g in week if uid in (g["a"], g["b"])), None)
                if mine_w is not None:
                    cut = scores[len(scores) // 2 - 1], scores[len(scores) // 2]
                    mr["w" if mine_w > cut[1] and mine_w >= cut[0] else "t" if cut[0] == cut[1] == mine_w else "l"] += 1
            median = wlt(mr)
            overall = {k: r[k] + mr[k] for k in r}
        claims = [c for c in ((x.get("faab") or {}).get("claims") or []) if c["roster"] in rids]
        faab = f"${sum(c['bid'] for c in claims)} · {len(claims)}" if x.get("faab") else "–"
        best = max(claims, key=lambda c: c.get("started", 0), default=None)
        pickup = (f'{e(best["name"])} <small>${best["bid"]} · {best["started"]:.0f} pts started</small>'
                  if best and best.get("started") else "–")
        trades = (str(sum(any(side["roster"] in rids for side in t["sides"]) for t in x["trades"]))
                  if x.get("trades") is not None else "–")
        picks = [p for p in ((x.get("draft") or {}).get("picks") or []) if p["owner"] == uid and not p["keeper"]]
        draft = f"{draft_grade(picks):+.1f}" if picks else "–"
        rows.append(f'<tr><td class="tn">{s}</td><td class="tn">{e(hist["names"].get(s, {}).get(uid, ""))}</td>'
                    f'<td>{wlt(r)}</td><td>{median}</td><td><b>{wlt(overall)}</b></td><td>{sum(m for m, _ in mine) / max(len(mine), 1):.1f}</td>'
                    f'<td>{max((m for m, _ in mine), default=0):.1f}</td><td>{finish}</td><td class="tn">{result}</td>'
                    f'<td>{faab}</td><td class="tn">{pickup}</td><td>{trades}</td><td>{draft}</td></tr>')
    return ('<table class="inner"><thead><tr><th class="tn">Season</th><th class="tn">Team</th><th>Record</th>'
            '<th>vs median</th><th>Overall</th><th>PF / game</th><th>High score</th><th>Finish</th><th class="tn">Playoffs</th>'
            '<th>FAAB spent · claims</th><th class="tn">Best pickup</th><th>Trades</th><th>Draft value</th>'
            '</tr></thead><tbody>' + "".join(rows) + '</tbody></table>')


def render_records(hist, data, site, extras=None, latest=None):
    e, mgr, names = html.escape, hist["managers"], hist["names"]
    totals, _, order, pct = history_totals(hist)
    who = lambda uid: (f'<span class="who">{avatar(mgr[uid])}<span>{e(mgr[uid]["name"])}'
                       f'<small>{e(mgr[uid]["manager"])}</small></span></span>')
    team = lambda season, uid: e(names[season].get(uid) or mgr[uid]["name"])

    standings = "".join(
        f'<tr class="mgr"><td class="tn"><button class="expand" aria-expanded="false" aria-controls="seasons-{u}">'
        f'{who(u)}<span class="chev" aria-hidden="true">▸</span></button></td><td>{len(totals[u]["seasons"])}</td>'
        f'<td>{wlt(totals[u])}</td><td>{pct(totals[u]):.3f}</td><td>{totals[u]["pf"] / max(totals[u]["games"], 1):.1f}</td>'
        f'<td>{"🏆" * totals[u]["titles"] or "–"}</td></tr>'
        f'<tr class="more" id="seasons-{u}" hidden><td colspan="6">{season_breakdown(hist, extras or {}, latest or {}, u)}</td></tr>'
        for u in order if totals[u]["games"])
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
<section><h2 class="sec">All-Time Standings</h2><div class="tbl"><table class="alltime">
  <thead><tr><th class="tn">Manager</th><th>Seasons</th><th>Record</th><th>Win %</th><th>PF / game</th><th>Titles</th></tr></thead>
  <tbody>{standings}</tbody></table></div><p class="hint">Tap a manager for their season-by-season breakdown.</p></section>
<script>
document.querySelectorAll('.alltime .expand').forEach(function (b) {{
  b.addEventListener('click', function () {{
    var open = b.getAttribute('aria-expanded') !== 'true';
    b.setAttribute('aria-expanded', open);
    document.getElementById(b.getAttribute('aria-controls')).hidden = !open;
  }});
}});
</script>
<section><h2 class="sec">Records</h2><div class="records">{lists}</div></section>"""
    footer = ("Regular-season head-to-head games only; median games and playoffs aren't counted in records or streaks. "
              "In the season breakdown, Finish is the official regular-season place (median wins included), "
              "vs median is the record against each week's league median, Overall is the two combined, Best pickup is the waiver claim with the "
              "most points in their starting lineup, "
              "Draft value is average value per pick from the Draft Report Card, and Trades counts trades made that season. "
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


# Bid log search: filters claims by player name and hides weeks with no matches.
SEARCH = """<input class="search" type="search" placeholder="Search players" aria-label="Search players">
<script>
(function () {
  var log = document.getElementById('log'), box = log.querySelector('.search'), none = log.querySelector('.nomatch');
  box.addEventListener('input', function () {
    var q = box.value.trim().toLowerCase(), any = false;
    log.querySelectorAll('.wk').forEach(function (wk) {
      var shown = 0;
      wk.querySelectorAll('li').forEach(function (li) {
        var hit = !q || (li.dataset.player || '').indexOf(q) !== -1;
        li.hidden = !hit;
        shown += hit;
      });
      wk.hidden = !shown;
      any = any || shown > 0;
    });
    none.hidden = any;
  });
})();
</script>"""


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
        gained = sum(c.get("started", 0) for c in mine)
        per = f"{gained / spent[rid]:.1f}" if spent[rid] else "–"
        table.append(f"""<tr><td class="tn"><span class="who">{avatar(teams[rid])}<span>{e(name(rid))}</span></span></td>
      <td>{money(spent[rid])}</td>{f"<td>{tr_txt}</td>" if trades else ""}
      <td><div class="left"><span>{money(left)}</span><span class="bar"><span style="width:{min(100, 100 * left / budget):.0f}%"></span></span></div></td>
      <td>{len(mine)}</td><td>{gained:.1f}</td><td>{per}</td><td class="tn buy">{big_txt}</td></tr>""")

    log = []
    for w in sorted({c["week"] for c in claims} | {x["week"] for x in trades}, reverse=True):
        items = [f"""<li class="trade"><span class="amt">{money(x['amount'])}</span>
        <div><b>{e(name(x['from']))}</b> sent {money(x['amount'])} FAAB to <b>{e(name(x['to']))}</b> in a trade</div></li>"""
                 for x in trades if x["week"] == w]
        for c in (c for c in claims if c["week"] == w):
            beat = ""
            outbid = [o for o in c["outbid"] if o["roster"] != c["roster"]]  # not the winner's own backup claims
            if outbid:
                others = ", ".join(f'{e(name(o["roster"]))}’s {money(o["bid"])}' for o in outbid)
                margin = c["bid"] - outbid[0]["bid"]
                beat = (f'<div class="beat">outbidding {others} · '
                        + (f'by {money(margin)}' if margin > 0 else 'won the tiebreaker') + '</div>')
            items.append(f"""<li data-player="{e(c['name'].lower())}"{' class="zero"' if not c['bid'] else ''}><span class="amt">{money(c['bid'])}</span>
        <div><b>{e(name(c['roster']))}</b> won <b>{e(c['name'])}</b> <span class="pos">{e(c['pos'])}</span>
        <span class="gain">{f"→ {c['pts']:.1f} pts ({c['started']:.1f} started)" if c.get('weeks') else "→ hasn't played for them yet"}</span>{beat}</div></li>""")
        log.append(f'<div class="wk"><h3>{f"Week {w}" if w else "Preseason"}</h3><ul class="bids">{"".join(items)}</ul></div>')

    body = f"""<section><h2 class="sec">Budget</h2><div class="tbl"><table>
  <thead><tr><th class="tn">Team</th><th>Spent</th>{"<th>Traded</th>" if trades else ""}<th>Remaining</th><th>Claims won</th><th>Started pts</th><th>Pts per $</th><th class="tn">Biggest buy</th></tr></thead>
  <tbody>{"".join(table)}</tbody></table></div></section>
<!--ROI-->
<section id="log"><div class="sechead"><h2 class="sec">Bid log</h2>{SEARCH if log else ""}</div>
{"".join(log) or "<p>No waiver claims yet.</p>"}<p class="nomatch" hidden>No players match.</p></section>"""
    def roi_line(c, val, unit):
        weeks = f'{c.get("weeks", 0)} wk{"s" if c.get("weeks", 0) != 1 else ""}' + (" so far" if c.get("active") else "")
        return (f'<li><span class="amt">{val}<small>{unit}</small></span><div><b>{e(c["name"])}</b> '
                f'<span class="pos">{e(c["pos"])}</span> · {e(name(c["roster"]))}<div class="beat">{money(c["bid"])} in '
                f'Wk {c["week"]} → {c.get("started", 0):.1f} pts started · {weeks}</div></div></li>')
    per_dollar = lambda c: c.get("started", 0) / c["bid"]  # ROI only counts points from the starting lineup
    values = sorted((c for c in claims if c["bid"] >= 2), key=per_dollar, reverse=True)[:5]
    # Judge a bust only once he's had a couple of weeks, or played and is already gone.
    busts = sorted((c for c in claims if c["bid"] >= 10 and (c.get("weeks", 0) >= 2
                                                             or c.get("weeks", 0) and not c.get("active"))),
                   key=lambda c: (per_dollar(c), -c["bid"]))[:5]
    free = sorted((c for c in claims if c["bid"] <= 1), key=lambda c: -c.get("started", 0))[:5]
    lst = lambda items: "".join(items) or "<li><div>None yet</div></li>"
    roi = "" if not claims else f"""<section><h2 class="sec">Waiver ROI</h2><div class="records">
  <div><h3>Best value ($2+ bids)</h3><ul class="bids">{lst(roi_line(c, f"{per_dollar(c):.1f}", "pts/$") for c in values)}</ul></div>
  <div><h3>Biggest busts ($10+ bids)</h3><ul class="bids">{lst(roi_line(c, f"{per_dollar(c):.1f}", "pts/$") for c in busts)}</ul></div>
  <div><h3>Best $0–$1 pickups</h3><ul class="bids">{lst(roi_line(c, f"{c.get('started', 0):.0f}", "pts") for c in free)}</ul></div>
</div></section>"""
    body = body.replace("<!--ROI-->", roi)
    footer = (f"Pickup pts are everything a claimed player scored while on that team (started or not), from the claim "
              "until he left the roster. Started pts, Pts per $ and Waiver ROI count only points scored in the starting lineup. "
              f"Each team starts with {money(budget)} of FAAB; remaining includes FAAB traded between teams. Winning "
              "waiver claims from Sleeper, newest week first. “Outbidding” lists the other teams that bid on the same "
              "player that week.")
    seasons_nav = "".join(f'<a href="{base}{s}/faab.html"{" class=cur" if s == season else ""}>{s}</a>'
                          for s in sorted(site["faab_seasons"], reverse=True))
    return page(data, base, sidebar(season, base, site, "faab"), seasons_nav,
                f"{e(data['league'])} · {e(season)}", "FAAB Tracker", body, footer, f"{season} FAAB spending")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def build():
    seasons = sorted(d.name for d in WEEKS.iterdir() if d.is_dir() and weeks_written(d.name)) if WEEKS.exists() else []
    if not seasons:
        sys.exit("No weeks yet. Run: py ffr.py new <week>")
    extras = {s: {k: read_json(WEEKS / s / f"{k}.json") for k in ("faab", "trades", "draft")} for s in seasons}
    hist = read_json(HISTORY)
    site = {"latest_week": {s: weeks_written(s)[-1] for s in seasons},
            "history": hist is not None,
            "complete": {f["season"]: True for f in (hist or {}).get("finishes", [])},
            "faab_seasons": {s for s in seasons if extras[s]["faab"]},
            "trade_seasons": {s for s in seasons if extras[s]["trades"] is not None},
            "draft_seasons": {s for s in seasons if extras[s]["draft"]}}
    if IMAGES.exists():
        shutil.copytree(IMAGES, SITE / "images", dirs_exist_ok=True)
    latest = {}  # each season's latest week data, for team names and roster owners
    for s in seasons:
        weeks = weeks_written(s)
        (SITE / s).mkdir(parents=True, exist_ok=True)
        prev_rank, history = None, []
        for w in weeks:
            txt, js = week_paths(s, w)
            data = json.loads(js.read_text(encoding="utf-8"))
            content = parse_week(txt, data)
            history.append((w, [r for r, _ in content["rankings"]]))
            out = render_week(data, content, prev_rank, weeks, site, "../", history)
            (SITE / s / f"week-{w:02d}.html").write_text(out, encoding="utf-8")
            if w == weeks[-1]:
                (SITE / s / "index.html").write_text(out, encoding="utf-8")
                if s == seasons[-1]:
                    root = render_week(data, content, prev_rank, weeks, site, "", history)
                    (SITE / "index.html").write_text(root, encoding="utf-8")
            prev_rank = history[-1][1]
        latest[s] = data
        x = extras[s]
        if x["faab"]:
            (SITE / s / "faab.html").write_text(render_faab(x["faab"], data, site), encoding="utf-8")
        if x["trades"] is not None:
            (SITE / s / "trades.html").write_text(render_trades(x["trades"], data, site), encoding="utf-8")
        if x["draft"]:
            drafts = {k: v["draft"] for k, v in extras.items() if v["draft"]}
            (SITE / s / "draft.html").write_text(
                render_draft(x["draft"], data, site, drafts, (hist or {}).get("managers", {})), encoding="utf-8")
        print(f"Built {s}: {len(weeks)} week(s) + " + ", ".join(k for k, v in x.items() if v is not None))
    if hist:
        (SITE / "history").mkdir(exist_ok=True)
        (SITE / "history" / "records.html").write_text(render_records(hist, data, site, extras, latest), encoding="utf-8")
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
:root{--bg:#f6f5f2;--card:#fff;--ink:#1b1d21;--muted:#6b6f78;--line:#e4e2dc;--chip:#f0eee9;--up:#1f8a4c;--down:#c8402f;--hl:#2a78d6;--trend:#c4c1b9;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#121417;--card:#1b1e23;--ink:#eceef1;--muted:#9aa0aa;--line:#2b2f36;--chip:#252930;--up:#4cc27e;--down:#ff7a66;--hl:#3987e5;--trend:#454a53;color-scheme:dark}}
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
td.buy{white-space:normal;min-width:180px}
.who{display:inline-flex;vertical-align:middle;align-items:center;gap:10px;font-weight:600}.who .av{width:28px;height:28px;font-size:.75rem}
.left{display:flex;align-items:center;gap:10px;justify-content:flex-end}
.bar{width:90px;height:8px;border-radius:99px;background:var(--chip);overflow:hidden}
.bar span{display:block;height:100%;background:var(--up);border-radius:99px}
h3{font-size:.95rem;margin:22px 0 8px;color:var(--muted)}
.sechead{display:flex;align-items:flex-end;justify-content:space-between;gap:8px 16px;flex-wrap:wrap}
.sechead .sec{margin-bottom:12px}
.search{margin-bottom:12px;width:min(260px,100%);padding:8px 12px;font:inherit;font-size:.9rem;color:var(--ink);background:var(--card);border:1px solid var(--line);border-radius:999px}
.search:focus{outline:2px solid var(--hl);outline-offset:1px}
.nomatch{color:var(--muted)}
.expand{all:unset;cursor:pointer;display:flex;align-items:center;gap:10px;border-radius:8px}
.expand:focus-visible{outline:2px solid var(--hl);outline-offset:2px}
.chev{color:var(--muted);font-size:.8rem;transition:transform .15s}
.expand[aria-expanded=true] .chev{transform:rotate(90deg)}
tr.more>td{padding:0 0 4px;background:var(--chip)}
table.inner{font-size:.85rem}
table.inner th,table.inner td{padding:8px 14px}
.hint{color:var(--muted);font-size:.8rem;margin:8px 2px 0}
[hidden]{display:none!important}
.bids{list-style:none;padding:0;margin:0;background:var(--card);border:1px solid var(--line);border-radius:14px}
.bids li{display:flex;gap:14px;align-items:baseline;padding:10px 16px;border-bottom:1px solid var(--line)}
.bids li:last-child{border-bottom:0}
.bids .amt small{display:block;font-size:.65rem;font-weight:600;color:var(--muted)}
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
.hint{color:var(--muted);font-size:.85rem;margin:8px 0}
.flag{font-size:.7rem;font-weight:700;text-transform:uppercase;letter-spacing:.04em;margin-left:6px}
.odds td.zero{color:var(--muted)}
.gain{color:var(--muted);font-size:.85rem;margin-left:4px}
.chart{position:relative;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:10px 8px}
.bump{display:block;width:100%;height:auto;overflow:visible}
.bump .ax{fill:var(--muted);font-size:12px}
.bump .gl{stroke:var(--line);stroke-width:1}
.bump .ln{fill:none;stroke:var(--trend);stroke-width:2;stroke-linejoin:round;stroke-linecap:round}
.bump .hit{fill:none;stroke:transparent;stroke-width:14;pointer-events:stroke}
.bump circle{fill:var(--trend);stroke:var(--card);stroke-width:2;opacity:0}
.bump .nm{fill:var(--muted);font-size:13px}
.bump .series{cursor:pointer;outline:none}
.bump.has-on .series:not(.on){opacity:.3}
.bump .series.on .ln{stroke:var(--hl);stroke-width:3}
.bump .series.on circle{fill:var(--hl);opacity:1}
.bump .series.on .nm{fill:var(--ink);font-weight:700}
.tip{position:absolute;transform:translate(-50%,-100%);background:var(--ink);color:var(--bg);font-size:.8rem;padding:4px 8px;border-radius:6px;pointer-events:none;white-space:nowrap}
.tview{margin-top:10px}.tview summary{cursor:pointer;color:var(--muted);font-size:.85rem}.tview .tbl{margin-top:8px}
.deal{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 16px;margin-bottom:14px}
.deal .when{color:var(--muted);font-size:.8rem;font-weight:600;margin-bottom:8px}
.deal .sides{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}
.deal .side{border:1px solid var(--line);border-radius:10px;padding:10px 12px}
.deal .side.lead{border-color:var(--up);box-shadow:inset 3px 0 0 var(--up)}
.deal .side .lbl{color:var(--muted);font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;margin:8px 0 4px}
.got{list-style:none;padding:0;margin:0;font-size:.9rem}
.got li{display:flex;gap:8px;align-items:baseline;padding:2px 0}
.got .pos{color:var(--muted);font-size:.7rem;font-weight:700;min-width:2.2em}
.got .pp{margin-left:auto;font-variant-numeric:tabular-nums}
.got .note{color:var(--muted)}
.deal .tot{margin-top:8px;font-weight:700;text-align:right;font-variant-numeric:tabular-nums}
.deal .verdict{margin-top:10px;font-size:.92rem}
.amt.up{color:var(--up)}.amt.down{color:var(--down)}
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
    elif args == ["refresh"]:
        season = max(load_seasons())
        if weeks_written(season):
            new_week(weeks_written(season)[-1], season)
        build()
    else:
        print(__doc__)
