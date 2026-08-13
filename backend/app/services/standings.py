"""Standings, playoff seeding and the bracket.

Standings rank on win/loss record with total points as the tiebreaker.  The
top ``playoff_teams`` seeds (8 by default) enter a bye-free single-elimination
bracket; the final is a two-week series decided on combined points.

Note on design intent, which the spec asked to have confirmed: exactly
``playoff_teams`` qualify regardless of league size.  In an 8-team league that
means nobody is eliminated in the regular season and the bracket is the whole
league; in a 14-team league six teams miss out.  The bracket size is a config
value, so a commissioner who wants a 4-team playoff in an 8-team league can set
one without touching this module.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..config import LeagueConfig
from . import leagues, schedule as schedule_svc


def table(conn: sqlite3.Connection, league: dict[str, Any]) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT id, name, is_bot, wins, losses, ties, points_for, faab_remaining, eliminated
             FROM teams WHERE league_id = ?""",
        (league["id"],),
    ).fetchall()
    teams = [dict(r) for r in rows]
    points_against = _points_against(conn, league["id"])
    for t in teams:
        t["points_for"] = round(t["points_for"], 2)
        t["points_against"] = round(points_against.get(t["id"], 0.0), 2)
        played = t["wins"] + t["losses"] + t["ties"]
        t["win_pct"] = round((t["wins"] + 0.5 * t["ties"]) / played, 3) if played else 0.0
    teams.sort(key=lambda t: (-t["win_pct"], -t["points_for"], t["name"]))
    for i, t in enumerate(teams, start=1):
        t["rank"] = i
    return teams


