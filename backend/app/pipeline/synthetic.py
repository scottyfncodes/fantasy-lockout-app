"""Deterministic synthetic season generator.

Retrosheet and ProSportsTransactions are the real sources (see
``retrosheet.py`` / ``prosportstransactions.py``), but both need outbound
network access.  This generator produces a complete, internally consistent
season from a seed so the draft, replay, IL and waiver engines can be
developed, tested and demoed with no network at all.

"Internally consistent" is the point: box scores are built from simulated
plate appearances, team runs are derived from those events, and the pitching
lines are allocated against the *opposing* team's actual hits and runs.  A
game's two halves therefore agree with each other, which is what the replay
engine and the scoring tests need.  Rare events (cycles, grand slams,
no-hitters, perfect games) occur at roughly their real rates.
"""

from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass, field
from typing import Any

from ..season_calendar import monday_of

TEAMS = [
    "ARI", "ATL", "BAL", "BOS", "CHC", "CHW", "CIN", "CLE", "COL", "DET",
    "HOU", "KCR", "LAA", "LAD", "MIA", "MIL", "MIN", "NYM", "NYY", "OAK",
    "PHI", "PIT", "SDP", "SEA", "SFG", "STL", "TBR", "TEX", "TOR", "WSN",
]

FIRST_NAMES = [
    "Alex", "Brandon", "Carlos", "Diego", "Eddie", "Felix", "Gary", "Hector",
    "Ivan", "Jose", "Kyle", "Luis", "Manny", "Nolan", "Omar", "Pedro",
    "Quinn", "Ryan", "Sam", "Tyler", "Ubaldo", "Victor", "Wade", "Xavier",
    "Yadier", "Zack", "Cody", "Dustin", "Evan", "Freddie", "Grant", "Hunter",
    "Isaac", "Jared", "Kenny", "Logan", "Miguel", "Nick", "Oscar", "Paul",
]
LAST_NAMES = [
    "Alvarez", "Boone", "Cabrera", "Delgado", "Ellis", "Fowler", "Garcia",
    "Hayes", "Ingram", "Jimenez", "Keller", "Lopez", "Moreno", "Nunez",
    "Ortega", "Perez", "Quintana", "Ramirez", "Sanders", "Torres", "Ulrich",
    "Vargas", "Whitfield", "Yates", "Zimmer", "Barnes", "Castillo", "Doyle",
    "Espinoza", "Franklin", "Gibson", "Holloway", "Iverson", "Jennings",
    "Klein", "Lindstrom", "Mancini", "Novak", "Oakley", "Pryor", "Reyes",
    "Sutton", "Trevino", "Underwood", "Vance", "Wagner", "Yorke", "Zapata",
]

BATTER_TEMPLATE = [
    # (position, count) — 15 batters per club, several with secondary spots
    ("C", 2), ("1B", 1), ("2B", 2), ("3B", 2), ("SS", 2), ("OF", 5), ("DH", 1),
]
PITCHER_TEMPLATE = [("SP", 6), ("RP", 11)]

INJURY_NOTES = [
    "strained left hamstring", "right elbow inflammation", "lower back strain",
    "left oblique strain", "sprained right ankle", "shoulder impingement",
    "fractured hamate bone", "concussion protocol", "right knee soreness",
    "forearm tightness", "groin strain", "sprained left thumb",
]


@dataclass
class SeasonData:
    year: int
    source: str
    opening_day: dt.date
    final_game_day: dt.date
    all_star_monday: dt.date
    players: list[dict[str, Any]] = field(default_factory=list)
    games: list[dict[str, Any]] = field(default_factory=list)
    batting: list[dict[str, Any]] = field(default_factory=list)
    pitching: list[dict[str, Any]] = field(default_factory=list)
    il_stints: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# calendar
# ---------------------------------------------------------------------------

