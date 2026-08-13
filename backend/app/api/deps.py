"""Shared request plumbing: database handles and the light identity model.

There is no account system.  A manager is whoever holds a team's token, and the
commissioner is whoever holds the league's commissioner token.  Tokens are
opaque, generated server-side, and kept in the browser's local storage.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterator

from fastapi import Depends, Header, HTTPException, Path

from .. import db
from ..config import LeagueConfig
from ..services import leagues as leagues_svc


def get_conn() -> Iterator[sqlite3.Connection]:
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def get_league(
    code: str = Path(..., description="league id or join code"),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    league = leagues_svc.get_league(conn, code)
    if league is None:
        raise HTTPException(404, f"no league {code!r}")
    return league


def get_config(league: dict[str, Any] = Depends(get_league)) -> LeagueConfig:
    return leagues_svc.league_config(league)


def require_commissioner(
    league: dict[str, Any] = Depends(get_league),
    x_commissioner_token: str | None = Header(None),
) -> dict[str, Any]:
    if x_commissioner_token != league["commissioner_token"]:
        raise HTTPException(403, "commissioner token required")
    return league


def current_team(
    league: dict[str, Any] = Depends(get_league),
    conn: sqlite3.Connection = Depends(get_conn),
    x_manager_token: str | None = Header(None),
) -> dict[str, Any]:
    if not x_manager_token:
        raise HTTPException(401, "missing manager token — join the league first")
    team = leagues_svc.team_for_token(conn, league["id"], x_manager_token)
    if team is None:
        raise HTTPException(403, "unknown manager token for this league")
    return team


def team_or_commissioner(
    team_id: str,
    league: dict[str, Any] = Depends(get_league),
    conn: sqlite3.Connection = Depends(get_conn),
    x_manager_token: str | None = Header(None),
    x_commissioner_token: str | None = Header(None),
) -> dict[str, Any]:
    """Managers act on their own team; the commissioner can act on any team."""
    team = leagues_svc.get_team(conn, league["id"], team_id)
    if team is None:
        raise HTTPException(404, "unknown team")
    if x_commissioner_token == league["commissioner_token"]:
        return team
    if x_manager_token and team["manager_token"] == x_manager_token:
        return team
    raise HTTPException(403, "you do not manage this team")
