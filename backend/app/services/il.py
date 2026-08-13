"""Injured list engine.

Real IL stints from the replayed season are applied to drafted players: if the
historical record says a player was on the IL, no manager gets to start him.
There is no override — you cannot cheat the past.

When is a player "on the IL" for a fantasy week?
-----------------------------------------------
His status **on the Monday the week begins**, which is the moment the lineup
locks.  A player already on the IL at lock time is unstartable that week and
may be stashed in an IL slot; a player who gets hurt mid-week stays in the
active lineup and simply stops producing, exactly as he would in a real weekly
league.  Judging by the whole week instead would mean the lineup screen knew
about an injury that had not happened yet — the same hindsight leak the waiver
rules exist to prevent.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Any, Iterable


def stints_for(
    conn: sqlite3.Connection, season: int, player_ids: Iterable[str] | None = None
) -> dict[str, list[dict[str, Any]]]:
    sql = "SELECT player_id, start_date, end_date, kind, note FROM il_stints WHERE season = ?"
    params: list[Any] = [season]
    ids = list(player_ids) if player_ids is not None else None
    if ids:
        sql += f" AND player_id IN ({','.join('?' * len(ids))})"
        params.extend(ids)
    out: dict[str, list[dict[str, Any]]] = {}
    for row in conn.execute(sql, params):
        out.setdefault(row["player_id"], []).append(dict(row))
    return out


def on_il(stints: list[dict[str, Any]], day: str) -> dict[str, Any] | None:
    """The stint covering ``day``, if any. ``end_date`` NULL means season-ending."""
    for s in stints:
        if s["start_date"] > day:
            continue
        if s["end_date"] is None or day < s["end_date"]:
            return s
    return None


def il_status(
    conn: sqlite3.Connection, season: int, player_ids: Iterable[str], as_of: str
) -> dict[str, dict[str, Any]]:
    """``{player_id: stint}`` for every listed player on the IL on ``as_of``."""
    all_stints = stints_for(conn, season, player_ids)
    out: dict[str, dict[str, Any]] = {}
    for pid, stints in all_stints.items():
        hit = on_il(stints, as_of)
        if hit:
            out[pid] = hit
    return out


def upcoming_returns(
    conn: sqlite3.Connection, season: int, player_ids: Iterable[str], as_of: str
) -> dict[str, str]:
    """Known return dates for currently-injured players.

    Only reports the end of a stint that has already started — the historical
    record of a stint in progress, not a forecast of a future injury.
    """
    out: dict[str, str] = {}
    for pid, stint in il_status(conn, season, player_ids, as_of).items():
        if stint["end_date"]:
            out[pid] = stint["end_date"]
    return out


def player_il_log(conn: sqlite3.Connection, season: int, player_id: str,
                  through: str | None = None) -> list[dict[str, Any]]:
    """IL history for a player page — truncated at the replay's current date.

    Showing stints that have not started yet would tell a manager which players
    are about to get hurt.
    """
    rows = conn.execute(
        "SELECT start_date, end_date, kind, note FROM il_stints "
        "WHERE season = ? AND player_id = ? ORDER BY start_date",
        (season, player_id),
    ).fetchall()
    log = [dict(r) for r in rows]
    if through is None:
        return log
    visible = []
    for s in log:
        if s["start_date"] > through:
            continue
        if s["end_date"] and s["end_date"] > through:
            s = {**s, "end_date": None, "note": s["note"] + " (still out)"}
        visible.append(s)
    return visible


def days_missed(stint: dict[str, Any], season_end: dt.date) -> int:
    start = dt.date.fromisoformat(stint["start_date"])
    end = dt.date.fromisoformat(stint["end_date"]) if stint["end_date"] else season_end
    return max(0, (end - start).days)
