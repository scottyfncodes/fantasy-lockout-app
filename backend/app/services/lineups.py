"""Weekly lineups: validation, locking and auto-fill.

Managers set 23 active slots each week; everything else sits on the bench or in
an IL slot.  Lineups lock at the start of the fantasy week (Monday 00:00 in the
replay calendar, i.e. the Sunday-night deadline) and cannot be edited for a
week that has already begun.

Auto-fill is used for bots, for managers who miss the deadline, and as the
starting point when a new week opens.  It is deliberately **hindsight-blind**:
it ranks players on production through the last simulated date only.  See
``bots.py`` for why that rule exists.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Any

from ..config import LeagueConfig
from . import il, leagues, players as players_svc, rosters, timeline

BENCH = "BENCH"
IL_SLOT = "IL"


class LineupError(RuntimeError):
    pass


def roster_players(
    conn: sqlite3.Connection, league: dict[str, Any], team_id: str
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT p.player_id, p.name, p.mlb_team, p.positions, p.is_pitcher,
                  r.acquired_week, r.acquired_via
             FROM rosters r JOIN players p
               ON p.player_id = r.player_id AND p.season = ?
            WHERE r.league_id = ? AND r.team_id = ?
            ORDER BY p.is_pitcher, p.name""",
        (league["season_year"], league["id"], team_id),
    ).fetchall()
    return [dict(r) for r in rows]


def is_locked(conn: sqlite3.Connection, league_id: str, team_id: str, week: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM lineup_locks WHERE league_id=? AND team_id=? AND week=?",
        (league_id, team_id, week),
    ).fetchone() is not None


def stored_lineup(
    conn: sqlite3.Connection, league_id: str, team_id: str, week: int
) -> dict[str, str]:
    rows = conn.execute(
        "SELECT player_id, slot FROM lineups WHERE league_id=? AND team_id=? AND week=?",
        (league_id, team_id, week),
    ).fetchall()
    return {r["player_id"]: r["slot"] for r in rows}


def validate(
    conn: sqlite3.Connection,
    league: dict[str, Any],
    cfg: LeagueConfig,
    team_id: str,
    week: int,
    assignment: dict[str, str],
) -> dict[str, Any]:
    """Check a proposed lineup. Raises LineupError with a specific reason."""
    roster = {p["player_id"]: p for p in roster_players(conn, league, team_id)}
    unknown = set(assignment) - set(roster)
    if unknown:
        raise LineupError(f"not on your roster: {', '.join(sorted(unknown))}")
    missing = set(roster) - set(assignment)
    if missing:
        raise LineupError(f"every rostered player needs a slot; missing {len(missing)}")

    week_start = timeline.week(conn, league["season_year"], cfg, week).start.isoformat()
    injured = il.il_status(conn, league["season_year"], roster.keys(), week_start)

    counts: dict[str, int] = {}
    for player_id, slot in assignment.items():
        counts[slot] = counts.get(slot, 0) + 1
        player = roster[player_id]
        if slot == BENCH:
            continue
        if slot == IL_SLOT:
            if player_id not in injured:
                raise LineupError(
                    f"{player['name']} was not on the injured list on {week_start}; "
                    "IL slots are only for players the historical record has out"
                )
            continue
        if slot not in cfg.active_slots:
            raise LineupError(f"unknown slot {slot!r}")
        if player_id in injured:
            stint = injured[player_id]
            raise LineupError(
                f"{player['name']} was on the {stint['kind']} on {week_start} "
                f"({stint['note']}) — bench him or use an IL slot"
            )
        if slot not in players_svc.eligible_slots(player, cfg.active_slots.keys()):
            raise LineupError(f"{player['name']} ({player['positions']}) is not eligible at {slot}")

    for slot, limit in cfg.active_slots.items():
        if counts.get(slot, 0) > limit:
            raise LineupError(f"too many players in {slot}: {counts[slot]} of {limit}")
    if counts.get(IL_SLOT, 0) > cfg.il_size:
        raise LineupError(f"too many IL slots used: {counts[IL_SLOT]} of {cfg.il_size}")
    # The full roster is drafted, but only some of it is hurt in any given week,
    # so an IL slot with nobody to put in it holds a healthy player instead.
    # Without this the bench would overflow every week fewer than `il_size`
    # players were on the historical IL.
    bench_capacity = cfg.bench_size + (cfg.il_size - counts.get(IL_SLOT, 0))
    if counts.get(BENCH, 0) > bench_capacity:
        raise LineupError(
            f"bench holds {bench_capacity} this week "
            f"({cfg.bench_size} bench + {cfg.il_size - counts.get(IL_SLOT, 0)} unused IL); "
            f"you have {counts[BENCH]}"
        )

    active_filled = sum(counts.get(s, 0) for s in cfg.active_slots)
    startable = [p for pid, p in roster.items() if pid not in injured]
    fillable = len(rosters.expand_slots(cfg.active_slots)) - len(
        rosters.unfilled_slots(startable, cfg.active_slots)
    )
    return {
        "active_filled": active_filled,
        "active_size": cfg.active_size,
        "max_fillable": fillable,
        "empty_slots": [
            s for s, limit in cfg.active_slots.items()
            for _ in range(limit - counts.get(s, 0))
        ],
        "injured": {pid: injured[pid] for pid in injured},
    }