def opening_day_for(year: int) -> dt.date:
    """First Thursday on or after March 26 — MLB's usual opening slot."""
    d = dt.date(year, 3, 26)
    while d.weekday() != 3:
        d += dt.timedelta(days=1)
    return d


def all_star_break_days(year: int) -> set[dt.date]:
    """Monday-Thursday of All-Star week: the season's only multi-day gap."""
    d = dt.date(year, 7, 1)
    while d.weekday() != 1:  # first Tuesday
        d += dt.timedelta(days=1)
    asg = d + dt.timedelta(days=7)  # second Tuesday
    mon = monday_of(asg)
    return {mon + dt.timedelta(days=i) for i in range(4)}


# ---------------------------------------------------------------------------
# player pool
# ---------------------------------------------------------------------------

def _make_name(rng: random.Random, used: set[str]) -> str:
    for _ in range(200):
        name = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
        if name not in used:
            used.add(name)
            return name
    name = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)} {len(used)}"
    used.add(name)
    return name


def build_players(rng: random.Random, year: int) -> list[dict[str, Any]]:
    players: list[dict[str, Any]] = []
    used_names: set[str] = set()
    pid = 0

    for team in TEAMS:
        for pos, count in BATTER_TEMPLATE:
            for depth in range(count):
                pid += 1
                positions = [pos]
                # Multi-position players are eligible everywhere they played.
                if rng.random() < 0.28:
                    extra = rng.choice(["1B", "2B", "3B", "SS", "OF", "DH"])
                    if extra not in positions:
                        positions.append(extra)
                # Regulars are better than depth pieces.
                tier = 1.0 - 0.16 * depth + rng.gauss(0, 0.12)
                players.append({
                    "player_id": f"S{year}{pid:04d}",
                    "season": year,
                    "name": _make_name(rng, used_names),
                    "mlb_team": team,
                    "positions": ",".join(positions),
                    "is_pitcher": 0,
                    "bats": rng.choice("RRRLLS"),
                    "throws": rng.choice("RRRL"),
                    "_talent": {
                        "playing_time": max(0.12, min(1.0, tier)),
                        "contact": max(0.55, rng.gauss(1.0, 0.13)),
                        "power": max(0.25, rng.gauss(1.0, 0.35)),
                        "eye": max(0.35, rng.gauss(1.0, 0.28)),
                        "speed": max(0.05, rng.gauss(1.0, 0.55)),
                    },
                })
        for pos, count in PITCHER_TEMPLATE:
            for depth in range(count):
                pid += 1
                positions = [pos]
                if pos == "RP" and rng.random() < 0.12:
                    positions.append("SP")  # swingman: eligible both ways
                tier = 1.0 - 0.09 * depth + rng.gauss(0, 0.12)
                players.append({
                    "player_id": f"S{year}{pid:04d}",
                    "season": year,
                    "name": _make_name(rng, used_names),
                    "mlb_team": team,
                    "positions": ",".join(positions),
                    "is_pitcher": 1,
                    "bats": rng.choice("RRRL"),
                    "throws": rng.choice("RRRL"),
                    "_talent": {
                        "playing_time": max(0.15, min(1.0, tier)),
                        "k_rate": max(0.5, rng.gauss(1.0, 0.24)),
                        "suppression": max(0.6, rng.gauss(1.0, 0.16)),
                        "stamina": max(0.5, rng.gauss(1.0, 0.15)),
                        "closer": 0.0,
                    },
                })

        # One reliever per club is the closer, one or two are setup men.
        staff = [p for p in players if p["mlb_team"] == team and p["is_pitcher"]]
        pen = [p for p in staff if p["positions"].startswith("RP")]
        if pen:
            pen[0]["_talent"]["closer"] = 1.0
            for setup in pen[1:3]:
                setup["_talent"]["closer"] = 0.5
    return players


# ---------------------------------------------------------------------------
# schedule
# ---------------------------------------------------------------------------

