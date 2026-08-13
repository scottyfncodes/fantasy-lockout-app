"""Player pool, position eligibility and stat aggregation.

Two kinds of aggregate live here and the difference matters:

``season_totals``   full-season production.  Legitimate for *draft* rankings —
                    the draft happens before the replay starts and every
                    manager is choosing from the same finished season.

``stats_through``   production through a cut-off date only.  This is what the
                    free-agent pool, player pages during the season and the
                    bot managers are allowed to see.  Nothing in the in-season
                    UI or in bot decision-making may call ``season_totals``.
"""

from __future__ import annotations

import json
import sqlite3
from collections import OrderedDict
from typing import Any, Iterable

from ..config import BATTER_POSITIONS, PITCHER_POSITIONS
from ..scoring import ScoreLine, ScoringConfig, score_batting, score_pitching

BAT_SUM = """
    SELECT player_id,
           COUNT(*) AS g, SUM(pa) pa, SUM(ab) ab, SUM(r) r, SUM(h) h,
           SUM(b1) b1, SUM(b2) b2, SUM(b3) b3, SUM(hr) hr, SUM(rbi) rbi,
           SUM(bb) bb, SUM(ibb) ibb, SUM(hbp) hbp, SUM(so) so,
           SUM(sb) sb, SUM(cs) cs, SUM(slam) slam
      FROM batting_lines WHERE season = ? {date_clause}
     GROUP BY player_id
"""

PIT_SUM = """
    SELECT player_id,
           COUNT(*) AS g, SUM(gs) gs, SUM(outs) outs, SUM(bf) bf, SUM(h) h,
           SUM(r) r, SUM(er) er, SUM(bb) bb, SUM(ibb) ibb, SUM(hbp) hbp,
           SUM(so) so, SUM(hr) hr, SUM(w) w, SUM(l) l, SUM(sv) sv,
           SUM(hld) hld, SUM(cg) cg, SUM(pick) pick
      FROM pitching_lines WHERE season = ? {date_clause}
     GROUP BY player_id
"""