def save(
    conn: sqlite3.Connection,
    league: dict[str, Any],
    cfg: LeagueConfig,
    team_id: str,
    week: int,
    assignment: dict[str, str],
) -> dict[str, Any]:
    if week < (league["current_week"] or 1):
        raise LineupError(f"week {week} is in the past")
    if is_locked(conn, league["id"], team_id, week):
        raise LineupError(f"week {week} is locked — the deadline has passed")
    summary = validate(conn, league, cfg, team_id, week, assignment)
    conn.execute(
        "DELETE FROM lineups WHERE league_id=? AND team_id=? AND week=?",
        (league["id"], team_id, week),
    )
    conn.executemany(
        "INSERT INTO lineups (league_id, team_id, week, player_id, slot) VALUES (?,?,?,?,?)",
        [(league["id"], team_id, week, pid, slot) for pid, slot in assignment.items()],
    )
    return summary


def autofill(
    conn: sqlite3.Connection,
    league: dict[str, Any],
    team_id: str,
    week: int,
    cfg: LeagueConfig,
    keep_existing: bool = False,
) -> dict[str, str]:
    """Build a legal lineup from what is known *before* the week starts."""
    roster = roster_players(conn, league, team_id)
    if not roster:
        return {}
    week_start = timeline.week(conn, league["season_year"], cfg, week).start
    injured = il.il_status(conn, league["season_year"], [p["player_id"] for p in roster],
                           week_start.isoformat())
    ranking = pre_week_ranking(conn, league, cfg, week)

    healthy = [p for p in roster if p["player_id"] not in injured]
    healthy.sort(key=lambda p: -ranking.get(p["player_id"], 0.0))

    forced: dict[str, str] = {}
    if keep_existing:
        existing = stored_lineup(conn, league["id"], team_id, week)
        for pid, slot in existing.items():
            if slot in cfg.active_slots and pid not in injured:
                forced[pid] = slot

    assignment: dict[str, str] = {}
    slots = rosters.expand_slots(cfg.active_slots)
    try:
        matched = rosters.max_matching(healthy, cfg.active_slots, forced=forced or None)
    except ValueError:
        matched = rosters.max_matching(healthy, cfg.active_slots)
    for idx, pid in matched.items():
        assignment[pid] = slots[idx]

    il_used = 0
    for p in roster:
        pid = p["player_id"]
        if pid in assignment:
            continue
        if pid in injured and il_used < cfg.il_size:
            assignment[pid] = IL_SLOT
            il_used += 1
        else:
            assignment[pid] = BENCH

    conn.execute(
        "DELETE FROM lineups WHERE league_id=? AND team_id=? AND week=?",
        (league["id"], team_id, week),
    )
    conn.executemany(
        "INSERT INTO lineups (league_id, team_id, week, player_id, slot) VALUES (?,?,?,?,?)",
        [(league["id"], team_id, week, pid, slot) for pid, slot in assignment.items()],
    )
    return assignment