def build_schedule(rng: random.Random, year: int) -> list[dict[str, Any]]:
    start = opening_day_for(year)
    end = dt.date(year, 9, 29)
    break_days = all_star_break_days(year)

    games: list[dict[str, Any]] = []
    gid = 0
    day = start
    while day <= end:
        if day in break_days:
            day += dt.timedelta(days=1)
            continue
        # Most clubs play most days; Mondays and Thursdays carry more off days.
        play_rate = 0.74 if day.weekday() in (0, 3) else 0.95
        playing = [t for t in TEAMS if rng.random() < play_rate]
        if len(playing) % 2:
            playing.pop()
        rng.shuffle(playing)
        for i in range(0, len(playing), 2):
            gid += 1
            games.append({
                "game_id": f"G{year}{gid:05d}",
                "season": year,
                "date": day.isoformat(),
                "home": playing[i],
                "away": playing[i + 1],
            })
        day += dt.timedelta(days=1)
    return games


# ---------------------------------------------------------------------------
# one game
# ---------------------------------------------------------------------------

# League-average per-PA outcome rates; each batter scales them by talent.
BASE = {"bb": 0.083, "hbp": 0.010, "so": 0.222, "hr": 0.034, "b3": 0.005, "b2": 0.047, "b1": 0.145}


def _bat_line(rng: random.Random, player: dict, pa: int, shutdown: bool) -> dict[str, int]:
    t = player["_talent"]
    line = {k: 0 for k in ("pa", "ab", "r", "h", "b1", "b2", "b3", "hr", "rbi",
                           "bb", "ibb", "hbp", "so", "sb", "cs", "slam")}
    p_bb = BASE["bb"] * t["eye"]
    p_hbp = BASE["hbp"]
    p_so = BASE["so"] / t["contact"]
    p_hr = BASE["hr"] * t["power"]
    p_b3 = BASE["b3"] * t["speed"] * t["contact"]
    p_b2 = BASE["b2"] * t["contact"]
    p_b1 = BASE["b1"] * t["contact"]
    if shutdown:  # facing a pitcher having an unhittable day
        p_hr = p_b3 = p_b2 = p_b1 = 0.0
        p_bb *= 0.35
        p_hbp *= 0.35
        p_so *= 1.25

    for _ in range(pa):
        line["pa"] += 1
        roll = rng.random()
        for key, prob in (("bb", p_bb), ("hbp", p_hbp), ("so", p_so), ("hr", p_hr),
                          ("b3", p_b3), ("b2", p_b2), ("b1", p_b1)):
            if roll < prob:
                line[key] += 1
                break
            roll -= prob
        else:
            line["ab"] += 1  # an out in play

    line["ab"] += line["so"] + line["hr"] + line["b3"] + line["b2"] + line["b1"]
    line["h"] = line["b1"] + line["b2"] + line["b3"] + line["hr"]
    if line["bb"] and rng.random() < 0.038 * line["bb"]:
        line["ibb"] = 1

    on_base = line["h"] + line["bb"] + line["hbp"] - line["hr"]
    if on_base > 0:
        attempts = sum(1 for _ in range(on_base) if rng.random() < 0.085 * t["speed"])
        for _ in range(attempts):
            if rng.random() < 0.73:
                line["sb"] += 1
            else:
                line["cs"] += 1
    return line


