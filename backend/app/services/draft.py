"""Snake draft.

Draft order comes from the mini-game; round 1 runs 1..N, round 2 runs N..1, and
so on for ``roster_size`` rounds.  The draft board is materialised up front
(one row per overall pick with the team that owns it) so "whose turn is it" is
a lookup rather than a computation, which keeps the live room simple.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Any

from ..scoring import ScoringConfig
from . import leagues, players as players_svc, rosters


class DraftError(RuntimeError):
    pass


def snake_order(team_ids_by_slot: list[str], rounds: int) -> list[str]:
    order: list[str] = []
    for r in range(rounds):
        order.extend(team_ids_by_slot if r % 2 == 0 else list(reversed(team_ids_by_slot)))
    return order


def initialize(conn: sqlite3.Connection, league: dict[str, Any]) -> int:
    cfg = leagues.league_config(league)
    roster = leagues.teams(conn, league["id"])
    if any(t["draft_slot"] is None for t in roster):
        raise DraftError("draft order has not been set — run the mini-game first")
    by_slot = [t["id"] for t in sorted(roster, key=lambda t: t["draft_slot"])]
    order = snake_order(by_slot, cfg.roster_size)

    conn.execute("DELETE FROM draft_picks WHERE league_id = ?", (league["id"],))
    rows = []
    n = len(by_slot)
    for i, team_id in enumerate(order):
        rows.append((league["id"], i + 1, i // n + 1, i % n + 1, team_id))
    conn.executemany(
        "INSERT INTO draft_picks (league_id, overall, round, pick_in_round, team_id) "
        "VALUES (?,?,?,?,?)",
        rows,
    )
    return len(rows)


def current_pick(conn: sqlite3.Connection, league_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM draft_picks WHERE league_id = ? AND player_id IS NULL "
        "ORDER BY overall LIMIT 1",
        (league_id,),
    ).fetchone()
    return dict(row) if row else None


def drafted_ids(conn: sqlite3.Connection, league_id: str) -> set[str]:
    return {
        r["player_id"] for r in conn.execute(
            "SELECT player_id FROM rosters WHERE league_id = ?", (league_id,)
        )
    }


def team_roster(
    conn: sqlite3.Connection, league: dict[str, Any], team_id: str
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT p.player_id, p.name, p.mlb_team, p.positions, p.is_pitcher,
                  r.acquired_week, r.acquired_via
             FROM rosters r JOIN players p
               ON p.player_id = r.player_id AND p.season = ?
            WHERE r.league_id = ? AND r.team_id = ?""",
        (league["season_year"], league["id"], team_id),
    ).fetchall()
    return [dict(r) for r in rows]


def available(
    conn: sqlite3.Connection,
    league: dict[str, Any],
    cfg_scoring: ScoringConfig,
    limit: int = 200,
    search: str | None = None,
    position: str | None = None,
) -> list[dict[str, Any]]:
    """Best available, ranked by full-season production.

    Full-season stats are fine *here*: the draft happens before the replay
    starts and every manager sees the same finished season.  In-season views
    must use :func:`players.stats_through` instead.
    """
    taken = drafted_ids(conn, league["id"])
    ranked = players_svc.draft_rankings(conn, league["season_year"], cfg_scoring)
    out = []
    for p in ranked:
        if p["player_id"] in taken:
            continue
        if position:
            allowed = set(p["positions"].split(",")) | {"P" if p["is_pitcher"] else "UTIL"}
            if position not in allowed:
                continue
        if search and search.lower() not in p["name"].lower():
            continue
        out.append(p)
        if len(out) >= limit:
            break
    return out