def pre_week_ranking(
    conn: sqlite3.Connection, league: dict[str, Any], cfg: LeagueConfig, week: int
) -> dict[str, float]:
    """Per-game production through the day before ``week`` starts.

    This is the *only* stat view auto-fill and bots are allowed. Rate rather
    than total, so a player who missed time is not punished for it, blended
    toward the mean for tiny samples.
    """
    scoring = leagues.league_scoring(league)
    start = timeline.week(conn, league["season_year"], cfg, week).start
    cutoff = (start - dt.timedelta(days=1)).isoformat()
    since, _ = timeline.replay_window(conn, league, cfg)
    totals = players_svc.stats_through(conn, league["season_year"], scoring, cutoff, since=since)

    if not totals:  # week 1: nothing has been played yet
        return {}
    scores: dict[str, float] = {}
    for pid, entry in totals.items():
        games = 0
        if entry["batting"]:
            games = max(games, entry["batting"]["g"])
        if entry["pitching"]:
            games = max(games, entry["pitching"]["g"])
        if games <= 0:
            continue
        per_game = entry["points"] / games
        # Shrink small samples toward zero so one hot game doesn't outrank a
        # season of steady work.
        weight = games / (games + 8.0)
        scores[pid] = per_game * weight + 0.02 * entry["points"]
    return scores


def draft_order_ranking(
    conn: sqlite3.Connection, league: dict[str, Any], cfg: LeagueConfig
) -> dict[str, float]:
    """Full-season ranking. Draft only — never during the season."""
    scoring = leagues.league_scoring(league)
    return {
        pid: entry["points"]
        for pid, entry in players_svc.season_totals(conn, league["season_year"], scoring).items()
    }


def lock_week(
    conn: sqlite3.Connection, league: dict[str, Any], cfg: LeagueConfig, week: int
) -> list[str]:
    """Close submissions for ``week``, auto-filling anyone who didn't submit."""
    locked: list[str] = []
    now = dt.datetime.utcnow().isoformat(timespec="seconds")
    for team in leagues.teams(conn, league["id"]):
        if is_locked(conn, league["id"], team["id"], week):
            continue
        existing = stored_lineup(conn, league["id"], team["id"], week)
        needs_fill = not existing
        if not needs_fill:
            try:
                validate(conn, league, cfg, team["id"], week, existing)
            except LineupError:
                needs_fill = True  # roster changed under a stale lineup
        if needs_fill:
            autofill(conn, league, team["id"], week, cfg, keep_existing=bool(existing))
        conn.execute(
            "INSERT OR REPLACE INTO lineup_locks (league_id, team_id, week, locked_at) "
            "VALUES (?,?,?,?)",
            (league["id"], team["id"], week, now),
        )
        locked.append(team["id"])
    return locked


def view(
    conn: sqlite3.Connection,
    league: dict[str, Any],
    cfg: LeagueConfig,
    team_id: str,
    week: int,
) -> dict[str, Any]:
    roster = roster_players(conn, league, team_id)
    assignment = stored_lineup(conn, league["id"], team_id, week)
    if not assignment:
        assignment = autofill(conn, league, team_id, week, cfg)
    week_obj = timeline.week(conn, league["season_year"], cfg, week)
    injured = il.il_status(
        conn, league["season_year"], [p["player_id"] for p in roster], week_obj.start.isoformat()
    )
    since, as_of = timeline.replay_window(conn, league, cfg)
    scoring = leagues.league_scoring(league)
    totals = players_svc.stats_through(conn, league["season_year"], scoring, as_of, since=since)

    entries = []
    for p in roster:
        pid = p["player_id"]
        t = totals.get(pid)
        entries.append({
            **p,
            "slot": assignment.get(pid, BENCH),
            "eligible_slots": players_svc.eligible_slots(p, cfg.active_slots.keys()),
            "il": injured.get(pid),
            "points_to_date": t["points"] if t else 0.0,
            "games_to_date": (t["batting"]["g"] if t and t["batting"] else 0)
                             or (t["pitching"]["g"] if t and t["pitching"] else 0),
        })
    return {
        "week": week,
        "label": week_obj.label,
        "week_start": week_obj.start.isoformat(),
        "week_end": week_obj.end.isoformat(),
        "locked": is_locked(conn, league["id"], team_id, week),
        "active_slots": cfg.active_slots,
        "bench_size": cfg.bench_size,
        "il_size": cfg.il_size,
        "stats_through": as_of,
        "players": entries,
    }