def _distribute_offense(rng: random.Random, lines: list[dict[str, int]]) -> int:
    """Derive team runs from the events, then hand out R and RBI consistently."""
    tot = {k: sum(l[k] for l in lines) for k in ("b1", "b2", "b3", "hr", "bb", "hbp")}
    expected = (0.36 * tot["b1"] + 0.55 * tot["b2"] + 0.80 * tot["b3"] + 1.45 * tot["hr"]
                + 0.28 * (tot["bb"] + tot["hbp"])) * 0.80
    runs = max(0, int(round(rng.gauss(expected, 1.1))))
    runs = max(runs, tot["hr"])  # every homer scores at least its hitter

    # Home runs first: they carry their own run and RBI, plus any runners.
    rbi_left = runs
    for line in lines:
        for _ in range(line["hr"]):
            line["r"] += 1
            extra = 0
            if rbi_left - 1 > 0:
                roll = rng.random()
                cap = min(3, rbi_left - 1)
                extra = 3 if (roll < 0.030 and cap >= 3) else min(cap, int(rng.random() * 2.2))
            if extra == 3:
                line["slam"] += 1
            line["rbi"] += 1 + extra
            rbi_left -= 1 + extra
    runs_left = runs - sum(l["r"] for l in lines)

    reach_weights = [max(0.0, l["b1"] + l["b2"] * 1.3 + l["b3"] * 1.7 + (l["bb"] + l["hbp"]) * 0.8)
                     for l in lines]
    for _ in range(max(0, runs_left)):
        idx = _weighted_pick(rng, reach_weights)
        if idx is None:
            break
        lines[idx]["r"] += 1

    hit_weights = [max(0.0, l["b1"] * 0.6 + l["b2"] * 1.4 + l["b3"] * 1.8) for l in lines]
    for _ in range(max(0, rbi_left)):
        idx = _weighted_pick(rng, hit_weights)
        if idx is None:
            break
        lines[idx]["rbi"] += 1
    return runs


def _weighted_pick(rng: random.Random, weights: list[float]) -> int | None:
    total = sum(weights)
    if total <= 0:
        return None
    target = rng.random() * total
    for i, w in enumerate(weights):
        target -= w
        if target <= 0:
            return i
    return len(weights) - 1


def _pitching_side(
    rng: random.Random,
    staff: list[dict],
    rotation_index: int,
    opp_hits: int,
    opp_runs: int,
    opp_bb: int,
    opp_hbp: int,
    opp_so: int,
    opp_hr: int,
    gem: bool,
    won: bool,
    margin: int,
) -> list[dict[str, Any]]:
    starters = [p for p in staff if "SP" in p["positions"].split(",")]
    pen = [p for p in staff if "RP" in p["positions"].split(",")]
    if not starters or not pen:
        return []
    sp = starters[rotation_index % len(starters)]

    if gem:
        sp_outs = 27
    else:
        sp_outs = int(round(rng.gauss(17.5 * sp["_talent"]["stamina"], 4.0)))
        sp_outs = max(3, min(27, sp_outs))
        if sp_outs >= 24 and rng.random() > 0.22:
            sp_outs = 21  # complete games are rare

    appearances: list[tuple[dict, int]] = [(sp, sp_outs)]
    remaining = 27 - sp_outs
    while remaining > 0:
        chunk = min(remaining, 3 if rng.random() < 0.75 else remaining)
        save_spot = remaining <= 3 and won and 0 < margin <= 3
        pool = [p for p in pen if p["_talent"]["closer"] >= (1.0 if save_spot else 0.0)] or pen
        pick = pool[int(rng.random() * len(pool))]
        if any(pick is a for a, _ in appearances):
            pick = pen[int(rng.random() * len(pen))]
        appearances.append((pick, chunk))
        remaining -= chunk

    total_outs = sum(o for _, o in appearances) or 1
    lines: list[dict[str, Any]] = []
    hits_left, runs_left, so_left, bb_left, hr_left = opp_hits, opp_runs, opp_so, opp_bb, opp_hr
    for idx, (pitcher, outs) in enumerate(appearances):
        share = outs / total_outs
        last = idx == len(appearances) - 1
        take = lambda pool_left, s=share: (pool_left if last else
                                           min(pool_left, max(0, int(round(rng.gauss(pool_left * s, 0.9))))))
        h = take(hits_left); hits_left -= h
        r = take(runs_left); runs_left -= r
        so = take(so_left); so_left -= so
        bb = take(bb_left); bb_left -= bb
        hr = min(h, take(hr_left)); hr_left -= hr
        er = max(0, r - (1 if rng.random() < 0.09 * r else 0))
        line = {
            "player_id": pitcher["player_id"],
            "gs": 1 if idx == 0 else 0,
            "outs": outs,
            "bf": outs + h + bb + (opp_hbp if idx == 0 else 0),
            "h": h, "r": r, "er": er, "bb": bb,
            "ibb": 1 if bb and rng.random() < 0.04 else 0,
            "hbp": opp_hbp if idx == 0 else 0,
            "so": so, "hr": hr,
            "w": 0, "l": 0, "sv": 0, "hld": 0,
            "cg": 1 if outs >= 27 and idx == 0 else 0,
            "pick": 1 if rng.random() < 0.015 else 0,
            "errors_allowed": 0 if gem else (1 if rng.random() < 0.12 else 0),
        }
        if gem and idx == 0:
            # A no-hitter can still allow an unearned run (walk, error, wild
            # pitch), so the run total is left alone — only the hits go to zero.
            line.update(h=0, hr=0, er=0, cg=1,
                        bf=27 + bb + line["hbp"] + line["errors_allowed"])
            if rng.random() < 0.15:  # perfect game: nobody reaches, nobody scores
                line.update(bb=0, hbp=0, ibb=0, errors_allowed=0, bf=27, r=0)
        lines.append(line)

    # Decisions. Approximate the real rules closely enough to be recognisable.
    if won:
        starter_line = lines[0]
        if starter_line["outs"] >= 15 and rng.random() < 0.72:
            starter_line["w"] = 1
        else:
            mid = lines[1] if len(lines) > 1 else lines[0]
            mid["w"] = 1
        if len(lines) > 1 and 0 < margin <= 3:
            final = lines[-1]
            if not final["w"] and final["outs"] >= 3:
                final["sv"] = 1
            for mid in lines[1:-1]:
                if not mid["w"] and mid["r"] < margin and rng.random() < 0.65:
                    mid["hld"] = 1
    else:
        loser = max(lines, key=lambda l: (l["er"], l["outs"]))
        loser["l"] = 1
    return lines