def make_pick(
    conn: sqlite3.Connection,
    league: dict[str, Any],
    team_id: str,
    player_id: str,
    auto: bool = False,
) -> dict[str, Any]:
    cfg = leagues.league_config(league)
    pick = current_pick(conn, league["id"])
    if pick is None:
        raise DraftError("the draft is complete")
    if pick["team_id"] != team_id:
        raise DraftError("it is not your pick")

    player = players_svc.get_player(conn, league["season_year"], player_id)
    if player is None:
        raise DraftError(f"no player {player_id!r} in the {league['season_year']} pool")
    if player_id in drafted_ids(conn, league["id"]):
        raise DraftError(f"{player['name']} is already drafted")

    roster = team_roster(conn, league, team_id)
    if len(roster) >= cfg.roster_size:
        raise DraftError("roster is full")
    picks_left_after = cfg.roster_size - len(roster) - 1
    ok, why = rosters.draft_feasible(roster, player, cfg.active_slots, picks_left_after)
    if not ok:
        raise DraftError(f"cannot draft {player['name']}: {why}")

    now = dt.datetime.utcnow().isoformat(timespec="seconds")
    conn.execute(
        "UPDATE draft_picks SET player_id = ?, auto = ?, picked_at = ? "
        "WHERE league_id = ? AND overall = ?",
        (player_id, 1 if auto else 0, now, league["id"], pick["overall"]),
    )
    conn.execute(
        "INSERT INTO rosters (league_id, team_id, player_id, acquired_week, acquired_via) "
        "VALUES (?,?,?,0,'draft')",
        (league["id"], team_id, player_id),
    )
    leagues.log(conn, league["id"], 0, "draft", team_id, player_id,
                f"round {pick['round']} pick {pick['pick_in_round']}")
    return {
        "overall": pick["overall"], "round": pick["round"],
        "pick_in_round": pick["pick_in_round"], "team_id": team_id,
        "player": player, "auto": auto,
    }


def board(conn: sqlite3.Connection, league: dict[str, Any], limit: int = 40) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT d.overall, d.round, d.pick_in_round, d.team_id, d.player_id, d.auto,
                  t.name AS team_name, p.name AS player_name, p.positions, p.mlb_team
             FROM draft_picks d
             JOIN teams t ON t.id = d.team_id
             LEFT JOIN players p ON p.player_id = d.player_id AND p.season = ?
            WHERE d.league_id = ? AND d.player_id IS NOT NULL
            ORDER BY d.overall DESC LIMIT ?""",
        (league["season_year"], league["id"], limit),
    ).fetchall()
    return [dict(r) for r in rows]


def upcoming(conn: sqlite3.Connection, league_id: str, count: int = 8) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT d.overall, d.round, d.pick_in_round, d.team_id, t.name AS team_name
             FROM draft_picks d JOIN teams t ON t.id = d.team_id
            WHERE d.league_id = ? AND d.player_id IS NULL
            ORDER BY d.overall LIMIT ?""",
        (league_id, count),
    ).fetchall()
    return [dict(r) for r in rows]


def progress(conn: sqlite3.Connection, league_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT COUNT(*) total, SUM(player_id IS NOT NULL) made "
        "FROM draft_picks WHERE league_id = ?",
        (league_id,),
    ).fetchone()
    total, made = row["total"] or 0, row["made"] or 0
    return {"total": total, "made": made, "complete": bool(total) and made >= total}


def state(conn: sqlite3.Connection, league: dict[str, Any]) -> dict[str, Any]:
    pick = current_pick(conn, league["id"])
    prog = progress(conn, league["id"])
    on_clock = None
    if pick:
        team = leagues.get_team(conn, league["id"], pick["team_id"])
        on_clock = {
            "team_id": pick["team_id"],
            "team_name": team["name"] if team else "?",
            "is_bot": bool(team["is_bot"]) if team else False,
            "overall": pick["overall"], "round": pick["round"],
            "pick_in_round": pick["pick_in_round"],
        }
    return {
        "type": "draft_state",
        "phase": league["phase"],
        "season_year": league["season_year"],
        "on_clock": on_clock,
        "progress": prog,
        "recent": board(conn, league, limit=12),
        "upcoming": upcoming(conn, league["id"]),
    }