def _points_against(conn: sqlite3.Connection, league_id: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for r in conn.execute(
        "SELECT home_team_id, away_team_id, home_points, away_points FROM matchups "
        "WHERE league_id = ? AND complete = 1",
        (league_id,),
    ):
        if r["home_team_id"]:
            out[r["home_team_id"]] = out.get(r["home_team_id"], 0.0) + (r["away_points"] or 0)
        if r["away_team_id"]:
            out[r["away_team_id"]] = out.get(r["away_team_id"], 0.0) + (r["home_points"] or 0)
    return out


def seeds(conn: sqlite3.Connection, league: dict[str, Any], cfg: LeagueConfig) -> list[dict[str, Any]]:
    return table(conn, league)[: cfg.playoff_teams]


def create_regular_season(
    conn: sqlite3.Connection, league: dict[str, Any], cfg: LeagueConfig
) -> int:
    team_ids = [t["id"] for t in leagues.teams(conn, league["id"])]
    schedule_svc.validate(cfg, team_ids)
    weeks = schedule_svc.regular_season(team_ids, cfg.regular_season_weeks)
    conn.execute("DELETE FROM matchups WHERE league_id = ? AND stage = 'regular'", (league["id"],))
    rows = []
    for week_index, pairs in enumerate(weeks, start=1):
        for slot, (home, away) in enumerate(pairs):
            rows.append((league["id"], week_index, slot, home, away, "regular"))
    conn.executemany(
        "INSERT INTO matchups (league_id, week, slot, home_team_id, away_team_id, stage) "
        "VALUES (?,?,?,?,?,?)",
        rows,
    )
    return len(rows)


def build_playoff_round(
    conn: sqlite3.Connection, league: dict[str, Any], cfg: LeagueConfig, round_index: int
) -> list[dict[str, Any]]:
    """Create the matchups for playoff round ``round_index`` (1-based)."""
    plan = schedule_svc.playoff_week_plan(cfg)
    if round_index > len(plan):
        return []
    stage = plan[round_index - 1]
    weeks = stage["weeks"]

    if round_index == 1:
        qualifiers = [t["id"] for t in seeds(conn, league, cfg)]
        pairs = schedule_svc.bracket_pairings(qualifiers)
        _eliminate_non_qualifiers(conn, league["id"], qualifiers)
    else:
        prev = plan[round_index - 2]
        winners = _round_winners(conn, league["id"], prev)
        if len(winners) < 2:
            return []
        pairs = [(winners[i], winners[i + 1]) for i in range(0, len(winners) - 1, 2)]

    rows = []
    for slot, (home, away) in enumerate(pairs):
        series = f"{stage['stage']}-{slot}"
        for week in weeks:
            rows.append((league["id"], week, slot, home, away, stage["stage"], series))
    conn.executemany(
        "INSERT OR REPLACE INTO matchups (league_id, week, slot, home_team_id, away_team_id, "
        "stage, series_id) VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    return [{"stage": stage["stage"], "weeks": weeks, "pairs": pairs}]


def _eliminate_non_qualifiers(conn: sqlite3.Connection, league_id: str, qualifiers: list[str]) -> None:
    conn.execute("UPDATE teams SET eliminated = 1 WHERE league_id = ?", (league_id,))
    if qualifiers:
        placeholders = ",".join("?" * len(qualifiers))
        conn.execute(
            f"UPDATE teams SET eliminated = 0 WHERE league_id = ? AND id IN ({placeholders})",
            [league_id, *qualifiers],
        )


def _round_winners(conn: sqlite3.Connection, league_id: str, stage: dict[str, Any]) -> list[str]:
    """Winners of a completed round, kept in bracket order."""
    rows = conn.execute(
        "SELECT slot, winner_team_id FROM matchups WHERE league_id=? AND stage=? "
        "AND winner_team_id IS NOT NULL GROUP BY slot ORDER BY slot",
        (league_id, stage["stage"]),
    ).fetchall()
    return [r["winner_team_id"] for r in rows]


def bracket(conn: sqlite3.Connection, league: dict[str, Any], cfg: LeagueConfig) -> dict[str, Any]:
    plan = schedule_svc.playoff_week_plan(cfg)
    names = {t["id"]: t["name"] for t in leagues.teams(conn, league["id"])}
    seeding = {t["id"]: t["rank"] for t in table(conn, league)}
    out = []
    for stage in plan:
        rows = conn.execute(
            "SELECT * FROM matchups WHERE league_id=? AND stage=? ORDER BY slot, week",
            (league["id"], stage["stage"]),
        ).fetchall()
        series: dict[str, dict[str, Any]] = {}
        for r in rows:
            key = r["series_id"] or f"{r['stage']}-{r['slot']}"
            entry = series.setdefault(key, {
                "slot": r["slot"], "stage": r["stage"], "weeks": [],
                "home": {"id": r["home_team_id"], "name": names.get(r["home_team_id"]),
                         "seed": seeding.get(r["home_team_id"]), "points": 0.0},
                "away": {"id": r["away_team_id"], "name": names.get(r["away_team_id"]),
                         "seed": seeding.get(r["away_team_id"]), "points": 0.0},
                "complete": True, "winner": None,
            })
            entry["weeks"].append(r["week"])
            entry["home"]["points"] = round(entry["home"]["points"] + (r["home_points"] or 0), 2)
            entry["away"]["points"] = round(entry["away"]["points"] + (r["away_points"] or 0), 2)
            entry["complete"] = entry["complete"] and bool(r["complete"])
        for entry in series.values():
            if entry["complete"] and entry["home"]["id"]:
                entry["winner"] = (entry["home"]["id"]
                                   if entry["home"]["points"] >= entry["away"]["points"]
                                   else entry["away"]["id"])
        out.append({"stage": stage["stage"], "round": stage["round"],
                    "weeks": stage["weeks"], "series": list(series.values())})
    return {"rounds": out, "playoff_teams": cfg.playoff_teams,
            "seeds": [{"rank": t["rank"], "id": t["id"], "name": t["name"],
                       "record": f"{t['wins']}-{t['losses']}", "points_for": t["points_for"]}
                      for t in seeds(conn, league, cfg)]}


def champion(conn: sqlite3.Connection, league: dict[str, Any], cfg: LeagueConfig) -> dict[str, Any] | None:
    data = bracket(conn, league, cfg)
    final = next((r for r in data["rounds"] if r["stage"] == "final"), None)
    if not final or not final["series"]:
        return None
    series = final["series"][0]
    if not series["complete"] or not series["winner"]:
        return None
    side = series["home"] if series["winner"] == series["home"]["id"] else series["away"]
    other = series["away"] if side is series["home"] else series["home"]
    return {"team_id": side["id"], "name": side["name"],
            "points": side["points"], "runner_up": other["name"],
            "runner_up_points": other["points"]}