def _lineup_for(rng: random.Random, roster: list[dict]) -> list[dict]:
    """Nine starters plus a couple of subs, weighted by playing time."""
    pool = sorted(roster, key=lambda p: -p["_talent"]["playing_time"])
    starters, bench = pool[:9], pool[9:]
    used = list(starters)
    for p in bench:
        if rng.random() < 0.30 * p["_talent"]["playing_time"] + 0.12:
            used.append(p)
    return used


def simulate_games(
    rng: random.Random,
    games: list[dict[str, Any]],
    players: list[dict[str, Any]],
) -> tuple[list[dict], list[dict]]:
    by_team_bat: dict[str, list[dict]] = {}
    by_team_pit: dict[str, list[dict]] = {}
    for p in players:
        (by_team_pit if p["is_pitcher"] else by_team_bat).setdefault(p["mlb_team"], []).append(p)

    rotation_counter: dict[str, int] = {t: 0 for t in TEAMS}
    batting_rows: list[dict] = []
    pitching_rows: list[dict] = []

    for game in games:
        home, away = game["home"], game["away"]
        # Decide gems first so the opposing offense is generated consistently.
        gem_home = rng.random() < 0.0007
        gem_away = rng.random() < 0.0007

        sides: dict[str, dict[str, Any]] = {}
        for team, shutdown in ((away, gem_home), (home, gem_away)):
            batters = _lineup_for(rng, by_team_bat[team])
            lines = []
            for i, batter in enumerate(batters):
                pa = 5 if i < 3 else 4
                if i >= 9:
                    pa = rng.choice([1, 1, 2])
                lines.append(_bat_line(rng, batter, pa, shutdown))
            runs = _distribute_offense(rng, lines)
            sides[team] = {"batters": batters, "lines": lines, "runs": runs}

        home_runs_scored = sides[home]["runs"]
        away_runs_scored = sides[away]["runs"]
        if home_runs_scored == away_runs_scored:  # no ties in baseball
            home_runs_scored += 1
            sides[home]["runs"] = home_runs_scored
            idx = _weighted_pick(rng, [1.0] * len(sides[home]["lines"]))
            if idx is not None:
                sides[home]["lines"][idx]["r"] += 1
                sides[home]["lines"][idx]["rbi"] += 1
        game["home_runs"] = home_runs_scored
        game["away_runs"] = away_runs_scored

        for team in (home, away):
            for batter, line in zip(sides[team]["batters"], sides[team]["lines"]):
                if line["pa"] == 0:
                    continue
                batting_rows.append({
                    "game_id": game["game_id"], "player_id": batter["player_id"],
                    "season": game["season"], "date": game["date"], "team": team,
                    "pos": batter["positions"].split(",")[0], **line,
                })

        for team, opp, gem, won in (
            (home, away, gem_home, home_runs_scored > away_runs_scored),
            (away, home, gem_away, away_runs_scored > home_runs_scored),
        ):
            opp_lines = sides[opp]["lines"]
            agg = lambda k: sum(l[k] for l in opp_lines)
            rot = rotation_counter[team]
            rotation_counter[team] += 1
            margin = abs(home_runs_scored - away_runs_scored)
            for line in _pitching_side(
                rng, by_team_pit[team], rot,
                opp_hits=agg("h"), opp_runs=sides[opp]["runs"], opp_bb=agg("bb"),
                opp_hbp=agg("hbp"), opp_so=agg("so"), opp_hr=agg("hr"),
                gem=gem, won=won, margin=margin,
            ):
                pitching_rows.append({
                    "game_id": game["game_id"], "season": game["season"],
                    "date": game["date"], "team": team, **line,
                })
    return batting_rows, pitching_rows


