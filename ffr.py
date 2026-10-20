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
    old_format = names and "exp" not in next(iter(names.values()))
    if stale or old_format or any(i not in names for i in ids):
        print("Downloading Sleeper player list...")
        names = {}
        for pid, p in get("/players/nfl").items():
            pos = p.get("position") or ""
            if pos == "DEF":
                name = f"{p.get('first_name', '')} {p.get('last_name', '')} D/ST".strip()
            else:
                name = p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip()
            # fpos: every lineup position the player is eligible for. The rest is for the player cards.
            names[pid] = {"name": name, "pos": pos, "fpos": p.get("fantasy_positions") or [pos],
                          "team": p.get("team"), "age": p.get("age"), "exp": p.get("years_exp"),
                          "no": p.get("number"), "college": p.get("college"), "inj": p.get("injury_status"),
                          "active": p.get("active")}
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
    players = load_players(set())
    for t in teams.values():
        # For team stats: opponents faced and left, and starter points by position (regular season).
        t.update({"opps": [], "left_opps": [], "pos_pts": {}})
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
                    if regular:
                        pos = players.get(pid, {}).get("pos") or "?"
                        teams[rid]["pos_pts"][pos] = teams[rid]["pos_pts"].get(pos, 0) + (p or 0)

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
                    t["opps"].append(str(opp))
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
            for a, b in remaining[-1]:
                teams[a]["left_opps"].append(str(b))
                teams[b]["left_opps"].append(str(a))
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
        t["scores"] = [round(x, 2) for x in scores[rid]]
        t["pos_pts"] = {k: round(v, 2) for k, v in t["pos_pts"].items()}
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


PLACES = {1: ("Champion", "Runner-up"), 3: ("3rd place", "4th place"), 5: ("5th place", "6th place")}


def fetch_history(seasons):
    """Every regular-season game, each season's standings, playoff brackets and results, keyed by manager
    (Sleeper user id)."""
    managers, games, finishes, names, detail, playoffs, playoff_games = {}, [], [], {}, {}, {}, []
    for season, league_id in sorted(seasons.items()):
        league = get(f"/league/{league_id}")
        settings = league.get("settings") or {}
        users = {u["user_id"]: u for u in get(f"/league/{league_id}/users")}
        rosters = get(f"/league/{league_id}/rosters")
        owner = {r["roster_id"]: r.get("owner_id") for r in rosters}
        names[season] = {}
        for uid in filter(None, owner.values()):
            u = users.get(uid, {})
            team = (u.get("metadata") or {}).get("team_name") or u.get("display_name", "?")
            names[season][uid] = team
            # Later seasons overwrite earlier ones, so each manager shows their latest team name.
            managers[uid] = {"name": team, "manager": u.get("display_name", ""), "avatar": avatar_url(u) if u else None}
        playoff_start = settings.get("playoff_week_start") or 15
        scored = settings.get("last_scored_leg") or 0
        for w in range(1, min(playoff_start - 1, scored) + 1):
            entries = get(f"/league/{league_id}/matchups/{w}")
            pts = {e["roster_id"]: float(e.get("custom_points") or e.get("points") or 0) for e in entries}
            pairs = {}
            for e in entries:
                if e.get("matchup_id"):
                    pairs.setdefault(e["matchup_id"], []).append(e["roster_id"])
            games += [{"season": season, "week": w, "a": owner[a], "a_pts": round(pts[a], 2),
                       "b": owner[b], "b_pts": round(pts[b], 2)}
                      for a, b in (p for p in pairs.values() if len(p) == 2) if owner[a] and owner[b]]

        # Standings as Sleeper keeps them (median games included); seed by wins, then points.
        stand = {}
        for r in rosters:
            st = r.get("settings") or {}
            if r.get("owner_id"):
                stand[r["owner_id"]] = {"w": st.get("wins", 0), "l": st.get("losses", 0), "t": st.get("ties", 0),
                                        "pf": round(st.get("fpts", 0) + st.get("fpts_decimal", 0) / 100, 2)}
        for seed, uid in enumerate(sorted(stand, key=lambda u: (-stand[u]["w"], -stand[u]["pf"])), 1):
            stand[uid]["seed"] = seed

        bracket = get(f"/league/{league_id}/winners_bracket") or []
        playoff_games += playoff_matchups(season, league_id, settings, bracket, owner, "playoffs")
        playoff_games += playoff_matchups(season, league_id, settings, get(f"/league/{league_id}/losers_bracket") or [],
                                          owner, "toilet")
        complete = league.get("status") == "complete"
        brackets = {}
        if complete:
            for kind in ("winners", "losers"):
                matches = []
                for m in get(f"/league/{league_id}/{kind}_bracket") or []:
                    week = playoff_start + m["r"] - 1  # one week per round
                    pts = {e["roster_id"]: float(e.get("custom_points") or e.get("points") or 0)
                           for e in get(f"/league/{league_id}/matchups/{week}") or []}
                    side = lambda rid: {"owner": owner.get(rid), "pts": round(pts.get(rid, 0), 2)} if rid else None
                    matches.append({"r": m["r"], "m": m["m"], "p": m.get("p"), "t1": side(m.get("t1")),
                                    "t2": side(m.get("t2")), "w": owner.get(m.get("w")), "week": week})
                brackets[kind] = sorted(matches, key=lambda m: (m["r"], m["m"]))
            in_playoffs = {s["owner"] for m in brackets["winners"] for s in (m["t1"], m["t2"]) if s}
            for uid in stand:
                stand[uid]["finish"] = "Playoffs" if uid in in_playoffs else "Missed playoffs"
            for m in brackets["winners"]:
                if m["p"] in PLACES and m["w"]:
                    loser = next(s["owner"] for s in (m["t1"], m["t2"]) if s and s["owner"] != m["w"])
                    stand[m["w"]]["finish"], stand[loser]["finish"] = PLACES[m["p"]]
                    if m["p"] == 1:
                        finishes.append({"season": season, "champion": m["w"], "runner_up": loser})
            playoffs[season] = playoff_results(bracket, owner)
        detail[season] = {"complete": complete, "standings": stand, "brackets": brackets,
                          "rosters": {str(rid): uid for rid, uid in owner.items() if uid}}
    return {"managers": managers, "names": names, "games": games, "finishes": finishes, "seasons": detail,
            "playoffs": playoffs, "playoff_games": playoff_games}


def bracket_stage(bracket, m, kind):
    """'Championship', 'Semifinal', 'Toilet bowl'... for one bracket match."""
    last = max(x["r"] for x in bracket)
    stage = {last: "Final", last - 1: "Semifinal", last - 2: "Quarterfinal"}.get(m["r"], f"Round {m['r']}")
    if kind == "toilet":
        return "Toilet bowl" if m.get("p") == 1 else f"Toilet bowl {stage.lower()}"
    return "Championship" if m.get("p") == 1 else stage


def playoff_matchups(season, league_id, settings, bracket, owner, kind):
    """Decided bracket games with both scores: in the winners bracket only games on the path to the title, in the
    losers bracket only games on the path to the toilet bowl. Placement games (3rd, 5th...) are left out.
    Each round is one week (Sleeper's default)."""
    out, start = [], settings.get("playoff_week_start") or 15
    for m in bracket:
        if m.get("p", 1) != 1 or not (m.get("w") and m.get("l")) or not (owner.get(m["w"]) and owner.get(m["l"])):
            continue
        week = start + m["r"] - 1
        pts = {e["roster_id"]: float(e.get("custom_points") or e.get("points") or 0)
               for e in get(f"/league/{league_id}/matchups/{week}") or []}
        # In the toilet bowl the lower score advances, so Sleeper's "winner" is the team that lost the game.
        w, l = (m["l"], m["w"]) if kind == "toilet" else (m["w"], m["l"])
        out.append({"season": season, "week": week, "bracket": kind, "stage": bracket_stage(bracket, m, kind),
                    "w": owner[w], "w_pts": round(pts.get(w, 0), 2), "l": owner[l], "l_pts": round(pts.get(l, 0), 2)})
    return out


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


def fetch_moves(league_id):
    """Every team's roster moves this season, and the players who changed hands the most."""
    teams, journeys = {}, {}
    for w in range(0, 19):
        for t in get(f"/league/{league_id}/transactions/{w}") or []:
            if t["status"] != "complete":
                continue
            kind = t.get("type")
            if kind == "trade":
                for rid in map(str, t["roster_ids"]):
                    teams.setdefault(rid, {}).setdefault("trades", 0)
                    teams[rid]["trades"] += 1
            for pid, rid in (t.get("adds") or {}).items():
                tally = teams.setdefault(str(rid), {})
                if kind in ("waiver", "free_agent"):
                    tally[kind] = tally.get(kind, 0) + 1
                journeys.setdefault(pid, []).append(str(rid))
            for pid, rid in (t.get("drops") or {}).items():
                if kind != "trade":
                    tally = teams.setdefault(str(rid), {})
                    tally["drops"] = tally.get("drops", 0) + 1
    players = load_players(set(journeys))
    traveled = sorted(journeys.items(), key=lambda kv: (-len(set(kv[1])), -len(kv[1])))[:10]
    return {"teams": teams,
            "players": [{"name": players.get(pid, {}).get("name", pid), "pos": players.get(pid, {}).get("pos", ""),
                         "teams": list(dict.fromkeys(rids)), "adds": len(rids)} for pid, rids in traveled]}


def fetch_rosters(league_id):
    """For the player cards: the league's scoring, and each player's time on each roster
    as {player_id: {roster_id: [weeks rostered, points, points as a starter]}}."""
    league = get(f"/league/{league_id}")
    rostered, _ = rostered_points(league_id)
    players = {}
    for (rid, pid), weeks in rostered.items():
        players.setdefault(pid, {})[rid] = [len(weeks), round(sum(p for p, _ in weeks.values()), 2),
                                            round(sum(s for _, s in weeks.values()), 2)]
    return {"scoring": league.get("scoring_settings") or {}, "players": players}


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
    complete = league.get("status") == "complete"
    # Mid-season, "kept" means on the roster today: a trade this week counts even before its games are scored.
    current = {str(r["roster_id"]): set(r.get("players") or []) for r in get(f"/league/{league_id}/rosters")}
    taken, out = {}, []
    for p in picks:
        pid, pos = p["player_id"], pos_of(p["player_id"])
        taken[pos] = taken.get(pos, 0) + 1
        # Someone who never scored finishes behind everyone who did.
        fin = finish.get(pid, len(by_pos.get(pos, [])) + 1)
        stay = tenure(rostered, last, str(p["roster_id"]), pid, 1)
        kept = stay["active"] if complete else pid in current.get(str(p["roster_id"]), ())
        out.append({"kept": kept, "team_pts": stay["pts"], "team_weeks": stay["weeks"],"round": p["round"], "pick": p["pick_no"], "roster": str(p["roster_id"]), "owner": p.get("picked_by"),
                    "name": players.get(pid, {}).get("name", pid), "pos": pos, "pts": round(totals.get(pid, 0), 2),
                    "drafted": taken[pos], "finish": fin, "value": taken[pos] - fin, "keeper": bool(p.get("is_keeper"))})
    return {"through_week": last, "complete": complete, "picks": out}


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
    if js.exists():
        # Keep the names this week was written with, so a team renamed on Sleeper still matches the rankings file.
        before = json.loads(js.read_text(encoding="utf-8"))["teams"]
        for rid, t in data["teams"].items():
            if rid in before:
                t["name"] = before[rid]["name"]
    js.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Saved stats to {js.relative_to(ROOT)}")
    faab = WEEKS / season / "faab.json"
    faab.write_text(json.dumps(fetch_faab(seasons[season]), indent=1, ensure_ascii=False), encoding="utf-8")
    HISTORY.write_text(json.dumps(fetch_history(seasons), indent=1, ensure_ascii=False), encoding="utf-8")
    for name, fetch in (("trades", fetch_trades), ("draft", fetch_draft), ("moves", fetch_moves),
                        ("rosters", fetch_rosters)):
        (WEEKS / season / f"{name}.json").write_text(json.dumps(fetch(seasons[season]), indent=1, ensure_ascii=False),
                                                     encoding="utf-8")
    # Past seasons don't change, so their roster history only needs pulling once.
    for s, league_id in seasons.items():
        path = WEEKS / s / "rosters.json"
        if s != season and (WEEKS / s).is_dir() and not path.exists():
            path.write_text(json.dumps(fetch_rosters(league_id), indent=1, ensure_ascii=False), encoding="utf-8")
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
        "// Videos play silently on a loop; write ![sound](clip.mp4) instead for a video with audio and a play button.",
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
            if m.group(1).lower() == "sound":
                # Browsers won't autoplay with audio, so show controls and let the viewer press play.
                return f'<video class="pic" src="{src}" controls playsinline preload="metadata"></video>'
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


