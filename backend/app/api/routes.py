"""REST API.

Everything that is not the draft room or the mini-game lives here: lobby,
lineups, waivers, standings, recaps, player pages and commissioner controls.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..config import ConfigError, LeagueConfig, pool_depth_check
from ..pipeline import coverage as coverage_mod
from ..scoring import ScoringConfig
from ..services import (
    draft as draft_svc,
    il as il_svc,
    leagues as leagues_svc,
    lineups as lineups_svc,
    players as players_svc,
    replay as replay_svc,
    standings as standings_svc,
    timeline,
    waivers as waivers_svc,
)
from .deps import (
    current_team,
    get_config,
    get_conn,
    get_league,
    require_commissioner,
    team_or_commissioner,
)

router = APIRouter(prefix="/api")


# ---------------------------------------------------------------------------
# request bodies
# ---------------------------------------------------------------------------

class CreateLeague(BaseModel):
    name: str = Field(default="Lockout League", max_length=60)
    config: dict[str, Any] | None = None


class JoinLeague(BaseModel):
    team_name: str = Field(..., max_length=40)


class LockIn(BaseModel):
    locked_in: bool = True


class SaveLineup(BaseModel):
    week: int
    assignment: dict[str, str]


class BidBody(BaseModel):
    add_player_id: str
    amount: int = Field(ge=0)
    drop_player_id: str | None = None
    priority: int = 1


class DropBody(BaseModel):
    player_id: str


class AdvanceBody(BaseModel):
    days: int = Field(default=1, ge=1, le=200)


class ConfigPatch(BaseModel):
    config: dict[str, Any] | None = None
    scoring: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# meta
# ---------------------------------------------------------------------------

@router.get("/meta/coverage")
def stat_coverage() -> dict[str, Any]:
    """Which stats each data source can supply — surfaced on the rules page."""
    return {
        "sources": {name: coverage_mod.report(name) for name in coverage_mod.SOURCES},
        "non_standard_stats": coverage_mod.NON_STANDARD_STATS,
    }


@router.get("/meta/seasons")
def seasons(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT year, source, opening_day, final_game_day, all_star_monday, player_count, "
        "game_count, eligible, ineligible_reason FROM seasons ORDER BY year"
    ).fetchall()
    return {"seasons": [dict(r) for r in rows]}


@router.get("/meta/defaults")
def defaults() -> dict[str, Any]:
    return {"config": LeagueConfig.load().to_dict(), "scoring": ScoringConfig.load().to_dict()}


# ---------------------------------------------------------------------------
# lobby
# ---------------------------------------------------------------------------

@router.post("/leagues")
def create_league(body: CreateLeague, conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, Any]:
    try:
        cfg = LeagueConfig.load().merged(body.config)
    except ConfigError as exc:
        raise HTTPException(400, str(exc)) from exc
    created = leagues_svc.create_league(conn, body.name, cfg, ScoringConfig.load())
    return {**created, "join_path": f"/join/{created['code']}", "config": cfg.to_dict()}


@router.get("/leagues/{code}")
def league_state(
    league: dict = Depends(get_league),
    cfg: LeagueConfig = Depends(get_config),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    state = leagues_svc.lobby_state(conn, league)
    state["config"] = cfg.to_dict()
    state["scoring"] = leagues_svc.league_scoring(league).to_dict()
    if league["season_year"]:
        state["timeline"] = timeline.describe(conn, league, cfg)
        state["pool_check"] = pool_depth_check(
            cfg, len(players_svc.list_players(conn, league["season_year"]))
        )
        state["season_caveats"] = _season_caveats(conn, league["season_year"])
    return state


def _season_caveats(conn: sqlite3.Connection, year: int) -> list[str]:
    """What this season's data cannot do, in a manager's terms.

    A season can be playable and still be missing something — most often the
    IL feed, which lives on a different site from the box scores. Managers
    should hear that from the app rather than work it out from nobody ever
    getting hurt.
    """
    row = conn.execute("SELECT coverage_json FROM seasons WHERE year=?", (year,)).fetchone()
    if row is None:
        return []
    caveats: list[str] = []
    if json.loads(row["coverage_json"] or "{}").get("no_il_data"):
        caveats.append(
            "No injured-list data was available for this season, so nobody goes on "
            "the IL: every drafted player stays startable all year, and the IL slots "
            "act as extra bench."
        )
    return caveats


@router.post("/leagues/{code}/join")
def join(
    body: JoinLeague,
    league: dict = Depends(get_league),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    try:
        return leagues_svc.join(conn, league, body.team_name)
    except leagues_svc.LeagueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/leagues/{code}/lock-in")
def lock_in(
    body: LockIn,
    league: dict = Depends(get_league),
    team: dict = Depends(current_team),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    leagues_svc.set_locked_in(conn, league["id"], team["id"], body.locked_in)
    return leagues_svc.lobby_state(conn, league)


@router.post("/leagues/{code}/start")
def start_league(
    league: dict = Depends(require_commissioner),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """Close the lobby: bots fill empty seats, then the season year is drawn."""
    try:
        result = leagues_svc.start_from_lobby(conn, league)
    except leagues_svc.LeagueError as exc:
        raise HTTPException(400, str(exc)) from exc
    league = leagues_svc.require_league(conn, league["id"])
    cfg = leagues_svc.league_config(league)
    pool = players_svc.list_players(conn, league["season_year"])
    result["pool_check"] = pool_depth_check(cfg, len(pool))
    result["timeline"] = timeline.describe(conn, league, cfg)
    return result


# ---------------------------------------------------------------------------
# draft (REST view; the live room is the WebSocket in live.py)
# ---------------------------------------------------------------------------

@router.get("/leagues/{code}/draft")
def draft_state(
    league: dict = Depends(get_league), conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    return draft_svc.state(conn, league)


@router.get("/leagues/{code}/draft/available")
def draft_available(
    league: dict = Depends(get_league),
    conn: sqlite3.Connection = Depends(get_conn),
    search: str | None = None,
    position: str | None = None,
    limit: int = Query(100, le=400),
) -> dict[str, Any]:
    if not league["season_year"]:
        raise HTTPException(400, "the season has not been drawn yet")
    scoring = leagues_svc.league_scoring(league)
    return {"players": draft_svc.available(conn, league, scoring, limit, search, position)}


# ---------------------------------------------------------------------------
# teams, rosters, lineups
# ---------------------------------------------------------------------------

@router.get("/leagues/{code}/teams")
def list_teams(
    league: dict = Depends(get_league), conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, Any]:
    return {"teams": [
        {k: v for k, v in t.items() if k != "manager_token"}
        for t in leagues_svc.teams(conn, league["id"])
    ]}


@router.get("/leagues/{code}/me")
def me(
    league: dict = Depends(get_league), team: dict = Depends(current_team)
) -> dict[str, Any]:
    return {k: v for k, v in team.items() if k != "manager_token"}


@router.get("/leagues/{code}/teams/{team_id}/lineup")
def get_lineup(
    team_id: str,
    week: int | None = None,
    league: dict = Depends(get_league),
    cfg: LeagueConfig = Depends(get_config),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    if not league["season_year"]:
        raise HTTPException(400, "the season has not started")
    current = max(1, league["current_week"] or 1)
    target = week or current
    if week is None and current < cfg.total_weeks and \
            lineups_svc.is_locked(conn, league["id"], team_id, current):
        # The current week locks the moment it starts, so defaulting to it would
        # open a read-only page on the one screen whose purpose is editing.
        # Without an explicit week, answer with the first one still open.
        target = current + 1
    try:
        return lineups_svc.view(conn, league, cfg, team_id, target)
    except LookupError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.put("/leagues/{code}/teams/{team_id}/lineup")
def put_lineup(
    body: SaveLineup,
    league: dict = Depends(get_league),
    cfg: LeagueConfig = Depends(get_config),
    team: dict = Depends(team_or_commissioner),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    try:
        summary = lineups_svc.save(conn, league, cfg, team["id"], body.week, body.assignment)
    except lineups_svc.LineupError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"saved": True, "summary": summary,
            "lineup": lineups_svc.view(conn, league, cfg, team["id"], body.week)}


@router.post("/leagues/{code}/teams/{team_id}/lineup/autofill")
def autofill_lineup(
    week: int,
    league: dict = Depends(get_league),
    cfg: LeagueConfig = Depends(get_config),
    team: dict = Depends(team_or_commissioner),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    if lineups_svc.is_locked(conn, league["id"], team["id"], week):
        raise HTTPException(400, "that week is locked")
    lineups_svc.autofill(conn, league, team["id"], week, cfg)
    return lineups_svc.view(conn, league, cfg, team["id"], week)


# ---------------------------------------------------------------------------
# waivers
# ---------------------------------------------------------------------------

@router.get("/leagues/{code}/free-agents")
def free_agents(
    league: dict = Depends(get_league),
    cfg: LeagueConfig = Depends(get_config),
    conn: sqlite3.Connection = Depends(get_conn),
    search: str | None = None,
    position: str | None = None,
    limit: int = Query(80, le=300),
) -> dict[str, Any]:
    if not league["season_year"]:
        raise HTTPException(400, "the season has not started")
    pool = waivers_svc.free_agents(conn, league, cfg, limit, search, position)
    return {
        "as_of": timeline.as_of_date(conn, league, cfg),
        "players": pool,
        "adds_frozen": waivers_svc.adds_frozen(cfg, (league["current_week"] or 1) + 1),
        "frozen_reason": waivers_svc.freeze_reason(cfg, (league["current_week"] or 1) + 1),
        "note": (
            "Stats shown are through the current replay date only. The app never "
            "shows full-season or future numbers for free agents."
        ),
    }


@router.post("/leagues/{code}/waivers/bids")
def place_bid(
    body: BidBody,
    league: dict = Depends(get_league),
    cfg: LeagueConfig = Depends(get_config),
    team: dict = Depends(current_team),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    try:
        bid_id = waivers_svc.submit_bid(
            conn, league, cfg, team["id"], body.add_player_id, body.amount,
            body.drop_player_id, body.priority,
        )
    except waivers_svc.WaiverError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"bid_id": bid_id, "processes_week": (league["current_week"] or 1) + 1}


@router.get("/leagues/{code}/waivers/bids")
def my_bids(
    league: dict = Depends(get_league),
    team: dict = Depends(current_team),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    return {"bids": waivers_svc.my_bids(conn, league["id"], team["id"]),
            "faab_remaining": team["faab_remaining"]}


@router.delete("/leagues/{code}/waivers/bids/{bid_id}")
def cancel_bid(
    bid_id: int,
    league: dict = Depends(get_league),
    team: dict = Depends(current_team),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    try:
        waivers_svc.cancel_bid(conn, league["id"], team["id"], bid_id)
    except waivers_svc.WaiverError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"cancelled": bid_id}


@router.get("/leagues/{code}/waivers/results")
def waiver_results(
    week: int | None = None,
    league: dict = Depends(get_league),
    cfg: LeagueConfig = Depends(get_config),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    return waivers_svc.summary(conn, league, cfg, week or (league["current_week"] or 1))


@router.post("/leagues/{code}/teams/{team_id}/drop")
def drop_player(
    body: DropBody,
    league: dict = Depends(get_league),
    cfg: LeagueConfig = Depends(get_config),
    team: dict = Depends(team_or_commissioner),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    try:
        clears = waivers_svc.drop_to_waivers(
            conn, league, cfg, team["id"], body.player_id, league["current_week"] or 1
        )
    except waivers_svc.WaiverError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"dropped": body.player_id, "clears_waivers_on": clears}


# ---------------------------------------------------------------------------
# standings, recaps, players
# ---------------------------------------------------------------------------

@router.get("/leagues/{code}/standings")
def get_standings(
    league: dict = Depends(get_league),
    cfg: LeagueConfig = Depends(get_config),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    data = {"standings": standings_svc.table(conn, league), "phase": league["phase"]}
    if league["season_year"]:
        data["timeline"] = timeline.describe(conn, league, cfg)
    if league["phase"] in ("playoffs", "complete"):
        data["bracket"] = standings_svc.bracket(conn, league, cfg)
        data["champion"] = standings_svc.champion(conn, league, cfg)
    return data


@router.get("/leagues/{code}/bracket")
def get_bracket(
    league: dict = Depends(get_league),
    cfg: LeagueConfig = Depends(get_config),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    return standings_svc.bracket(conn, league, cfg)


@router.get("/leagues/{code}/matchups")
def matchups(
    week: int | None = None,
    league: dict = Depends(get_league),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    target = week or (league["current_week"] or 1)
    names = {t["id"]: t["name"] for t in leagues_svc.teams(conn, league["id"])}
    rows = conn.execute(
        "SELECT * FROM matchups WHERE league_id=? AND week=? ORDER BY slot",
        (league["id"], target),
    ).fetchall()
    return {"week": target, "matchups": [
        {**dict(r), "home_name": names.get(r["home_team_id"]),
         "away_name": names.get(r["away_team_id"])} for r in rows
    ]}


@router.get("/leagues/{code}/recap")
def recap(
    week: int | None = None,
    league: dict = Depends(get_league),
    cfg: LeagueConfig = Depends(get_config),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    target = week or max(1, (league["current_week"] or 1))
    try:
        return replay_svc.week_recap(conn, league, cfg, target)
    except LookupError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/leagues/{code}/day")
def day_recap(
    date: str | None = None,
    league: dict = Depends(get_league),
    cfg: LeagueConfig = Depends(get_config),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """One replayed day. Defaults to the most recent one — last night's games."""
    try:
        target = dt.date.fromisoformat(date) if date else None
    except ValueError as exc:
        raise HTTPException(400, "date must be YYYY-MM-DD") from exc
    try:
        return replay_svc.day_recap(conn, league, cfg, target)
    except LookupError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("/leagues/{code}/leaders")