# ---------------------------------------------------------------------------
# injuries
# ---------------------------------------------------------------------------

def build_il_stints(
    rng: random.Random, year: int, players: list[dict], start: dt.date, end: dt.date
) -> list[dict[str, Any]]:
    stints: list[dict[str, Any]] = []
    span = (end - start).days
    for p in players:
        n = 0
        if rng.random() < 0.34:
            n = 1 if rng.random() < 0.78 else 2
        cursor = start
        for _ in range(n):
            offset = rng.randint(0, max(1, span - 20))
            begin = max(cursor, start + dt.timedelta(days=offset))
            length = rng.choice([10, 10, 15, 15, 21, 28, 40, 60])
            finish = begin + dt.timedelta(days=length)
            if begin >= end:
                break
            season_ending = finish >= end
            stints.append({
                "season": year,
                "player_id": p["player_id"],
                "start_date": begin.isoformat(),
                "end_date": None if season_ending else finish.isoformat(),
                "kind": "60-day IL" if length >= 60 else "10-day IL",
                "note": rng.choice(INJURY_NOTES),
            })
            if season_ending:
                break
            cursor = finish + dt.timedelta(days=14)
    return stints


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def generate_season(year: int, seed: int | None = None) -> SeasonData:
    rng = random.Random(seed if seed is not None else year * 7919)
    players = build_players(rng, year)
    games = build_schedule(rng, year)
    batting, pitching = simulate_games(rng, games, players)

    days = sorted({dt.date.fromisoformat(g["date"]) for g in games})
    stints = build_il_stints(rng, year, players, days[0], days[-1])

    for p in players:
        p.pop("_talent", None)

    return SeasonData(
        year=year,
        source="synthetic",
        opening_day=days[0],
        final_game_day=days[-1],
        all_star_monday=monday_of(sorted(all_star_break_days(year))[0]),
        players=players,
        games=games,
        batting=batting,
        pitching=pitching,
        il_stints=stints,
    )
