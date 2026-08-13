"""League lifecycle: lobby -> year reveal -> mini-game -> draft -> season.

Identity is deliberately light — a league has a short join code, each manager
gets an opaque token stored in their browser, and the commissioner gets one
extra token.  That is enough for a friend league and avoids an auth system.
"""

from __future__ import annotations

import datetime as dt
import json
import random
import secrets
import sqlite3
from typing import Any

from ..config import LeagueConfig
from ..scoring import ScoringConfig

PHASES = ["lobby", "year_reveal", "minigame", "draft", "season", "playoffs", "complete"]

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no I/O/0/1
BOT_NAMES = [
    "Sabermetric Sam", "Bullpen Betty", "Waiver Wire Wally", "Dinger Dave",
    "Small Ball Sal", "Launch Angle Lou", "Bunt Brigade", "Rotisserie Rex",
    "Cheap Saves Chuck", "Punt Steals Pete", "Ghost Runner Gus", "Sacrifice Fly Fran",
    "Two-Start Tony", "Platoon Patty",
]


class LeagueError(RuntimeError):
    pass


def new_code(conn: sqlite3.Connection) -> str:
    for _ in range(50):
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
        if not conn.execute("SELECT 1 FROM leagues WHERE code = ?", (code,)).fetchone():
            return code
    raise LeagueError("could not allocate a unique join code")


def create_league(
    conn: sqlite3.Connection,
    name: str,
    config: LeagueConfig,
    scoring: ScoringConfig,
) -> dict[str, Any]:
    league_id = secrets.token_urlsafe(9)
    code = new_code(conn)
    token = secrets.token_urlsafe(16)
    conn.execute(
        """INSERT INTO leagues (id, code, name, commissioner_token, config_json,
                                scoring_json, season_year, phase, current_week, created_at)
           VALUES (?,?,?,?,?,?,NULL,'lobby',0,?)""",
        (league_id, code, name, token, json.dumps(config.to_dict()),
         json.dumps(scoring.to_dict()), dt.datetime.utcnow().isoformat(timespec="seconds")),
    )
    return {"id": league_id, "code": code, "commissioner_token": token}


def get_league(conn: sqlite3.Connection, key: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM leagues WHERE id = ? OR code = ?", (key, key.upper())
    ).fetchone()
    return dict(row) if row else None


def require_league(conn: sqlite3.Connection, key: str) -> dict[str, Any]:
    league = get_league(conn, key)
    if not league:
        raise LeagueError(f"no league {key!r}")
    return league


def league_config(league: dict[str, Any]) -> LeagueConfig:
    return LeagueConfig.from_dict(json.loads(league["config_json"]))


def league_scoring(league: dict[str, Any]) -> ScoringConfig:
    return ScoringConfig.from_dict(json.loads(league["scoring_json"]))


def teams(conn: sqlite3.Connection, league_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM teams WHERE league_id = ? ORDER BY seat", (league_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_team(conn: sqlite3.Connection, league_id: str, team_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM teams WHERE league_id = ? AND id = ?", (league_id, team_id)
    ).fetchone()
    return dict(row) if row else None


def team_for_token(conn: sqlite3.Connection, league_id: str, token: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM teams WHERE league_id = ? AND manager_token = ?", (league_id, token)
    ).fetchone()
    return dict(row) if row else None


def join(conn: sqlite3.Connection, league: dict[str, Any], team_name: str) -> dict[str, Any]:
    cfg = league_config(league)
    if league["phase"] != "lobby":
        raise LeagueError("this league has already started")
    current = teams(conn, league["id"])
    if len(current) >= cfg.max_teams:
        raise LeagueError(f"league is full ({cfg.max_teams} teams)")
    name = team_name.strip() or f"Team {len(current) + 1}"
    if any(t["name"].lower() == name.lower() for t in current):
        raise LeagueError(f"team name {name!r} is taken")

    team_id = secrets.token_urlsafe(8)
    token = secrets.token_urlsafe(16)
    seat = (max((t["seat"] for t in current), default=0)) + 1
    conn.execute(
        """INSERT INTO teams (id, league_id, name, manager_token, is_bot, seat,
                              locked_in, faab_remaining)
           VALUES (?,?,?,?,0,?,0,?)""",
        (team_id, league["id"], name, token, seat, cfg.faab_budget),
    )
    return {"team_id": team_id, "manager_token": token, "name": name, "seat": seat}


def set_locked_in(conn: sqlite3.Connection, league_id: str, team_id: str, locked: bool) -> None:
    conn.execute(
        "UPDATE teams SET locked_in = ? WHERE league_id = ? AND id = ?",
        (1 if locked else 0, league_id, team_id),
    )