def leaders(
    league: dict = Depends(get_league),
    cfg: LeagueConfig = Depends(get_config),
    conn: sqlite3.Connection = Depends(get_conn),
    limit: int = Query(25, le=100),
) -> dict[str, Any]:
    """Stat leaders among rostered players, through the current replay date."""
    rows = conn.execute(
        """SELECT s.player_id, p.name, p.positions, p.mlb_team, t.name AS team_name,
                  ROUND(SUM(s.points), 2) pts, COUNT(DISTINCT s.date) days
             FROM scoring_lines s
             JOIN players p ON p.player_id = s.player_id AND p.season = ?
             JOIN teams t ON t.id = s.team_id
            WHERE s.league_id = ?
            GROUP BY s.player_id
            ORDER BY pts DESC LIMIT ?""",
        (league["season_year"], league["id"], limit),
    ).fetchall()
    return {"as_of": timeline.as_of_date(conn, league, cfg), "leaders": [dict(r) for r in rows]}


@router.get("/leagues/{code}/players/{player_id}")
def player_page(
    player_id: str,
    league: dict = Depends(get_league),
    cfg: LeagueConfig = Depends(get_config),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    season = league["season_year"]
    if not season:
        raise HTTPException(400, "the season has not been drawn yet")
    player = players_svc.get_player(conn, season, player_id)
    if player is None:
        raise HTTPException(404, "unknown player")
    scoring = leagues_svc.league_scoring(league)
    since, as_of = timeline.replay_window(conn, league, cfg)
    # Player pages are capped at the replay's current date for the same reason
    # the free-agent pool is: no in-app hindsight.
    totals = players_svc.stats_through(conn, season, scoring, as_of, since=since).get(player_id)
    owner = conn.execute(
        "SELECT t.id, t.name FROM rosters r JOIN teams t ON t.id = r.team_id "
        "WHERE r.league_id=? AND r.player_id=?",
        (league["id"], player_id),
    ).fetchone()
    fantasy = conn.execute(
        "SELECT week, ROUND(SUM(points),2) pts FROM scoring_lines "
        "WHERE league_id=? AND player_id=? GROUP BY week ORDER BY week",
        (league["id"], player_id),
    ).fetchall()
    return {
        "player": player,
        "as_of": as_of,
        "owner": dict(owner) if owner else None,
        "eligible_slots": players_svc.eligible_slots(player, cfg.active_slots.keys()),
        "totals": totals,
        "by_week": [dict(r) for r in fantasy],
        "game_log": players_svc.daily_lines(conn, season, player_id, since, as_of)[-30:],
        "il_log": il_svc.player_il_log(conn, season, player_id, through=as_of),
    }


@router.get("/leagues/{code}/transactions")
def transactions(
    league: dict = Depends(get_league),
    conn: sqlite3.Connection = Depends(get_conn),
    limit: int = Query(60, le=300),
) -> dict[str, Any]:
    rows = conn.execute(
        """SELECT x.*, t.name AS team_name, p.name AS player_name
             FROM transactions x
             LEFT JOIN teams t ON t.id = x.team_id
             LEFT JOIN players p ON p.player_id = x.player_id AND p.season = ?
            WHERE x.league_id = ? ORDER BY x.id DESC LIMIT ?""",
        (league["season_year"], league["id"], limit),
    ).fetchall()
    return {"transactions": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# commissioner
# ---------------------------------------------------------------------------

@router.patch("/leagues/{code}/settings")
def update_settings(
    body: ConfigPatch,
    league: dict = Depends(require_commissioner),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    cfg = leagues_svc.league_config(league)
    scoring = leagues_svc.league_scoring(league)
    if body.config:
        if league["phase"] != "lobby" and any(
            k in body.config for k in ("team_count", "min_teams", "max_teams", "active_slots")
        ):
            raise HTTPException(400, "roster shape and team count are locked once the draft begins")
        try:
            cfg = cfg.merged(body.config)
        except ConfigError as exc:
            raise HTTPException(400, str(exc)) from exc
    if body.scoring:
        merged = scoring.to_dict()
        for half in ("batting", "pitching", "options"):
            if half in body.scoring:
                merged[half] = {**merged.get(half, {}), **body.scoring[half]}
        scoring = ScoringConfig.from_dict(merged)
    conn.execute(
        "UPDATE leagues SET config_json = ?, scoring_json = ? WHERE id = ?",
        (json.dumps(cfg.to_dict()), json.dumps(scoring.to_dict()), league["id"]),
    )
    return {"config": cfg.to_dict(), "scoring": scoring.to_dict()}


@router.post("/leagues/{code}/advance")
def advance(
    body: AdvanceBody,
    league: dict = Depends(require_commissioner),
    cfg: LeagueConfig = Depends(get_config),
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, Any]:
    """Run the nightly replay step now. The real cadence is 8:00 PM CST daily."""
    steps = replay_svc.catch_up(conn, league, cfg, body.days)
    league = leagues_svc.require_league(conn, league["id"])
    return {"steps": steps, "phase": league["phase"],
            "current_week": league["current_week"],
            "last_simulated_date": league["last_simulated_date"]}