# Player cards. build() fills this in; pl() notes every player a page mentions so page() can embed just their details.
CARDS = {"players": {}, "ids": {}, "hist": {}, "scoring": {}, "latest": "", "on_page": set()}


def setup_cards(seasons):
    """Load player details and each player's history on this league's rosters, for pl() and PLAYER_JS."""
    try:
        players = load_players(set())
    except OSError:  # offline: use whatever's cached
        players = read_json(PLAYERS) or {}
    ids, by_name = {}, {}
    for pid, p in players.items():
        key = (p["name"].lower(), p["pos"])
        # Names aren't unique; prefer the player who's on a team.
        if key not in ids or (p.get("team") and not players[ids[key]].get("team")):
            ids[key] = pid
    for (name, _), pid in list(ids.items()):
        by_name.setdefault(name, []).append(pid)
    ids.update({name: pids[0] for name, pids in by_name.items() if len(pids) == 1})
    hist, scoring = {}, {}
    for s in seasons:
        rosters = read_json(WEEKS / s / "rosters.json")
        if not rosters:
            continue
        scoring[s] = rosters["scoring"]
        teams = read_json(week_paths(s, weeks_written(s)[-1])[1])["teams"]
        for pid, by_roster in rosters["players"].items():
            for rid, (weeks, pts, started) in by_roster.items():
                hist.setdefault(pid, []).append([s, teams.get(rid, {}).get("name", f"Team {rid}"), weeks, pts, started])
    for rows in hist.values():
        rows.sort(key=lambda r: (r[0], r[2]), reverse=True)
    CARDS.update(players=players, ids=ids, hist=hist, scoring=scoring, latest=seasons[-1])


def pl(p):
    """A player's name as a button that opens his card. `p` has name and pos, plus his Sleeper id as "player" if known."""
    name, ids = p["name"], CARDS["ids"]
    pid = p.get("player") or ids.get((name.lower(), p.get("pos") or "")) or ids.get(name.lower())
    if pid not in CARDS["players"]:
        return html.escape(name)
    CARDS["on_page"].add(pid)
    return f'<button type="button" class="pl" data-pid="{html.escape(pid)}">{html.escape(name)}</button>'