def lobby_state(conn: sqlite3.Connection, league: dict[str, Any]) -> dict[str, Any]:
    cfg = league_config(league)
    roster = teams(conn, league["id"])
    humans = [t for t in roster if not t["is_bot"]]
    return {
        "phase": league["phase"],
        "code": league["code"],
        "name": league["name"],
        "season_year": league["season_year"],
        "teams": [
            {"id": t["id"], "name": t["name"], "is_bot": bool(t["is_bot"]),
             "locked_in": bool(t["locked_in"]), "seat": t["seat"],
             "draft_slot": t["draft_slot"]}
            for t in roster
        ],
        "target_team_count": cfg.team_count,
        "min_teams": cfg.min_teams,
        "max_teams": cfg.max_teams,
        "humans": len(humans),
        "all_locked_in": bool(humans) and all(t["locked_in"] for t in humans),
        "final_size_if_started": planned_size(cfg, len(humans)),
    }


def planned_size(cfg: LeagueConfig, human_count: int) -> int:
    """How big the league becomes when the lobby closes.

    Bots fill *unfilled* slots, but the league shrinks toward ``min_teams``
    rather than stuffing a half-empty lobby with bots: the final size is the
    commissioner's target unless fewer humans showed up, in which case it drops
    to the smallest even size that seats everyone, floored at ``min_teams``.
    """
    size = max(cfg.min_teams, human_count)
    if size % 2:
        size += 1
    return min(size, cfg.team_count)


def fill_bots(conn: sqlite3.Connection, league: dict[str, Any]) -> list[dict[str, Any]]:
    cfg = league_config(league)
    roster = teams(conn, league["id"])
    humans = [t for t in roster if not t["is_bot"]]
    target = planned_size(cfg, len(humans))
    existing_names = {t["name"] for t in roster}

    added: list[dict[str, Any]] = []
    seat = max((t["seat"] for t in roster), default=0)
    pool = [n for n in BOT_NAMES if n not in existing_names]
    while len(humans) + len(added) < target:
        seat += 1
        name = pool.pop(0) if pool else f"Bot {seat}"
        team_id = secrets.token_urlsafe(8)
        conn.execute(
            """INSERT INTO teams (id, league_id, name, manager_token, is_bot, seat,
                                  locked_in, faab_remaining)
               VALUES (?,?,?,NULL,1,?,1,?)""",
            (team_id, league["id"], name, seat, cfg.faab_budget),
        )
        added.append({"id": team_id, "name": name, "seat": seat})
    return added


def eligible_years(conn: sqlite3.Connection, cfg: LeagueConfig) -> list[int]:
    """Cached seasons that passed the pipeline's eligibility check."""
    allowed = set(cfg.eligible_years())
    rows = conn.execute("SELECT year FROM seasons WHERE eligible = 1 ORDER BY year").fetchall()
    return [r["year"] for r in rows if r["year"] in allowed]


def draw_season(
    conn: sqlite3.Connection, league: dict[str, Any], rng: random.Random | None = None
) -> int:
    """Randomly draw the replay season. Not a commissioner choice, by design."""
    cfg = league_config(league)
    years = eligible_years(conn, cfg)
    if not years:
        raise LeagueError(
            "no eligible seasons are cached — run "
            "`python -m app.pipeline.build --years 2000-2019` first"
        )
    year = (rng or random.SystemRandom()).choice(years)
    conn.execute("UPDATE leagues SET season_year = ? WHERE id = ?", (year, league["id"]))
    return year


def set_phase(conn: sqlite3.Connection, league_id: str, phase: str) -> None:
    if phase not in PHASES:
        raise LeagueError(f"unknown phase {phase!r}")
    conn.execute("UPDATE leagues SET phase = ? WHERE id = ?", (phase, league_id))


def start_from_lobby(
    conn: sqlite3.Connection, league: dict[str, Any], rng: random.Random | None = None
) -> dict[str, Any]:
    """Close the lobby: fill bots, draw the year, move to the reveal."""
    if league["phase"] != "lobby":
        raise LeagueError("lobby already closed")
    roster = teams(conn, league["id"])
    if not [t for t in roster if not t["is_bot"]]:
        raise LeagueError("at least one manager must join before starting")
    bots = fill_bots(conn, league)
    year = draw_season(conn, league, rng)
    set_phase(conn, league["id"], "year_reveal")
    return {"bots_added": bots, "season_year": year,
            "team_count": len(teams(conn, league["id"]))}


def log(conn: sqlite3.Connection, league_id: str, week: int, kind: str,
        team_id: str | None, player_id: str | None, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO transactions (league_id, week, ts, kind, team_id, player_id, detail) "
        "VALUES (?,?,?,?,?,?,?)",
        (league_id, week, dt.datetime.utcnow().isoformat(timespec="seconds"),
         kind, team_id, player_id, detail),
    )