def list_players(conn: sqlite3.Connection, season: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT player_id, name, mlb_team, positions, is_pitcher FROM players WHERE season = ?",
        (season,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_player(conn: sqlite3.Connection, season: int, player_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT player_id, name, mlb_team, positions, is_pitcher, bats, throws "
        "FROM players WHERE season = ? AND player_id = ?",
        (season, player_id),
    ).fetchone()
    return dict(row) if row else None


def eligible_slots(player: dict[str, Any], active_slots: Iterable[str]) -> list[str]:
    """Which lineup slots this player may fill, from his real positions."""
    positions = {p for p in str(player["positions"]).split(",") if p}
    out: list[str] = []
    for slot in active_slots:
        if slot == "UTIL":
            if not player["is_pitcher"]:
                out.append(slot)
        elif slot == "P":
            if player["is_pitcher"]:
                out.append(slot)
        elif slot in positions:
            out.append(slot)
    return out


def position_group(player: dict[str, Any]) -> str:
    return "P" if player["is_pitcher"] else "B"


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------

def _fetch_sums(
    conn: sqlite3.Connection, season: int, through: str | None, since: str | None = None
) -> tuple[dict[str, dict], dict[str, dict]]:
    clause, params = "", [season]
    if since:
        clause += " AND date >= ?"
        params.append(since)
    if through:
        clause += " AND date <= ?"
        params.append(through)
    bat = {r["player_id"]: dict(r) for r in conn.execute(BAT_SUM.format(date_clause=clause), params)}
    pit = {r["player_id"]: dict(r) for r in conn.execute(PIT_SUM.format(date_clause=clause), params)}
    return bat, pit


def _score_totals(
    conn: sqlite3.Connection, season: int, cfg: ScoringConfig, through: str | None,
    since: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Fantasy points per player over a date window.

    Per-game bonuses (cycle, no-hitter, perfect game, quality start) cannot be
    read off a summed line, so they are counted from the daily rows and added
    back on top of the summed rate stats.
    """
    bat, pit = _fetch_sums(conn, season, through, since)
    bonuses = _count_bonuses(conn, season, cfg, through, since)

    out: dict[str, dict[str, Any]] = {}
    for pid, line in bat.items():
        # Per-game bonuses are counted separately from the daily rows; a season
        # total trivially "has" a single, double, triple and homer.
        scored = score_batting(line, cfg, include_derived=False)
        entry = out.setdefault(pid, {"player_id": pid, "points": 0.0, "batting": None,
                                     "pitching": None, "breakdown": {}})
        entry["batting"] = line
        entry["points"] += scored.points
        entry["breakdown"].update(scored.breakdown)
    for pid, line in pit.items():
        scored = score_pitching(line, cfg, include_derived=False)
        entry = out.setdefault(pid, {"player_id": pid, "points": 0.0, "batting": None,
                                     "pitching": None, "breakdown": {}})
        entry["pitching"] = line
        entry["points"] += scored.points
        for k, v in scored.breakdown.items():
            entry["breakdown"][k] = entry["breakdown"].get(k, 0.0) + v
    for pid, extra in bonuses.items():
        entry = out.setdefault(pid, {"player_id": pid, "points": 0.0, "batting": None,
                                     "pitching": None, "breakdown": {}})
        for k, v in extra.items():
            if k == "points":
                entry["points"] += v
            else:
                entry["breakdown"][k] = entry["breakdown"].get(k, 0.0) + v
    for entry in out.values():
        entry["points"] = round(entry["points"], 2)
    return out


def _count_bonuses(
    conn: sqlite3.Connection, season: int, cfg: ScoringConfig, through: str | None,
    since: str | None = None,
) -> dict[str, dict[str, float]]:
    from ..scoring import is_cycle, is_no_hitter, is_perfect_game, is_quality_start

    clause, params = "", [season]
    if since:
        clause += " AND date >= ?"
        params.append(since)
    if through:
        clause += " AND date <= ?"
        params.append(through)

    out: dict[str, dict[str, float]] = {}

    def add(pid: str, key: str, value: float) -> None:
        entry = out.setdefault(pid, {"points": 0.0})
        entry[key] = entry.get(key, 0.0) + value
        entry["points"] += value

    slam_pts = cfg.batting.get("SLAM", 0)
    cyc_pts = cfg.batting.get("CYC", 0)
    rows = conn.execute(
        f"SELECT player_id, b1, b2, b3, hr, slam FROM batting_lines "
        f"WHERE season = ? {clause} AND (slam > 0 OR (b1 > 0 AND b2 > 0 AND b3 > 0 AND hr > 0))",
        params,
    )
    for r in rows:
        if r["slam"]:
            add(r["player_id"], "SLAM", r["slam"] * slam_pts)
        if is_cycle(dict(r)):
            add(r["player_id"], "CYC", cyc_pts)

    rows = conn.execute(
        f"SELECT player_id, gs, outs, er, cg, h, bb, hbp, bf, errors_allowed "
        f"FROM pitching_lines WHERE season = ? {clause} AND (gs > 0 OR cg > 0)",
        params,
    )
    for r in rows:
        line = dict(r)
        if is_quality_start(line, cfg):
            add(r["player_id"], "QS", cfg.pitching.get("QS", 0))
        if is_perfect_game(line):
            add(r["player_id"], "NH", cfg.pitching.get("NH", 0))
            add(r["player_id"], "PG", cfg.pitching.get("PG", 0))
        elif is_no_hitter(line):
            add(r["player_id"], "NH", cfg.pitching.get("NH", 0))
    return out


# Aggregating a season is a full scan of ~70k rows. The inputs are immutable
# (cached historical data + a scoring config that changes rarely), so results
# are memoised; the draft room would otherwise re-rank the season on every pick.
_TOTALS_CACHE: "OrderedDict[tuple, dict[str, dict[str, Any]]]" = OrderedDict()
_CACHE_LIMIT = 12


def _cached(
    conn: sqlite3.Connection, season: int, cfg: ScoringConfig,
    through: str | None, since: str | None,
) -> dict[str, dict[str, Any]]:
    key = (season, json.dumps(cfg.to_dict(), sort_keys=True), through, since)
    hit = _TOTALS_CACHE.get(key)
    if hit is not None:
        _TOTALS_CACHE.move_to_end(key)
        return hit
    value = _score_totals(conn, season, cfg, through=through, since=since)
    _TOTALS_CACHE[key] = value
    while len(_TOTALS_CACHE) > _CACHE_LIMIT:
        _TOTALS_CACHE.popitem(last=False)
    return value


def invalidate_cache() -> None:
    _TOTALS_CACHE.clear()


def season_totals(
    conn: sqlite3.Connection, season: int, cfg: ScoringConfig
) -> dict[str, dict[str, Any]]:
    """FULL-SEASON totals. Draft rankings only — never in-season UI or bots."""
    return _cached(conn, season, cfg, through=None, since=None)


def stats_through(
    conn: sqlite3.Connection, season: int, cfg: ScoringConfig, through: str,
    since: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Totals through ``through`` (inclusive). The hindsight-safe aggregate."""
    return _cached(conn, season, cfg, through=through, since=since)


def draft_rankings(
    conn: sqlite3.Connection, season: int, cfg: ScoringConfig
) -> list[dict[str, Any]]:
    """Player pool ordered by full-season fantasy points under the live config."""
    totals = season_totals(conn, season, cfg)
    pool = list_players(conn, season)
    ranked = []
    for p in pool:
        t = totals.get(p["player_id"])
        ranked.append({
            **p,
            "points": t["points"] if t else 0.0,
            "games": (t["batting"]["g"] if t and t["batting"] else 0)
                     or (t["pitching"]["g"] if t and t["pitching"] else 0),
        })
    ranked.sort(key=lambda r: (-r["points"], r["name"]))
    for i, r in enumerate(ranked, start=1):
        r["rank"] = i
    return ranked


def daily_lines(
    conn: sqlite3.Connection, season: int, player_id: str, start: str, end: str
) -> list[dict[str, Any]]:
    bat = {r["date"]: dict(r) for r in conn.execute(
        "SELECT * FROM batting_lines WHERE season=? AND player_id=? AND date BETWEEN ? AND ?",
        (season, player_id, start, end))}
    pit = {r["date"]: dict(r) for r in conn.execute(
        "SELECT * FROM pitching_lines WHERE season=? AND player_id=? AND date BETWEEN ? AND ?",
        (season, player_id, start, end))}
    days = sorted(set(bat) | set(pit))
    return [{"date": d, "batting": bat.get(d), "pitching": pit.get(d)} for d in days]


def score_player_day(
    batting: dict | None, pitching: dict | None, cfg: ScoringConfig
) -> ScoreLine:
    from ..scoring import score_day
    return score_day(batting, pitching, cfg)


def slot_fill_order(active_slots: dict[str, int]) -> list[str]:
    """Most constrained slots first, so auto-fill doesn't strand a catcher."""
    priority = {"C": 0, "SS": 1, "2B": 2, "3B": 3, "1B": 4, "OF": 5,
                "SP": 6, "RP": 7, "P": 8, "UTIL": 9}
    return sorted(active_slots, key=lambda s: (priority.get(s, 5), s))


def is_batter_slot(slot: str) -> bool:
    return slot == "UTIL" or slot in BATTER_POSITIONS


def is_pitcher_slot(slot: str) -> bool:
    return slot == "P" or slot in PITCHER_POSITIONS