def card_data():
    """The details PLAYER_JS needs for every player this page mentions."""
    if not CARDS["on_page"]:
        return ""
    players = {}
    for pid in sorted(CARDS["on_page"]):
        p = CARDS["players"][pid]
        players[pid] = {"n": p["name"], "p": p["pos"], "t": p.get("team"), "no": p.get("no"), "age": p.get("age"),
                        "exp": p.get("exp"), "col": p.get("college"), "inj": p.get("inj"), "rv": CARDS["hist"].get(pid, [])}
    CARDS["on_page"].clear()
    blob = json.dumps({"latest": CARDS["latest"], "scoring": CARDS["scoring"], "players": players},
                      separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")
    return f'<script type="application/json" id="pl-data">{blob}</script>'


def sidebar(season, base, site, current=None):
    """Power rankings by season, plus league and history links. `current` marks a non-rankings page."""
    lw = site["latest_week"]
    years = "".join(f'<a href="{base}{s}/week-{lw[s]:02d}.html"{" class=cur" if s == season and not current else ""}>'
                    f'{s}</a>' for s in sorted(lw, reverse=True))
    link = lambda href, label, key: f'<a href="{href}"{" class=cur" if current == key else ""}>{label}</a>'
    league = f'<a href="{html.escape(RULE_BOOK_URL)}" target="_blank" rel="noopener">Rule Book ↗</a>'
    for key, label, have in (("recap", "Season Recap", "recap_seasons"), ("stats", "Team Stats", "stats_seasons"),
                             ("faab", "FAAB Tracker", "faab_seasons"), ("trades", "Trade Grades", "trade_seasons"),
                             ("draft", "Draft Report Card", "draft_seasons"), ("moves", "Transactions", "moves_seasons")):
        if season in site.get(have, ()):
            league += link(f"{base}{season}/{key}.html", label, key)
    groups = [("Power Rankings", years), ("League", league)]
    if site["history"]:
        groups.append(("League History", link(f"{base}history/records.html", "Record Book", "records")
                       + link(f"{base}history/h2h.html", "Head-to-Head", "h2h")
                       + link(f"{base}history/managers.html", "Managers", "managers")))
    return "".join(f'<div class="side-group"><div class="seasons-lbl">{label}</div>'
                   f'<div class="seasons-list">{links}</div></div>' for label, links in groups)


def week_nav(season, weeks, base, current=None):
    return "".join(f'<a href="{base}{season}/week-{w:02d}.html"{" class=cur" if w == current else ""}>Wk {w}</a>'
                   for w in weeks)


def page(data, base, side, nav, kicker, heading, body, footer, description):
    e = html.escape
    logo = f'<img class="logo" src="{e(data["league_avatar"])}" alt="">' if data.get("league_avatar") else ""
    return PAGE.format(site_name=e(SITE_NAME), description=e(description), side=side, logo=logo, kicker=kicker,
                       heading=heading, nav=nav, body=body, footer=footer, css=CSS,
                       theme_head=THEME_HEAD, scripts=card_data() + SORT_JS + THEME_JS + MOBILE_JS + PLAYER_JS)


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
        chips = lambda picks: "".join(f'<li><span class="pos">{e(p.get("rank") or p["pos"])}</span>{pl(p)}'
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
            items = "".join(f'<li><span class="pos">{e(p["pos"])}</span>{pl(p)}<span class="pp">{p["pts"]:.1f}</span></li>'
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
                f'{p["value"]:+d}</span><div><b>{pl(p)}</b> <span class="pos">{e(p["pos"])}</span> · {tname(p["roster"])}'
                f'<div class="beat">Round {p["round"]}, pick {p["pick"]} · drafted {move(p)} · '
                f'{p.get("team_pts", p["pts"]):.1f} pts for them{stayed}</div></div></li>')
    steals = sorted((p for p in picks if p.get("kept", True) and p["value"] > 0), key=lambda p: (-p["value"], p["pick"]))[:8]
    busts = sorted((p for p in picks if p["round"] <= 5), key=lambda p: (p["value"], p["pick"]))[:8]

    rounds = []
    for r in sorted({p["round"] for p in picks}):
        rp = [p for p in picks if p["round"] == r]
        b, w = max(rp, key=draft_value), min(rp, key=draft_value)
        cell = lambda p: f'{pl(p)} <small>{tname(p["roster"])} · {move(p)}</small>'
        rounds.append(f'<tr><td>{r}</td><td class="tn">{cell(b)}</td><td class="tn">{cell(w)}</td></tr>')

    grades = []
    for rid in sorted(teams, key=lambda r: -draft_grade([p for p in picks if p["roster"] == r])):
        mine = [p for p in picks if p["roster"] == rid]
        if not mine:
            continue
        top = max(mine, key=draft_value)
        grades.append(f'<tr><td class="tn">{team_label(teams, rid)}</td><td>{draft_grade(mine):+.1f}</td>'
                      f'<td>{sum(p.get("team_pts", p["pts"]) for p in mine):,.0f}</td><td class="tn">{pl(top)} <small>{move(top)}</small></td></tr>')

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

def season_nav(site, key, season, base="../"):
    """Season buttons for a per-season page, linking to the same page in other seasons."""
    return "".join(f'<a href="{base}{s}/{key}.html"{" class=cur" if s == season else ""}>{s}</a>'
                   for s in sorted(site[f"{key}_seasons"], reverse=True))


def ppg(t):
    return statistics.mean(t["scores"]) if t.get("scores") else 0


def render_stats(data, site):
    """Strength of schedule, positional strength, and consistency, as of the season's latest week."""
    teams, season, e, base = data["teams"], str(data["season"]), html.escape, "../"
    avg_of = lambda opps: statistics.mean(ppg(teams[o]) for o in opps) if opps else None
    past = {r: avg_of(t.get("opps")) for r, t in teams.items()}
    left = {r: avg_of(t.get("left_opps")) for r, t in teams.items()}
    rank = lambda vals: {r: i for i, r in enumerate(sorted((r for r in vals if vals[r] is not None),
                                                           key=lambda r: -vals[r]), 1)}
    past_rank, left_rank = rank(past), rank(left)
    any_left = any(v is not None for v in left.values())
    # data-v holds each cell's sort value; blank sorts last.
    cell = lambda v, rk: (f'<td data-v="{v:.3f}">{v:.1f} <small>#{rk}</small></td>' if v is not None
                          else '<td data-v="">–</td>')
    win_pct = lambda t: (t["wins"] + t["ties"] / 2) / max(t["wins"] + t["losses"] + t["ties"], 1)
    sos = "".join(
        f'<tr><td class="tn" data-v="{e(teams[r]["name"].lower())}">{team_label(teams, r)}</td>'
        f'<td data-v="{win_pct(teams[r]):.4f}">{rec(teams[r])}</td><td data-v="{ppg(teams[r]):.3f}">{ppg(teams[r]):.1f}</td>'
        f'{cell(past[r], past_rank.get(r))}{cell(left[r], left_rank.get(r)) if any_left else ""}</tr>'
        for r in sorted(teams, key=lambda r: past_rank.get(r, 99)))

    # Positional strength: points per game from each position's starters, against the league average.
    positions = [p for p in ("QB", "RB", "WR", "TE", "K", "DEF") if any(p in t.get("pos_pts", {}) for t in teams.values())]
    per_game = {r: {p: t.get("pos_pts", {}).get(p, 0) / max(len(t.get("scores") or []), 1) for p in positions}
                for r, t in teams.items()}
    league_avg = {p: statistics.mean(per_game[r][p] for r in teams) for p in positions}
    spread = {p: statistics.pstdev(per_game[r][p] for r in teams) or 1 for p in positions}

    def heat(r, p):
        z = (per_game[r][p] - league_avg[p]) / spread[p]
        step = 0 if abs(z) < 0.5 else 1 if abs(z) < 1 else 2 if abs(z) < 1.5 else 3
        cls = f' class="{"hi" if z > 0 else "lo"}{step}"' if step else ""
        return f'<td{cls} data-v="{per_game[r][p]:.3f}">{per_game[r][p]:.1f}</td>'
    heat_rows = "".join(
        f'<tr><td class="tn" data-v="{e(teams[r]["name"].lower())}">{team_label(teams, r)}</td>'
        f'{"".join(heat(r, p) for p in positions)}</tr>'
        for r in sorted(teams, key=lambda r: -ppg(teams[r])))
    heat_avg = "".join(f"<td>{league_avg[p]:.1f}</td>" for p in positions)

    # Consistency: how much weekly scores swing.
    swing = {r: statistics.pstdev(t["scores"]) if len(t.get("scores") or []) > 1 else 0 for r, t in teams.items()}
    order = sorted(teams, key=lambda r: swing[r])
    third = max(len(order) // 3, 1)
    label = lambda r: "Steady" if r in order[:third] else "Boom or bust" if r in order[-third:] else "Balanced"
    styles = ["Steady", "Balanced", "Boom or bust"]  # sorts in this order
    num = lambda v: f'<td data-v="{v:.3f}">{v:.1f}</td>'
    cons = "".join(
        f'<tr><td class="tn" data-v="{e(teams[r]["name"].lower())}">{team_label(teams, r)}</td>'
        f'{num(ppg(teams[r]))}{num(swing[r])}{num(min(teams[r]["scores"]))}{num(max(teams[r]["scores"]))}'
        f'<td class="tn" data-v="{styles.index(label(r))}">{label(r)}</td></tr>'
        for r in order if teams[r].get("scores"))

    body = f"""<section><h2 class="sec">Strength of Schedule</h2><div class="tbl"><table class="sortable">
  <thead><tr><th class="tn" data-sort="text">Team</th><th data-sort="num">Record</th><th data-sort="num">Pts / game</th>
  <th data-sort="num" aria-sort="descending">Opponents so far</th>{'<th data-sort="num">Opponents left</th>' if any_left else ""}</tr></thead>
  <tbody>{sos}</tbody></table></div>
<p class="hint">Average points per game of the opponents each team has played, and has left to play. #1 is the toughest.
Tap a column to sort.</p></section>
<section><h2 class="sec">Positional Strength</h2>
<div class="legend"><span class="sw lo3"></span><span class="sw lo2"></span><span class="sw lo1"></span><span class="sw"></span>
<span class="sw hi1"></span><span class="sw hi2"></span><span class="sw hi3"></span><span>Below league average → above</span></div>
<div class="tbl"><table class="heat sortable">
  <thead><tr><th class="tn" data-sort="text">Team</th>{"".join(f'<th data-sort="num">{p}</th>' for p in positions)}</tr></thead>
  <tbody>{heat_rows}</tbody>
  <tfoot><tr class="avg"><td class="tn">League average</td>{heat_avg}</tr></tfoot></table></div>
<p class="hint">Points per game from each position's starters. Flex starters count at their own position. Tap a column to sort.</p></section>
<section><h2 class="sec">Consistency</h2><div class="tbl"><table class="sortable">
  <thead><tr><th class="tn" data-sort="text">Team</th><th data-sort="num">Avg</th>
  <th data-sort="num" aria-sort="ascending">Swing (std dev)</th><th data-sort="num">Floor</th><th data-sort="num">Ceiling</th>
  <th class="tn" data-sort="num">Style</th></tr></thead>
  <tbody>{cons}</tbody></table></div>
<p class="hint">Swing is how far a team's weekly score typically lands from its average. Floor and ceiling are its lowest and
highest weeks. Tap a column to sort.</p></section>"""
    footer = f"Regular season through week {data['week']}. Stats from Sleeper."
    return page(data, base, sidebar(season, base, site, "stats"), season_nav(site, "stats", season),
                f"{e(data['league'])} · {e(season)} · Week {data['week']}", "Team Stats", body, footer,
                f"{season} team stats")


def render_moves(moves, data, site, all_moves, hist):
    teams, season, e, base = data["teams"], str(data["season"]), html.escape, "../"
    cols = (("waiver", "Waiver claims"), ("free_agent", "Free-agent adds"), ("drops", "Drops"), ("trades", "Trades"))
    total = lambda tally: sum(tally.get(k, 0) for k, _ in cols if k != "drops")
    rows = "".join(
        f'<tr><td class="tn">{team_label(teams, r)}</td>{"".join(f"<td>{tally.get(k, 0)}</td>" for k, _ in cols)}'
        f'<td><b>{total(tally)}</b></td></tr>'
        for r, tally in sorted(moves["teams"].items(), key=lambda kv: -total(kv[1])) if r in teams)
    traveled = "".join(
        f'<li><span class="amt">{len(p["teams"])}<small>teams</small></span><div><b>{pl(p)}</b> '
        f'<span class="pos">{e(p["pos"])}</span><div class="beat">'
        f'{" → ".join(e(teams.get(r, {}).get("name", "?")) for r in p["teams"])} · added {p["adds"]} times</div></div></li>'
        for p in moves["players"][:8])

    alltime = ""
    if hist and len(all_moves) > 1:
        by_owner = {}
        for s, mv in all_moves.items():
            owners = hist["seasons"].get(s, {}).get("rosters", {})
            for r, tally in mv["teams"].items():
                if owners.get(r):
                    acc = by_owner.setdefault(owners[r], {})
                    for k, _ in cols:
                        acc[k] = acc.get(k, 0) + tally.get(k, 0)
        mgr = hist["managers"]
        alltime = f"""<section><h2 class="sec">All-Time Leaders</h2><div class="tbl"><table>
  <thead><tr><th class="tn">Manager</th>{"".join(f"<th>{label}</th>" for _, label in cols)}<th>Total moves</th></tr></thead>
  <tbody>{"".join(f'<tr><td class="tn">{manager_link(mgr, o, base)}</td>{"".join(f"<td>{t.get(k, 0)}</td>" for k, _ in cols)}<td><b>{total(t)}</b></td></tr>'
                   for o, t in sorted(by_owner.items(), key=lambda kv: -total(kv[1])) if o in mgr)}</tbody></table></div></section>"""

    body = f"""<section><h2 class="sec">Most Active Managers</h2><div class="tbl"><table>
  <thead><tr><th class="tn">Team</th>{"".join(f"<th>{label}</th>" for _, label in cols)}<th>Total moves</th></tr></thead>
  <tbody>{rows}</tbody></table></div></section>
<section><h2 class="sec">Most-Traveled Players</h2><div class="records"><div><ul class="bids">{traveled or "<li><div>No moves yet</div></li>"}</ul></div></div></section>
{alltime}"""
    footer = "Total moves counts waiver claims, free-agent adds and trades. Drops are shown separately."
    return page(data, base, sidebar(season, base, site, "moves"), season_nav(site, "moves", season),
                f"{e(data['league'])} · {e(season)}", "Transactions", body, footer, f"{season} transactions")


def toilet_bowl(matches):
    """Sleeper's consolation bracket can advance the loser of each game instead of the winner."""
    played = [m for m in matches if m["w"] and m["t1"] and m["t2"] and m["t1"]["pts"] != m["t2"]["pts"]]
    return bool(played) and all(m["w"] == min((m["t1"], m["t2"]), key=lambda s: s["pts"])["owner"] for m in played)


def render_bracket(matches, hist, season, base, consolation=False):
    mgr, names, e = hist["managers"], hist["names"][season], html.escape
    titles = ({1: "Final"} if consolation else
              {1: "Championship", 3: "3rd place", 5: "5th place", 7: "7th place", 9: "9th place"})
    rounds = sorted({m["r"] for m in matches})
    cols = []
    for r in rounds:
        boxes = []
        for m in (m for m in matches if m["r"] == r):
            top = max((x for x in (m["t1"], m["t2"]) if x), key=lambda x: x["pts"], default=None)
            sides = "".join(
                f'<div class="slot{" won" if s and top and s is top and m["w"] else ""}">'
                f'<span>{e(names.get(s["owner"]) or mgr.get(s["owner"], {}).get("name", "?"))}</span>'
                f'<b>{s["pts"]:.2f}</b></div>' if s else '<div class="slot tbd"><span>TBD</span></div>'
                for s in (m["t1"], m["t2"]))
            boxes.append(f'<div class="match">{f"<div class=mlbl>{titles[m["p"]]}</div>" if m.get("p") in titles else ""}{sides}</div>')
        week = next(m["week"] for m in matches if m["r"] == r)
        cols.append(f'<div class="round"><div class="rlbl">Round {r} · Wk {week}</div>{"".join(boxes)}</div>')
    return f'<div class="bracket">{"".join(cols)}</div>'


def render_recap(season, data, site, hist, extras):
    """End-of-season summary: podium, final standings, brackets, and the year's standout moves."""
    e, base, mgr, names = html.escape, "../", hist["managers"], hist["names"][season]
    detail = hist["seasons"][season]
    stand = detail["standings"]
    team = lambda uid: e(names.get(uid) or mgr.get(uid, {}).get("name", "?"))
    owner_of = detail["rosters"]
    tname = lambda rid: team(owner_of.get(rid))
    place = lambda label: next((u for u, s in stand.items() if s.get("finish") == label), None)
    podium = "".join(
        f'<div class="award podium"><div class="lbl">{lbl}</div><div class="who">'
        f'{avatar(mgr[u]) if u in mgr else ""} {team(u)}</div><div class="val">{e(mgr.get(u, {}).get("manager", ""))}</div></div>'
        for lbl, u in (("🏆 Champion", place("Champion")), ("Runner-up", place("Runner-up")), ("3rd place", place("3rd place"))) if u)
    order = sorted(stand, key=lambda u: stand[u]["seed"])
    table = "".join(
        f'<tr><td>{stand[u]["seed"]}</td><td class="tn">{manager_link(mgr, u, base, names.get(u))}</td>'
        f'<td>{stand[u]["w"]}-{stand[u]["l"]}{"-" + str(stand[u]["t"]) if stand[u]["t"] else ""}</td>'
        f'<td>{stand[u]["pf"]:.2f}</td><td class="tn">{e(stand[u].get("finish", ""))}</td></tr>' for u in order)

    games = [g for g in hist["games"] if g["season"] == season]
    items = []
    if order:
        items.append(("Regular-season champ", team(order[0]), f'{stand[order[0]]["w"]}-{stand[order[0]]["l"]}, #1 seed'))
        most = max(stand, key=lambda u: stand[u]["pf"])
        items.append(("Most points", team(most), f'{stand[most]["pf"]:.2f} pts'))
    if games:
        hi = max(((g["a_pts"], g["a"], g) for g in games), key=lambda x: x[0])
        hi = max([hi] + [(g["b_pts"], g["b"], g) for g in games], key=lambda x: x[0])
        items.append(("Best single week", team(hi[1]), f'{hi[0]:.2f} pts in Wk {hi[2]["week"]}'))
    faab = extras.get("faab")
    if faab and faab["claims"]:
        c = max(faab["claims"], key=lambda c: c.get("pts", 0))
        items.append(("Best pickup", f'{pl(c)} · {tname(c["roster"])}', f'${c["bid"]} → {c.get("pts", 0):.1f} pts'))
    trades = extras.get("trades")
    if trades:
        def margin(t):
            tot = sorted((s["total"] for s in t["sides"]), reverse=True)
            return tot[0] - (tot[1] if len(tot) > 1 else 0)
        t = max(trades, key=margin)
        win = max(t["sides"], key=lambda s: s["total"])
        if margin(t) > 0:
            items.append(("Best trade", tname(win["roster"]),
                          f'{", ".join(pl(p) for p in win["players"]) or "picks"} · won by {margin(t):.1f} pts'))
    draft = extras.get("draft")
    if draft:
        kept = [p for p in draft["picks"] if p.get("kept", True) and not p["keeper"]]
        if kept:
            s_ = max(kept, key=lambda p: p["value"])
            items.append(("Steal of the draft", f'{pl(s_)} · {tname(s_["roster"])}',
                          f'Rd {s_["round"]} · {s_["pos"]}{s_["drafted"]} → {s_["pos"]}{s_["finish"]}'))
        early = [p for p in draft["picks"] if p["round"] <= 3]
        if early:
            b = min(early, key=lambda p: p["value"])
            items.append(("Biggest bust", f'{pl(b)} · {tname(b["roster"])}',
                          f'Rd {b["round"]} · {b["pos"]}{b["drafted"]} → {b["pos"]}{b["finish"]}'))
    moves = extras.get("moves")
    if moves and moves["teams"]:
        r, tally = max(moves["teams"].items(), key=lambda kv: sum(v for k, v in kv[1].items() if k != "drops"))
        items.append(("Busiest manager", tname(r), f'{sum(v for k, v in tally.items() if k != "drops")} moves'))

    brackets = detail.get("brackets") or {}
    consolation = ""
    if brackets.get("losers"):
        toilet = toilet_bowl(brackets["losers"])
        consolation = (f'<section><h2 class="sec">{"Toilet Bowl" if toilet else "Consolation Bracket"}</h2>'
                       f'{render_bracket(brackets["losers"], hist, season, base, consolation=True)}'
                       + ('<p class="hint">The lower score advances; whoever survives the final finishes last.</p>' if toilet else "")
                       + "</section>")
    body = f"""<section><div class="awards">{podium}</div></section>
<section><h2 class="sec">Playoff Bracket</h2>{render_bracket(brackets.get("winners", []), hist, season, base)}</section>
{consolation}
{tiles("Season Awards", items)}
<section><h2 class="sec">Final Standings</h2><div class="tbl"><table>
  <thead><tr><th>Seed</th><th class="tn">Team</th><th>Record</th><th>PF</th><th class="tn">Finish</th></tr></thead>
  <tbody>{table}</tbody></table></div></section>"""
    footer = "Records include median games, as Sleeper counts them. Seeds are by wins, then points."
    return page(data, base, sidebar(season, base, site, "recap"), season_nav(site, "recap", season),
                f"{e(data['league'])} · {e(season)}", f"{season} Season Recap", body, footer, f"{season} season recap")


def manager_link(mgr, uid, base, label=None):
    """A manager's name (or `label`) with avatar, linking to their manager page."""
    m = mgr.get(uid)
    if not m:
        return html.escape(label or "?")
    return (f'<a class="who mlink" href="{base}history/m-{uid}.html">{avatar(m)}<span>{html.escape(label or m["name"])}'
            f'<small>{html.escape(m["manager"])}</small></span></a>')


def award_counts(hist):
    """How often each manager has won each weekly award, across every regular-season week."""
    weeks = {}
    for g in hist["games"]:
        weeks.setdefault((g["season"], g["week"]), []).append(g)
    keys = ("top", "basement", "blowout", "closest", "unlucky", "lucky")
    counts = {}
    bump = lambda uid, k: counts.setdefault(uid, dict.fromkeys(keys, 0)).__setitem__(k, counts[uid][k] + 1)
    for games in weeks.values():
        scores = [(g["a_pts"], g["a"]) for g in games] + [(g["b_pts"], g["b"]) for g in games]
        bump(max(scores)[1], "top")
        bump(min(scores)[1], "basement")
        res = [((g["a"], g["a_pts"]), (g["b"], g["b_pts"])) if g["a_pts"] >= g["b_pts"]
               else ((g["b"], g["b_pts"]), (g["a"], g["a_pts"])) for g in games]  # (winner, loser)
        margin = lambda x: x[0][1] - x[1][1]
        bump(max(res, key=margin)[0][0], "blowout")
        bump(min(res, key=margin)[0][0], "closest")
        bump(max(res, key=lambda x: x[1][1])[1][0], "unlucky")
        bump(min(res, key=lambda x: x[0][1])[0][0], "lucky")
    return counts, keys


def render_managers(hist, data, site):
    totals, _, order, pct_ = history_totals(hist)
    mgr = hist["managers"]
    cards = "".join(
        f'<a class="mcard" href="m-{u}.html">{avatar(mgr[u])}<div><b>{html.escape(mgr[u]["name"])}</b>'
        f'<small>{html.escape(mgr[u]["manager"])}</small><div class="beat">{wlt(totals[u])} · '
        f'{len(totals[u]["seasons"])} season{"s" if len(totals[u]["seasons"]) != 1 else ""}'
        f'{" · " + "🏆" * totals[u]["titles"] if totals[u]["titles"] else ""}</div></div></a>'
        for u in order if totals[u]["games"])
    return history_page(hist, data, site, "managers", "Managers", f'<div class="mgrid">{cards}</div>',
                        "Regular-season head-to-head records across every season.", "League managers")


def render_manager(uid, hist, data, site, extras_by_season):
    e, base, mgr = html.escape, "../", hist["managers"]
    m = mgr[uid]
    totals, pairs, _, pct_ = history_totals(hist)
    me = totals[uid]

    seasons_rows, best_trades, pickups, steals = [], [], [], []
    for season in sorted(hist["seasons"], reverse=True):
        d = hist["seasons"][season]
        if uid not in d["standings"]:
            continue
        st = d["standings"][uid]
        mine = {r for r, o in d["rosters"].items() if o == uid}
        x = extras_by_season.get(season, {})
        grade = spent = moves = "–"
        if x.get("draft"):
            picks = [p for p in x["draft"]["picks"] if p["roster"] in mine and not p["keeper"]]
            grade = f"{draft_grade(picks):+.1f}" if picks else "–"
            steals += [(p, season) for p in picks if p.get("kept", True) and p["value"] > 0]
        if x.get("faab"):
            claims = [c for c in x["faab"]["claims"] if c["roster"] in mine]
            spent = f'${sum(c["bid"] for c in claims)}'
            pickups += [(c, season) for c in claims]
        if x.get("moves"):
            t = next((v for r, v in x["moves"]["teams"].items() if r in mine), {})
            moves = sum(v for k, v in t.items() if k != "drops")
        for t in x.get("trades") or []:
            side = next((s for s in t["sides"] if s["roster"] in mine), None)
            if side and any(x["total"] for x in t["sides"]):
                other = max((s["total"] for s in t["sides"] if s is not side), default=0)
                best_trades.append((side["total"] - other, side, t, season))
        seasons_rows.append(
            f'<tr><td>{season}</td><td class="tn">{e(hist["names"][season].get(uid, m["name"]))}</td>'
            f'<td>{st["w"]}-{st["l"]}{"-" + str(st["t"]) if st["t"] else ""}</td><td>{st["pf"]:.2f}</td><td>{st["seed"]}</td>'
            f'<td class="tn">{e(st.get("finish") or "In progress")}</td><td>{grade}</td><td>{spent}</td><td>{moves}</td></tr>')

    rivals = "".join(
        f'<tr><td class="tn">{manager_link(mgr, o, base)}</td><td class="{"win" if r["w"] > r["l"] else "loss" if r["w"] < r["l"] else ""}">{wlt(r)}</td></tr>'
        for (a, o), r in sorted(((k, v) for k, v in pairs.items() if k[0] == uid),
                                key=lambda kv: -(kv[1]["w"] + kv[1]["l"] + kv[1]["t"])) if o in mgr)

    def trade_line(item):
        margin, side, t, season = item
        got = ", ".join(pl(p) for p in side["players"]) or "picks/FAAB"
        return (f'<li><span class="amt {"up" if margin > 0 else "down"}">{margin:+.0f}</span><div><b>{got}</b>'
                f'<div class="beat">{season} Wk {t["week"]} · {side["total"]:.1f} pts for them</div></div></li>')
    best_trades.sort(key=lambda x: x[0])
    trades_html = ""
    if best_trades:
        trades_html = f"""<div><h3>Best trades</h3><ul class="bids">{"".join(trade_line(x) for x in best_trades[::-1][:3])}</ul></div>
  <div><h3>Worst trades</h3><ul class="bids">{"".join(trade_line(x) for x in best_trades[:3] if x[0] < 0) or "<li><div>None lost yet</div></li>"}</ul></div>"""
    pick_html = "".join(f'<li><span class="amt">{c.get("pts", 0):.0f}<small>pts</small></span><div><b>{pl(c)}</b> '
                        f'<span class="pos">{e(c["pos"])}</span><div class="beat">{season} · ${c["bid"]} in Wk {c["week"]}</div></div></li>'
                        for c, season in sorted(pickups, key=lambda x: -x[0].get("pts", 0))[:3])
    steal_html = "".join(f'<li><span class="amt up">{p["value"]:+d}</span><div><b>{pl(p)}</b> '
                         f'<span class="pos">{e(p["pos"])}</span><div class="beat">{season} · Rd {p["round"]} · '
                         f'{p["pos"]}{p["drafted"]} → {p["pos"]}{p["finish"]}</div></div></li>'
                         for p, season in sorted(steals, key=lambda x: -x[0]["value"])[:3])

    best = min((hist["seasons"][s]["standings"][uid].get("finish") for s in hist["seasons"]
                if uid in hist["seasons"][s]["standings"] and hist["seasons"][s]["standings"][uid].get("finish")),
               key=lambda f: ["Champion", "Runner-up", "3rd place", "4th place", "5th place", "6th place", "Playoffs",
                              "Missed playoffs"].index(f), default="–")
    head = tiles("Career", [("Seasons", str(len(me["seasons"])), ""), ("Record", wlt(me), f"{pct_(me):.3f}"),
                            ("Titles", "🏆" * me["titles"] or "0", ""), ("Best finish", e(best), ""),
                            ("Pts / game", f'{me["pf"] / max(me["games"], 1):.1f}', "")])
    body = f"""{head}
<section><h2 class="sec">Season by Season</h2><div class="tbl"><table>
  <thead><tr><th>Season</th><th class="tn">Team</th><th>Record</th><th>PF</th><th>Seed</th><th class="tn">Finish</th>
  <th>Draft grade</th><th>FAAB spent</th><th>Moves</th></tr></thead><tbody>{"".join(seasons_rows)}</tbody></table></div></section>
<section><h2 class="sec">Moves</h2><div class="records">{trades_html}
  <div><h3>Best pickups</h3><ul class="bids">{pick_html or "<li><div>None yet</div></li>"}</ul></div>
  <div><h3>Best draft steals</h3><ul class="bids">{steal_html or "<li><div>None yet</div></li>"}</ul></div></div></section>
<section><h2 class="sec">Head-to-Head</h2><div class="tbl"><table class="h2h-list">
  <thead><tr><th class="tn">Opponent</th><th>Record</th></tr></thead><tbody>{rivals}</tbody></table></div></section>"""
    footer = ("Career record and head-to-head are regular-season games only (no median games). Season records are "
              "Sleeper's and include median games.")
    return history_page(hist, data, site, "managers", e(m["name"]), body, footer, f'{m["name"]} manager page')


def history_totals(hist):
    """All-time regular-season record per manager, and head-to-head records between each pair."""
    tally = lambda: {"w": 0, "l": 0, "t": 0}
    totals = {uid: {**tally(), "pf": 0.0, "pa": 0.0, "games": 0, "seasons": set(), "titles": 0} for uid in hist["managers"]}
    pairs = {}
    for g in hist["games"]:
        for me, mine, opp, theirs in ((g["a"], g["a_pts"], g["b"], g["b_pts"]), (g["b"], g["b_pts"], g["a"], g["a_pts"])):
            k = "w" if mine > theirs else "l" if mine < theirs else "t"
            totals[me][k] += 1
            totals[me]["pf"] += mine
            totals[me]["pa"] += theirs
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
                  for key, label in (("records", "Record Book"), ("h2h", "Head-to-Head"), ("managers", "Managers")))
    seasons = sorted({g["season"] for g in hist["games"]})
    span = f"{seasons[0]}–{seasons[-1]}" if seasons else ""
    return page(data, base, sidebar(str(data["season"]), base, site, current), nav,
                f"{html.escape(data['league'])} · {span}", heading, body, footer, description)


def ordinal(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def postseason(hist, kind):
    """Playoff ("playoffs") or toilet bowl ("toilet") games from history.json."""
    return [g for g in hist.get("playoff_games") or [] if g.get("bracket", "playoffs") == kind]


def last_place(hist):
    """{season: manager} who lost the toilet bowl game."""
    return {g["season"]: g["l"] for g in postseason(hist, "toilet") if g["stage"] == "Toilet bowl"}


def postseason_breakdown(hist, uid):
    """Every playoff and toilet bowl game a manager played, newest first, with how each season ended."""
    e, mgr, lasts = html.escape, hist["managers"], last_place(hist)
    team = lambda s, u: e(hist["names"].get(s, {}).get(u) or mgr[u]["name"])
    games = [g for g in hist.get("playoff_games") or [] if uid in (g["w"], g["l"])]
    rows, seen = [], set()
    for g in sorted(games, key=lambda g: (g["season"], g["week"]), reverse=True):
        s, won = g["season"], g["w"] == uid
        finish = ""
        if s not in seen:
            seen.add(s)
            finish = (hist.get("playoffs", {}).get(s, {}).get(uid)
                      or ("🚽 Toilet bowl “winner”" if lasts.get(s) == uid else "Escaped toilet bowl" if s in lasts else "In progress"))
        mine, theirs = (g["w_pts"], g["l_pts"]) if won else (g["l_pts"], g["w_pts"])
        rows.append(f'<tr><td class="tn">{s if finish else ""}</td><td class="tn">{finish}</td><td class="tn">{g["stage"]}</td>'
                    f'<td class="tn">{team(s, g["l"] if won else g["w"])}</td><td>{mine:.1f}–{theirs:.1f}</td>'
                    f'<td class="{"win" if won else "loss"}">{"W" if won else "L"}</td></tr>')
    return ('<table class="inner"><thead><tr><th class="tn">Season</th><th class="tn">Finish</th><th class="tn">Round</th>'
            '<th class="tn">Opponent</th><th>Score</th><th>Result</th></tr></thead><tbody>' + "".join(rows) + '</tbody></table>')


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
            toilet = {u for g in postseason(hist, "toilet") if g["season"] == s for u in (g["w"], g["l"])}
            result = hist["playoffs"][s].get(uid) or ("🚽 Toilet bowl “winner”" if last_place(hist).get(s) == uid else
                                                      "Escaped toilet bowl" if uid in toilet else "Missed")
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
        pickup = (f'{pl(best)} <small>${best["bid"]} · {best["started"]:.0f} pts started</small>'
                  if best and best.get("started") else "–")
        trades = (str(sum(any(side["roster"] in rids for side in t["sides"]) for t in x["trades"]))
                  if x.get("trades") is not None else "–")
        picks = [p for p in ((x.get("draft") or {}).get("picks") or []) if p["owner"] == uid and not p["keeper"]]
        draft = f"{draft_grade(picks):+.1f}" if picks else "–"
        rows.append(f'<tr><td class="tn">{s}</td><td class="tn">{e(hist["names"].get(s, {}).get(uid, ""))}</td>'
                    f'<td>{wlt(r)}</td><td>{median}</td><td><b>{wlt(overall)}</b></td><td>{sum(m for m, _ in mine) / max(len(mine), 1):.1f}</td>'
                    f'<td>{sum(o for _, o in mine) / max(len(mine), 1):.1f}</td>'
                    f'<td>{max((m for m, _ in mine), default=0):.1f}</td><td>{finish}</td><td class="tn">{result}</td>'
                    f'<td>{faab}</td><td class="tn">{pickup}</td><td>{trades}</td><td>{draft}</td></tr>')
    return ('<table class="inner"><thead><tr><th class="tn">Season</th><th class="tn">Team</th><th>Record</th>'
            '<th>vs median</th><th>Overall</th><th>PF / game</th><th>PA / game</th><th>High score</th><th>Finish</th><th class="tn">Playoffs</th>'
            '<th>FAAB spent · claims</th><th class="tn">Best pickup</th><th>Trades</th><th>Draft value</th>'
            '</tr></thead><tbody>' + "".join(rows) + '</tbody></table>')


def render_records(hist, data, site, extras=None, latest=None):
    e, mgr, names = html.escape, hist["managers"], hist["names"]
    totals, _, order, pct = history_totals(hist)
    who = lambda uid: manager_link(mgr, uid, "../")
    # Inside an expand button the name can't also be a link; the expanded row links to the manager page instead.
    plain = lambda uid: (f'<span class="who">{avatar(mgr[uid])}<span>{e(mgr[uid]["name"])}'
                         f'<small>{e(mgr[uid]["manager"])}</small></span></span>')
    page_link = lambda uid: f'<a class="mpage" href="m-{uid}.html">{e(mgr[uid]["name"])}’s manager page →</a>'
    team = lambda season, uid: e(names[season].get(uid) or mgr[uid]["name"])

    lasts = last_place(hist)
    toilets = lambda u: "🚽" * sum(v == u for v in lasts.values()) or "–"
    standings = "".join(
        f'<tr class="mgr"><td class="tn"><button class="expand" aria-expanded="false" aria-controls="seasons-{u}">'
        f'{plain(u)}<span class="chev" aria-hidden="true">▸</span></button></td><td>{len(totals[u]["seasons"])}</td>'
        f'<td>{wlt(totals[u])}</td><td>{pct(totals[u]):.3f}</td><td>{totals[u]["pf"] / max(totals[u]["games"], 1):.1f}</td>'
        f'<td>{totals[u]["pa"] / max(totals[u]["games"], 1):.1f}</td>'
        f'<td>{"🏆" * totals[u]["titles"] or "–"}</td><td>{toilets(u)}</td></tr>'
        f'<tr class="more" id="seasons-{u}" hidden><td colspan="8">{page_link(u)}{season_breakdown(hist, extras or {}, latest or {}, u)}</td></tr>'
        for u in order if totals[u]["games"])
    loser = lambda s: f'{team(s, lasts[s])} <small>{e(mgr[lasts[s]]["manager"])}</small>' if s in lasts else "–"
    champs = "".join(
        f'<tr><td class="tn">{f["season"]}</td><td class="tn"><b>{team(f["season"], f["champion"])}</b> '
        f'<small>{e(mgr[f["champion"]]["manager"])}</small></td>'
        f'<td class="tn">{team(f["season"], f["runner_up"])} <small>{e(mgr[f["runner_up"]]["manager"])}</small></td>'
        f'<td class="tn">{loser(f["season"])}</td></tr>'
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

    counts, keys = award_counts(hist)
    award_rows = "".join(f'<tr><td class="tn">{manager_link(mgr, u, "../")}</td>{"".join(f"<td>{c[k] or chr(8211)}</td>" for k in keys)}</tr>'
                         for u, c in sorted(counts.items(), key=lambda kv: (-kv[1]["top"], kv[1]["basement"])) if u in mgr)
    po, tb = postseason(hist, "playoffs"), postseason(hist, "toilet")
    post_rows = []
    for u in order:
        trips = {g["season"] for g in po if u in (g["w"], g["l"])}
        tb_trips = {g["season"] for g in tb if u in (g["w"], g["l"])}
        if not (trips or tb_trips):
            continue
        rec = lambda gs: wlt({"w": sum(g["w"] == u for g in gs), "l": sum(g["l"] == u for g in gs), "t": 0})
        finals = sum(g["stage"] == "Championship" and u in (g["w"], g["l"]) for g in po)
        post_rows.append((-totals[u]["titles"], -finals, -len(trips), len(tb_trips), mgr[u]["name"].lower(),
            f'<tr><td class="tn"><button class="expand" aria-expanded="false" aria-controls="post-{u}">'
            f'{plain(u)}<span class="chev" aria-hidden="true">▸</span></button></td><td>{len(trips) or "–"}</td>'
            f'<td>{rec([g for g in po if u in (g["w"], g["l"])]) if trips else "–"}</td><td>{finals or "–"}</td>'
            f'<td>{"🏆" * totals[u]["titles"] or "–"}</td><td>{len(tb_trips) or "–"}</td>'
            f'<td>{rec([g for g in tb if u in (g["w"], g["l"])]) if tb_trips else "–"}</td>'
            f'<td>{toilets(u)}</td></tr>'
            f'<tr class="more" id="post-{u}" hidden><td colspan="8">{page_link(u)}{postseason_breakdown(hist, u)}</td></tr>'))
    post_table = "".join(r[-1] for r in sorted(post_rows))
    post_score = lambda gs: [(g["w_pts"], g["w"], g) for g in gs] + [(g["l_pts"], g["l"], g) for g in gs]
    post_when = lambda g: f'{g["season"]} {g["stage"]}'
    post_line = lambda g: f'{team(g["season"], g["w"])} over {team(g["season"], g["l"])}'
    post_lists = "".join([
        top5("Highest playoff scores", [(f"{p:.1f}", team(g["season"], u), post_when(g))
                                        for p, u, g in sorted(post_score(po), key=lambda x: -x[0])]),
        top5("Lowest playoff scores", [(f"{p:.1f}", team(g["season"], u), post_when(g))
                                       for p, u, g in sorted(post_score(po), key=lambda x: x[0])]),
        top5("Biggest playoff blowouts", [(f"+{g['w_pts'] - g['l_pts']:.1f}", post_line(g), post_when(g))
                                          for g in sorted(po, key=lambda g: g["l_pts"] - g["w_pts"])]),
        top5("Closest playoff games", [(f"+{g['w_pts'] - g['l_pts']:.2f}", post_line(g), post_when(g))
                                       for g in sorted(po, key=lambda g: g["w_pts"] - g["l_pts"])]),
        top5("Highest toilet bowl scores", [(f"{p:.1f}", team(g["season"], u), post_when(g))
                                            for p, u, g in sorted(post_score(tb), key=lambda x: -x[0])]),
        top5("Lowest toilet bowl scores", [(f"{p:.1f}", team(g["season"], u), post_when(g))
                                           for p, u, g in sorted(post_score(tb), key=lambda x: x[0])]),
    ]) if po or tb else ""
    postseason_html = "" if not post_rows else f"""<section><h2 class="sec">Postseason</h2><div class="tbl"><table>
  <thead><tr><th class="tn">Manager</th><th>Playoff trips</th><th>Playoff record</th><th>Finals</th><th>Titles</th>
  <th>Toilet bowl trips</th><th>Toilet bowl record</th><th>Toilet bowl “wins”</th></tr></thead>
  <tbody>{post_table}</tbody></table></div><p class="hint">Tap a manager for every playoff and toilet bowl game they've played.</p></section>"""

    body = f"""<section><h2 class="sec">Champions</h2><div class="tbl"><table>
  <thead><tr><th class="tn">Season</th><th class="tn">Champion</th><th class="tn">Runner-up</th><th class="tn">Toilet bowl “winner” 🚽</th></tr></thead>
  <tbody>{champs or '<tr><td class="tn" colspan="4">No completed seasons yet.</td></tr>'}</tbody></table></div></section>
<section><h2 class="sec">All-Time Standings</h2><div class="tbl"><table class="alltime">
  <thead><tr><th class="tn">Manager</th><th>Seasons</th><th>Record</th><th>Win %</th><th>PF / game</th><th>PA / game</th><th>Titles</th><th>Toilet bowls</th></tr></thead>
  <tbody>{standings}</tbody></table></div><p class="hint">Tap a manager for their season-by-season breakdown.</p></section>
{postseason_html}
<section><h2 class="sec">Weekly Award Counts</h2><div class="tbl"><table>
  <thead><tr><th class="tn">Manager</th><th>Top score</th><th>Basement</th><th>Biggest blowout</th><th>Closest win</th>
  <th>Unluckiest loss</th><th>Luckiest win</th></tr></thead><tbody>{award_rows}</tbody></table></div>
<p class="hint">How many regular-season weeks each manager won each weekly award.</p></section>
<section><h2 class="sec">Records</h2><div class="records">{lists}</div></section>
{f'<section><h2 class="sec">Postseason Records</h2><div class="records">{post_lists}</div></section>' if post_lists else ""}
<script>
document.querySelectorAll('.expand').forEach(function (b) {{
  b.addEventListener('click', function () {{
    var open = b.getAttribute('aria-expanded') !== 'true';
    b.setAttribute('aria-expanded', open);
    document.getElementById(b.getAttribute('aria-controls')).hidden = !open;
  }});
}});
</script>"""
    footer = ("Standings, Records and streaks count regular-season head-to-head games only; median games and "
              "playoffs aren't counted. Postseason counts only games on the path to the title (no 3rd or 5th place "
              "games) and losers-bracket games on the path to the toilet bowl, where the higher score is the win. "
              "The toilet bowl “winner” 🚽 is the team that lost the toilet bowl game and finished last. "
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
        rows.append(f'<tr><td class="tn">{manager_link(mgr, me, "../")}</td>'
                    f'<td class="tot">{wlt(totals[me])}</td>{"".join(cells)}</tr>')
    body = f"""<section><h2 class="sec">Regular Season</h2><div class="tbl"><table class="h2h">
  <thead><tr><th class="tn">Manager</th><th>All-time</th>{head}</tr></thead>
  <tbody>{"".join(rows)}</tbody></table></div></section>
{playoff_h2h(hist)}"""
    footer = ("Read across a row: that manager's record against each opponent, across every season. "
              "Green means they lead the series. Regular Season counts head-to-head games only (no median games). "
              "Playoffs counts only games on the path to the title (no 3rd or 5th place games). Toilet Bowl counts "
              "only losers-bracket games on the path to the toilet bowl; the higher score is the win, since the "
              "lower score moves on toward last place.")
    return history_page(hist, data, site, "h2h", "Head-to-Head", body, footer, "All-time head-to-head records")


def playoff_h2h(hist):
    """Playoff and toilet bowl sections for the Head-to-Head page."""
    games = hist.get("playoff_games") or []
    return "".join(bracket_h2h(hist, [g for g in games if g.get("bracket", "playoffs") == kind], title, label)
                   for kind, title, label in (("playoffs", "Playoffs", "Playoffs"), ("toilet", "Toilet Bowl", "Record")))


def bracket_h2h(hist, games, title, label):
    """Record grid (only managers who played one of these games) plus every game, newest season first."""
    e, mgr = html.escape, hist["managers"]
    if not games:
        return ""
    tally = lambda: {"w": 0, "l": 0, "t": 0}
    totals, pairs = {}, {}
    for g in games:
        for me, opp, k in ((g["w"], g["l"], "w"), (g["l"], g["w"], "l")):
            totals.setdefault(me, tally())[k] += 1
            pairs.setdefault((me, opp), tally())[k] += 1
    pct = lambda r: r["w"] / max(r["w"] + r["l"], 1)
    order = sorted(totals, key=lambda u: (-pct(totals[u]), -totals[u]["w"], mgr[u]["name"].lower()))
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
    team = lambda s, u: e(hist["names"].get(s, {}).get(u) or mgr[u]["name"])
    log = []
    for s in sorted({g["season"] for g in games}, reverse=True):
        items = "".join(
            f'<li><span class="amt">{g["w_pts"]:.1f}<small>{g["l_pts"]:.1f}</small></span><div><b>{team(s, g["w"])}</b> '
            f'over {team(s, g["l"])}<div class="beat">{g["stage"]} · Wk {g["week"]}</div></div></li>'
            for g in sorted((g for g in games if g["season"] == s), key=lambda g: -g["week"]))
        log.append(f'<div><h3>{s}</h3><ul class="bids">{items}</ul></div>')
    return f"""<section><h2 class="sec">{title}</h2><div class="tbl"><table class="h2h">
  <thead><tr><th class="tn">Manager</th><th>{label}</th>{head}</tr></thead>
  <tbody>{"".join(rows)}</tbody></table></div></section>
<section><h2 class="sec">{title.removesuffix("s")} Games</h2><div class="records">{"".join(log)}</div></section>
"""


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
        big_txt = f'{pl(big)}, {money(big["bid"])}, Wk {big["week"]}' if big and big["bid"] else "–"
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
        <div><b>{e(name(c['roster']))}</b> won <b>{pl(c)}</b> <span class="pos">{e(c['pos'])}</span>
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
        return (f'<li><span class="amt">{val}<small>{unit}</small></span><div><b>{pl(c)}</b> '
                f'<span class="pos">{e(c["pos"])}</span> · {e(name(c["roster"]))}<div class="beat">{money(c["bid"])} in '
                f'Wk {c["week"]} → {c.get("started", 0):.1f} pts started · {weeks}</div></div></li>')
    values = sorted((c for c in claims if c["bid"] >= 1), key=lambda c: -c.get("started", 0))[:5]
    # Judge a bust only once he's had a couple of weeks, or played and is already gone.
    busts = sorted((c for c in claims if c["bid"] >= 10 and (c.get("weeks", 0) >= 2
                                                             or c.get("weeks", 0) and not c.get("active"))),
                   key=lambda c: (c.get("started", 0), -c["bid"]))[:5]
    free = sorted((c for c in claims if not c["bid"]), key=lambda c: -c.get("started", 0))[:5]
    lst = lambda items: "".join(items) or "<li><div>None yet</div></li>"
    roi = "" if not claims else f"""<section><h2 class="sec">Waiver ROI</h2><div class="records">
  <div><h3>Best paid pickups ($1+ bids)</h3><ul class="bids">{lst(roi_line(c, f"{c.get('started', 0):.1f}", "pts") for c in values)}</ul></div>
  <div><h3>Biggest busts ($10+ bids)</h3><ul class="bids">{lst(roi_line(c, f"{c.get('started', 0):.1f}", "pts") for c in busts)}</ul></div>
  <div><h3>Best free pickups ($0)</h3><ul class="bids">{lst(roi_line(c, f"{c.get('started', 0):.1f}", "pts") for c in free)}</ul></div>
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
    extras = {s: {k: read_json(WEEKS / s / f"{k}.json") for k in ("faab", "trades", "draft", "moves")} for s in seasons}
    hist = read_json(HISTORY)
    site = {"latest_week": {s: weeks_written(s)[-1] for s in seasons},
            "history": hist is not None,
            "complete": {f["season"]: True for f in (hist or {}).get("finishes", [])},
            "faab_seasons": {s for s in seasons if extras[s]["faab"]},
            "trade_seasons": {s for s in seasons if extras[s]["trades"] is not None},
            "draft_seasons": {s for s in seasons if extras[s]["draft"]},
            "moves_seasons": {s for s in seasons if extras[s]["moves"]},
            "stats_seasons": set(seasons),
            "recap_seasons": {s for s in seasons if (hist or {}).get("seasons", {}).get(s, {}).get("complete")}}
    if IMAGES.exists():
        shutil.copytree(IMAGES, SITE / "images", dirs_exist_ok=True)
    setup_cards(seasons)
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
        (SITE / s / "stats.html").write_text(render_stats(data, site), encoding="utf-8")
        if x["moves"]:
            moves = {k: v["moves"] for k, v in extras.items() if v["moves"]}
            (SITE / s / "moves.html").write_text(render_moves(x["moves"], data, site, moves, hist), encoding="utf-8")
        if s in site["recap_seasons"]:
            (SITE / s / "recap.html").write_text(render_recap(s, data, site, hist, x), encoding="utf-8")
        print(f"Built {s}: {len(weeks)} week(s) + " + ", ".join(k for k, v in x.items() if v is not None))
    if hist:
        (SITE / "history").mkdir(exist_ok=True)
        (SITE / "history" / "records.html").write_text(render_records(hist, data, site, extras, latest), encoding="utf-8")
        (SITE / "history" / "h2h.html").write_text(render_h2h(hist, data, site), encoding="utf-8")
        (SITE / "history" / "managers.html").write_text(render_managers(hist, data, site), encoding="utf-8")
        for uid in hist["managers"]:
            (SITE / "history" / f"m-{uid}.html").write_text(render_manager(uid, hist, data, site, extras),
                                                             encoding="utf-8")
        print("Built league history")
    print("Open docs/index.html to preview.")


SORT_JS = r"""<script>
// Every table sorts by any column. A cell's data-v (if set) is its sort value; otherwise it's read from the text:
// numbers, $ and %, records like 9-1 (by win %), <1% / >99%, and runs of 🏆 / 🚽 (by count). Blanks sort last.
(function () {
  function value(cell) {
    if (cell.dataset.v !== undefined) return cell.dataset.v;
    var t = (cell.innerText || '').trim().split('\n')[0].trim();
    if (t === '' || t === '–' || t === '-' || t === '—') return '';
    var rec = t.match(/^(\d+)-(\d+)(?:-(\d+))?$/);
    if (rec) {
      var w = +rec[1], l = +rec[2], d = +(rec[3] || 0);
      return String((w + d / 2) / Math.max(w + l + d, 1) + w * 1e-6);
    }
    if (t === '<1%') return '0.5';
    if (t === '>99%') return '99.5';
    var icons = t.match(/^(?:🏆|🚽)+$/u);
    if (icons) return String(Array.from(t).length);
    var num = t.replace(/\u2212/g, '-').match(/^([+-]?)\$?([\d,]*\.?\d+)/);
    if (num) return String((num[1] === '-' ? -1 : 1) * parseFloat(num[2].replace(/,/g, '')));
    return t.toLowerCase();
  }
  document.querySelectorAll('main table').forEach(function (table) {
    var head = table.tHead && table.tHead.rows[0], body = table.tBodies[0];
    if (!head || !body || body.rows.length < 2) return;
    // Rows to move together: a row plus any expandable detail rows (tr.more) that follow it.
    function groups() {
      var out = [];
      Array.prototype.forEach.call(body.rows, function (r) {
        if (r.classList.contains('more') && out.length) out[out.length - 1].push(r); else out.push([r]);
      });
      return out;
    }
    Array.prototype.forEach.call(head.cells, function (th, col) {
      var vals = groups().map(function (g) { return g[0].cells[col] ? value(g[0].cells[col]) : ''; });
      if (!th.dataset.sort) {
        var filled = vals.filter(function (v) { return v !== ''; });
        var nums = filled.filter(function (v) { return !isNaN(parseFloat(v)) && isFinite(v); });
        th.dataset.sort = filled.length && nums.length >= filled.length * 0.6 ? 'num' : 'text';
      }
      th.tabIndex = 0;
      function sort() {
        var now = th.getAttribute('aria-sort');
        // First click: numbers high to low, names A to Z. Clicking again flips it.
        var asc = now ? now === 'descending' : th.dataset.sort === 'text';
        Array.prototype.forEach.call(head.cells, function (h) { h.removeAttribute('aria-sort'); });
        th.setAttribute('aria-sort', asc ? 'ascending' : 'descending');
        var num = th.dataset.sort === 'num';
        var rows = groups().map(function (g) { return { g: g, v: g[0].cells[col] ? value(g[0].cells[col]) : '' }; });
        rows.sort(function (a, b) {
          if (a.v === '' || b.v === '') return a.v === b.v ? 0 : a.v === '' ? 1 : -1;  // blanks always last
          var d = num ? parseFloat(a.v) - parseFloat(b.v) : String(a.v).localeCompare(String(b.v), undefined, { numeric: true });
          return asc ? d : -d;
        });
        rows.forEach(function (r) { r.g.forEach(function (tr) { body.appendChild(tr); }); });
      }
      th.addEventListener('click', sort);
      th.addEventListener('keydown', function (ev) { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); sort(); } });
    });
  });
})();
</script>"""


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{site_name}</title>
<meta property="og:title" content="{site_name}">
<meta property="og:site_name" content="{site_name}">
<meta property="og:description" content="{description}">
<style>{css}</style>{theme_head}</head>
<body><div class="layout">
<div class="mbar"><button class="menu-btn" type="button" aria-expanded="false" aria-controls="side"><span aria-hidden="true">&#9776;</span> Menu</button></div>
<aside class="seasons" id="side">{side}</aside>
<main class="wrap">
<header>{logo}<div><div class="kicker">{kicker}</div><h1>{heading}</h1></div><button class="theme" type="button" aria-label="Switch light/dark mode"></button></header>
<nav>{nav}</nav>
{body}
<footer>{footer}</footer>
</main></div>{scripts}</body></html>
"""

# Apply a saved light/dark choice before first paint so the page doesn't flash the other theme.
THEME_HEAD = """<script>try{var t=localStorage.getItem('theme');if(t)document.documentElement.dataset.theme=t}catch(e){}</script>"""

# The header button flips between light and dark; with no saved choice the page follows the device setting.
THEME_JS = """<script>
(function () {
  var b = document.querySelector('.theme');
  if (!b) return;
  var root = document.documentElement, mq = window.matchMedia('(prefers-color-scheme: dark)');
  function cur() { return root.dataset.theme || (mq.matches ? 'dark' : 'light'); }
  function paint() {
    var dark = cur() === 'dark';
    b.textContent = dark ? '\\u2600\\ufe0e' : '\\u263e';
    b.title = dark ? 'Switch to light mode' : 'Switch to dark mode';
    b.setAttribute('aria-label', b.title);
  }
  b.addEventListener('click', function () {
    var t = cur() === 'dark' ? 'light' : 'dark';
    root.dataset.theme = t;
    try { localStorage.setItem('theme', t); } catch (e) {}
    paint();
  });
  if (mq.addEventListener) mq.addEventListener('change', paint);
  paint();
})();
</script>"""

# Phones: the Menu button opens the sidebar links as a panel, and the swipeable week row starts on the current week.
MOBILE_JS = """<script>
(function () {
  var btn = document.querySelector('.menu-btn'), side = document.getElementById('side');
  function setOpen(open) { side.classList.toggle('open', open); btn.setAttribute('aria-expanded', open); }
  if (btn && side) {
    btn.addEventListener('click', function (ev) { ev.stopPropagation(); setOpen(!side.classList.contains('open')); });
    document.addEventListener('click', function (ev) { if (!side.contains(ev.target)) setOpen(false); });
    document.addEventListener('keydown', function (ev) { if (ev.key === 'Escape') setOpen(false); });
  }
  var nav = document.querySelector('nav'), cur = nav && nav.querySelector('.cur');
  if (cur && nav.scrollWidth > nav.clientWidth) {
    nav.scrollLeft += cur.getBoundingClientRect().left - nav.getBoundingClientRect().left - (nav.clientWidth - cur.offsetWidth) / 2;
  }
})();
</script>"""

# Player cards: tapping a player's name opens his Sleeper stats in a dialog, so nobody leaves the site.
# Profile and league history come from the page (card_data); game logs load from Sleeper's stats API on open.
PLAYER_JS = r"""<script>
(function () {
  var data = document.getElementById('pl-data');
  if (!data) return;
  var D = JSON.parse(data.textContent), API = 'https://api.sleeper.com/stats/nfl/player/';
  var REC = [['rec_tgt', 'Tgt'], ['rec', 'Rec'], ['rec_yd', 'Rec Yd'], ['rec_td', 'Rec TD']];
  var COLS = {
    QB: [['pass_yd', 'Pass Yd'], ['pass_td', 'Pass TD'], ['pass_int', 'INT'], ['rush_yd', 'Rush Yd'], ['rush_td', 'Rush TD']],
    RB: [['rush_att', 'Car'], ['rush_yd', 'Rush Yd'], ['rush_td', 'Rush TD'], ['rec', 'Rec'], ['rec_yd', 'Rec Yd'], ['rec_td', 'Rec TD']],
    WR: REC, TE: REC,
    K: [['fgm', 'FGM'], ['fga', 'FGA'], ['xpm', 'XPM']],
    DEF: [['sack', 'Sack'], ['int', 'INT'], ['fum_rec', 'FR'], ['def_td', 'TD'], ['pts_allow', 'PA']]
  };
  var cache = {}, dlg;

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }
  function num(v) { return v ? String(Math.round(v * 10) / 10) : '0'; }
  function pts(v) { return v.toFixed(1); }
  function ordinal(n) { return n + (['th', 'st', 'nd', 'rd'][(n % 100 - 20) % 10] || ['th', 'st', 'nd', 'rd'][n % 100] || 'th'); }
  // Seasons before the league existed use its earliest scoring; later ones its latest.
  function scoring(season) {
    var have = Object.keys(D.scoring).sort();
    if (!have.length) return {};
    return D.scoring[season] || D.scoring[season < have[0] ? have[0] : have[have.length - 1]];
  }
  function score(stats, sc) {
    var t = 0;
    for (var k in sc) if (typeof stats[k] === 'number') t += stats[k] * sc[k];
    return t;
  }
  // Position ranks are Sleeper's half-PPR ranks, which is how the league scores.
  var RANK = 'pos_rank_half_ppr';
  function getJSON(url) {
    return fetch(url).then(function (r) { return r.ok ? r.json() : {}; }).catch(function () { return null; });
  }

  // Every season of a player's career, newest first: [{season, games: [{wk, opp, played, stats, pts}]}].
  function load(pid, p) {
    if (cache[pid]) return cache[pid];
    var last = +D.latest, first = last - (p.exp == null ? 4 : Math.min(p.exp, 25)), years = [];
    for (var y = last; y >= first; y--) years.push(y);
    cache[pid] = Promise.all(years.map(function (y) {
      var url = API + encodeURIComponent(pid) + '?season_type=regular&season=' + y;
      // By week for the game log; the season totals carry the season-long position rank.
      return Promise.all([getJSON(url + '&grouping=week'), getJSON(url)]).then(function (r) {
        return { season: String(y), weeks: r[0], total: r[1] };
      });
    })).then(function (list) {
      if (list.every(function (s) { return s.weeks === null; })) throw new Error('offline');
      return list.filter(function (s) { return s.weeks; }).map(function (s) {
        var sc = scoring(s.season), key = RANK, games = [];
        Object.keys(s.weeks).sort(function (a, b) { return a - b; }).forEach(function (w) {
          var g = s.weeks[w];
          if (!g || !g.stats) return;
          var st = g.stats;
          games.push({ wk: +w, team: g.team, opp: g.opponent ? (g.is_away_team ? '@' : '') + g.opponent : '',
                       played: (st.gp || 0) > 0 || (st.pts_ppr || 0) !== 0, stats: st, pts: score(st, sc), rank: st[key] });
        });
        var total = s.total && s.total.stats;
        return { season: s.season, games: games, rank: total ? total[key] : null };
      }).filter(function (s) { return s.games.some(function (g) { return g.played; }); });
    });
    cache[pid].catch(function () { delete cache[pid]; });
    return cache[pid];
  }

  function table(head, rows, cls) {
    return '<div class="tbl"><table' + (cls ? ' class="' + cls + '"' : '') + '><thead><tr>' +
      head.map(function (h, i) { return '<th' + (i < 2 ? ' class="tn"' : '') + '>' + h + '</th>'; }).join('') +
      '</tr></thead><tbody>' + rows.join('') + '</tbody></table></div>';
  }

  function career(p, seasons) {
    var cols = COLS[p.p] || [];
    var rows = seasons.map(function (s) {
      var played = s.games.filter(function (g) { return g.played; }), tot = {}, total = 0, teams = {};
      played.forEach(function (g) {
        total += g.pts;
        if (g.team) teams[g.team] = 1;
        cols.forEach(function (c) { tot[c[0]] = (tot[c[0]] || 0) + (g.stats[c[0]] || 0); });
      });
      return '<tr class="pc-season" data-season="' + s.season + '" tabindex="0"><td class="tn">' + s.season + '</td><td class="tn">' +
        esc(Object.keys(teams).join('/')) + '</td><td>' + played.length + '</td><td><b>' + pts(total) + '</b></td><td>' +
        pts(played.length ? total / played.length : 0) + '</td>' + rankCell(p, s.rank) +
        cols.map(function (c) { return '<td>' + num(tot[c[0]]) + '</td>'; }).join('') + '</tr>';
    });
    return table(['Season', 'Team', 'GP', 'Pts', 'PPG', 'Rank'].concat(cols.map(function (c) { return c[1]; })), rows, 'pc-career');
  }

  // Position rank like RB4; top 12 at the position is highlighted.
  function rankCell(p, r) {
    if (!r) return '<td class="pc-rank">–</td>';
    return '<td class="pc-rank' + (r <= 12 ? ' hi' : '') + '">' + esc(p.p) + r + '</td>';
  }

  function gameLog(p, s) {
    var cols = COLS[p.p] || [];
    var rows = s.games.map(function (g) {
      if (!g.played) {
        return '<tr class="dnp"><td class="tn">' + g.wk + '</td><td class="tn">' + esc(g.opp) + '</td><td colspan="' +
          (cols.length + 2) + '">Did not play</td></tr>';
      }
      return '<tr><td class="tn">' + g.wk + '</td><td class="tn">' + esc(g.opp) + '</td><td><b>' + pts(g.pts) + '</b></td>' +
        rankCell(p, g.rank) + cols.map(function (c) { return '<td>' + num(g.stats[c[0]]) + '</td>'; }).join('') + '</tr>';
    });
    return '<h3>' + s.season + ' games</h3>' +
      table(['Wk', 'Opp', 'Pts', 'Rank'].concat(cols.map(function (c) { return c[1]; })), rows);
  }

  function league(p) {
    if (!p.rv.length) return '';
    var rows = p.rv.map(function (r) {
      return '<tr><td class="tn">' + r[0] + '</td><td class="tn">' + esc(r[1]) + '</td><td>' + r[2] + '</td><td>' +
        pts(r[3]) + '</td><td><b>' + pts(r[4]) + '</b></td></tr>';
    });
    return '<h3>On Red Vines rosters</h3>' + table(['Season', 'Team', 'Weeks', 'Pts', 'As starter'], rows);
  }

  function header(pid, p) {
    var photo = p.p === 'DEF' ? 'https://sleepercdn.com/images/team_logos/nfl/' + pid.toLowerCase() + '.png'
                              : 'https://sleepercdn.com/content/nfl/players/thumb/' + encodeURIComponent(pid) + '.jpg';
    var line1 = [p.p, p.t || 'Free agent', p.no != null && p.p !== 'DEF' ? '#' + p.no : ''].filter(Boolean).join(' · ');
    var line2 = p.p === 'DEF' ? '' : [p.age ? 'Age ' + p.age : '', p.exp == null ? '' : p.exp ? ordinal(p.exp + 1) + ' season' : 'Rookie',
                                      p.col].filter(Boolean).join(' · ');
    return '<div class="pc-head"><img class="pc-photo" src="' + photo + '" alt="" onerror="this.style.visibility=\'hidden\'">' +
      '<div class="pc-id"><div class="pc-name">' + esc(p.n) + (p.inj ? ' <span class="pc-inj">' + esc(p.inj) + '</span>' : '') + '</div>' +
      '<div class="pc-sub">' + esc(line1) + '</div>' + (line2 ? '<div class="pc-sub">' + esc(line2) + '</div>' : '') + '</div>' +
      '<button type="button" class="pc-x" aria-label="Close">&times;</button></div>';
  }

  function open(pid) {
    var p = D.players[pid];
    if (!p) return;
    if (!dlg) {
      dlg = document.createElement('dialog');
      dlg.className = 'pcard';
      document.body.appendChild(dlg);
      dlg.addEventListener('click', function (ev) {
        if (ev.target === dlg || ev.target.closest('.pc-x')) dlg.close();
        var row = ev.target.closest('.pc-season');
        if (row) pick(row);
      });
      dlg.addEventListener('keydown', function (ev) {
        var row = ev.target.closest && ev.target.closest('.pc-season');
        if (row && (ev.key === 'Enter' || ev.key === ' ')) { ev.preventDefault(); pick(row); }
      });
    }
    dlg.setAttribute('aria-label', p.n);
    dlg.innerHTML = header(pid, p) + '<div class="pc-body">' + league(p) +
      '<h3>Career</h3><div class="pc-career-box"><p class="pc-note">Loading stats from Sleeper…</p></div>' +
      '<div class="pc-log"></div><p class="pc-note">Stats from Sleeper. Points use Red Vines scoring. Tap a season to see its games.</p></div>';
    if (!dlg.open) dlg.showModal();
    dlg.scrollTop = 0;
    var box = dlg.querySelector('.pc-career-box');
    load(pid, p).then(function (seasons) {
      if (dlg.getAttribute('aria-label') !== p.n) return;  // another card was opened meanwhile
      if (!seasons.length) { box.innerHTML = '<p class="pc-note">No NFL games yet.</p>'; return; }
      dlg._seasons = seasons;
      dlg._player = p;
      box.innerHTML = career(p, seasons);
      pick(box.querySelector('.pc-season'));
    }).catch(function () {
      box.innerHTML = '<p class="pc-note">Couldn’t reach Sleeper. Check your connection and tap the name again.</p>';
    });
  }

  function pick(row) {
    if (!row || !dlg._seasons) return;
    var season = row.dataset.season;
    dlg.querySelectorAll('.pc-season').forEach(function (r) { r.classList.toggle('cur', r === row); });
    var s = dlg._seasons.filter(function (x) { return x.season === season; })[0];
    dlg.querySelector('.pc-log').innerHTML = gameLog(dlg._player, s);
  }

  document.addEventListener('click', function (ev) {
    var b = ev.target.closest && ev.target.closest('button.pl');
    if (b) open(b.dataset.pid);
  });
})();
</script>"""

DARK = "--bg:#121417;--card:#1b1e23;--ink:#eceef1;--muted:#9aa0aa;--line:#2b2f36;--chip:#252930;--up:#4cc27e;--down:#ff7a66;--hl:#3987e5;--trend:#454a53;--pos:#3987e5;--neg:#e66767;--neutral:#383835;color-scheme:dark"

CSS = """
:root{--bg:#f6f5f2;--card:#fff;--ink:#1b1d21;--muted:#6b6f78;--line:#e4e2dc;--chip:#f0eee9;--up:#1f8a4c;--down:#c8402f;--hl:#2a78d6;--trend:#c4c1b9;--pos:#2a78d6;--neg:#e34948;--neutral:#f0efec;color-scheme:light}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){""" + DARK + """}}
:root[data-theme=dark]{""" + DARK + """}
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
.mbar{display:none}
@media (max-width:760px){
  .layout{display:block}
  .mbar{display:flex;position:sticky;top:0;z-index:20;height:60px;align-items:center;padding:0 16px;background:var(--bg);border-bottom:1px solid var(--line)}
  .menu-btn{font:inherit;font-size:.95rem;font-weight:600;color:var(--ink);background:var(--card);border:1px solid var(--line);border-radius:999px;min-height:40px;padding:0 16px;display:inline-flex;align-items:center;gap:8px;cursor:pointer}
  .menu-btn:focus-visible{outline:2px solid var(--hl);outline-offset:2px}
  .seasons{display:none;position:fixed;top:60px;left:0;right:0;z-index:19;width:auto;max-height:calc(100vh - 60px);overflow-y:auto;padding:14px 16px 18px;background:var(--bg);border-bottom:1px solid var(--line);box-shadow:0 12px 24px rgba(0,0,0,.12)}
  .seasons.open{display:block}
  .side-group+.side-group{margin-top:16px}
  .seasons-lbl{margin:0 0 8px 2px}
  .seasons-list{flex-direction:row;flex-wrap:wrap;gap:8px}
  .seasons a{padding:9px 14px;border:1px solid var(--line);border-radius:999px;background:var(--card);color:var(--ink)}
  .seasons a.cur{background:var(--ink);color:var(--bg);border-color:var(--ink);box-shadow:none}
  .wrap{padding-top:20px}
}
header{display:flex;gap:16px;align-items:center}
.logo{width:64px;height:64px;border-radius:14px}
.theme{margin-left:auto;align-self:flex-start;flex:none;width:38px;height:38px;border-radius:999px;border:1px solid var(--line);background:var(--card);color:var(--ink);font-size:1.15rem;line-height:1;cursor:pointer;display:inline-flex;align-items:center;justify-content:center}
.theme:hover{background:var(--chip)}
.theme:focus-visible{outline:2px solid var(--hl);outline-offset:2px}
.kicker{color:var(--muted);font-size:.85rem;letter-spacing:.04em;text-transform:uppercase}
h1{margin:.1em 0 0;font-size:clamp(1.7rem,5vw,2.4rem);line-height:1.15}
h2{margin:0;font-size:1.1rem}
nav{display:flex;flex-wrap:wrap;gap:6px;margin:20px 0 28px}
nav a{padding:4px 10px;border-radius:999px;border:1px solid var(--line);color:var(--muted);text-decoration:none;font-size:.85rem}
nav a.cur{background:var(--ink);color:var(--bg);border-color:var(--ink)}
@media (max-width:760px){
  nav{flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none;margin:16px -16px 24px;padding:2px 16px}
  nav::-webkit-scrollbar{display:none}
  nav a{flex:none;padding:7px 14px;font-size:.9rem}
}
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
table.inner td.win{color:var(--up);font-weight:700}
table.inner td.loss{color:var(--down);font-weight:700}
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
.legend{display:flex;align-items:center;gap:3px;color:var(--muted);font-size:.8rem;margin:0 0 8px}
.legend span:last-child{margin-left:8px}
.sw{width:22px;height:12px;border-radius:3px;background:var(--neutral)}
.heat td{min-width:64px}
.heat .hi1,.sw.hi1{background:color-mix(in srgb,var(--pos) 18%,var(--neutral))}
.heat .hi2,.sw.hi2{background:color-mix(in srgb,var(--pos) 38%,var(--neutral))}
.heat .hi3,.sw.hi3{background:color-mix(in srgb,var(--pos) 60%,var(--neutral))}
.heat .lo1,.sw.lo1{background:color-mix(in srgb,var(--neg) 18%,var(--neutral))}
.heat .lo2,.sw.lo2{background:color-mix(in srgb,var(--neg) 38%,var(--neutral))}
.heat .lo3,.sw.lo3{background:color-mix(in srgb,var(--neg) 60%,var(--neutral))}
.heat tr.avg td{color:var(--muted);font-weight:600;border-top:1px solid var(--line);border-bottom:0}
td small,.tn small{color:var(--muted);font-size:.75rem}
.bracket{display:flex;gap:18px;overflow-x:auto;padding-bottom:6px}
.round{display:flex;flex-direction:column;justify-content:space-around;gap:12px;min-width:220px}
.rlbl{color:var(--muted);font-size:.72rem;text-transform:uppercase;letter-spacing:.05em}
.match{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}
.mlbl{font-size:.7rem;color:var(--muted);padding:6px 10px 0;text-transform:uppercase;letter-spacing:.04em}
.slot{display:flex;justify-content:space-between;gap:10px;padding:7px 10px;color:var(--muted);font-size:.9rem}
.slot+.slot{border-top:1px solid var(--line)}
.slot.won{color:var(--ink);font-weight:700}
.slot b{font-variant-numeric:tabular-nums;font-weight:inherit}
.slot.tbd{font-style:italic}
.podium .who{display:flex;align-items:center;gap:8px;font-size:1.05rem}
a.mpage{display:inline-block;margin:4px 0 8px;font-size:.85rem;color:var(--hl)}
a.mlink{color:inherit;text-decoration:none}a.mlink:hover span{text-decoration:underline}
.mgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:10px}
.mcard{display:flex;gap:12px;align-items:center;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;color:inherit;text-decoration:none}
.mcard:hover{border-color:var(--muted)}
.mcard small{display:block;color:var(--muted)}
.h2h-list .win{color:var(--up);font-weight:600}.h2h-list .loss{color:var(--down)}
th[data-sort]{cursor:pointer;user-select:none;white-space:nowrap}
th[data-sort]::after{content:"↕";margin-left:4px;opacity:.35}
th[aria-sort=ascending]::after{content:"↑";opacity:1}th[aria-sort=descending]::after{content:"↓";opacity:1}
th[data-sort]:hover,th[data-sort]:focus-visible{color:var(--ink)}
footer{margin-top:40px;color:var(--muted);font-size:.8rem;text-align:center}
button.pl{all:unset;cursor:pointer;text-decoration:underline dotted color-mix(in srgb,currentColor 45%,transparent);text-underline-offset:3px}
button.pl:hover{text-decoration-style:solid;text-decoration-color:currentColor}
button.pl:focus-visible{outline:2px solid var(--hl);outline-offset:2px;border-radius:3px}
html:has(dialog.pcard[open]){overflow:hidden}
dialog.pcard{width:min(760px,calc(100vw - 32px));max-height:min(88vh,900px);padding:0;border:1px solid var(--line);border-radius:16px;background:var(--bg);color:var(--ink);box-shadow:0 24px 60px rgba(0,0,0,.3)}
dialog.pcard::backdrop{background:rgba(10,12,16,.55)}
.pc-head{position:sticky;top:0;z-index:1;display:flex;gap:14px;align-items:center;padding:16px;background:var(--card);border-bottom:1px solid var(--line)}
.pc-photo{width:72px;height:72px;border-radius:50%;object-fit:cover;background:var(--chip);flex:none}
.pc-id{min-width:0}
.pc-name{font-size:1.35rem;font-weight:800;line-height:1.2}
.pc-sub{color:var(--muted);font-size:.88rem}
.pc-inj{display:inline-block;vertical-align:middle;padding:1px 8px;border-radius:99px;background:color-mix(in srgb,var(--down) 16%,transparent);color:var(--down);font-size:.72rem;font-weight:700}
.pc-x{margin-left:auto;align-self:flex-start;flex:none;width:38px;height:38px;border-radius:999px;border:1px solid var(--line);background:var(--bg);color:var(--ink);font-size:1.4rem;line-height:1;cursor:pointer}
.pc-x:hover{background:var(--chip)}
.pc-x:focus-visible{outline:2px solid var(--hl);outline-offset:2px}
.pc-body{padding:0 16px 18px}
.pc-body h3{margin:20px 0 8px}
.pc-body table{font-size:.85rem}
.pc-body th,.pc-body td{padding:8px 10px}
.pc-body tr.pc-season{cursor:pointer}
.pc-body tr.pc-season:hover td{background:var(--chip)}
.pc-body tr.pc-season.cur td{background:color-mix(in srgb,var(--hl) 14%,var(--card))}
.pc-body tr.pc-season:focus-visible{outline:2px solid var(--hl);outline-offset:-2px}
.pc-body tr.dnp td{color:var(--muted);font-style:italic}
.pc-body td.pc-rank{color:var(--muted);font-weight:600}
.pc-body td.pc-rank.hi{color:var(--up)}
.pc-note{color:var(--muted);font-size:.82rem;margin:10px 2px 0}
@media (max-width:760px){
  dialog.pcard{width:100vw;max-width:100vw;height:100dvh;max-height:100dvh;margin:0;border:0;border-radius:0}
  .pc-photo{width:56px;height:56px}
  .pc-name{font-size:1.15rem}
}
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
