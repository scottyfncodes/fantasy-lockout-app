"""WebSocket endpoints: the draft-order mini-game and the live draft room.

These are the only two places in the app where managers act at the same
instant, so they get a shared room with server-authoritative state.  Everything
else uses the REST API in ``routes.py``.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from .. import db
from ..services import (
    bots,
    draft as draft_svc,
    leagues as leagues_svc,
    minigame as minigame_svc,
    replay as replay_svc,
)
from ..ws import hub

router = APIRouter()

TICK_HZ = 12
BOT_PICK_DELAY = 0.9


def _identify(conn: sqlite3.Connection, code: str, token: str | None,
              commissioner_token: str | None) -> tuple[dict[str, Any], dict | None, bool]:
    league = leagues_svc.get_league(conn, code)
    if league is None:
        raise LookupError(f"no league {code!r}")
    team = leagues_svc.team_for_token(conn, league["id"], token) if token else None
    is_commissioner = bool(commissioner_token) and commissioner_token == league["commissioner_token"]
    return league, team, is_commissioner


# ---------------------------------------------------------------------------
# lobby + mini-game
# ---------------------------------------------------------------------------

@router.websocket("/ws/{code}/lobby")
async def lobby_socket(
    websocket: WebSocket, code: str, token: str | None = None, commish: str | None = None
) -> None:
    with db.closing_conn() as conn:
        try:
            league, team, is_commissioner = _identify(conn, code, token, commish)
        except LookupError:
            await websocket.close(code=4404)
            return
        league_id = league["id"]
        state = leagues_svc.lobby_state(conn, league)

    room = hub.room(league_id, "lobby")
    await room.join(websocket)
    await websocket.send_json({"type": "lobby_state", **state})
    rnd = hub.round_for(league_id)
    if rnd:
        await websocket.send_json(rnd.snapshot())

    try:
        while True:
            msg = await websocket.receive_json()
            kind = msg.get("type")

            if kind == "tap" and team:
                rnd = hub.round_for(league_id)
                if rnd:
                    rnd.tap(team["id"])

            elif kind == "lock_in" and team:
                await run_in_threadpool(_set_lock, league_id, team["id"], bool(msg.get("value", True)))
                await _broadcast_lobby(league_id)

            elif kind == "start_countdown" and is_commissioner:
                seconds = msg.get("seconds")
                hub.spawn(f"lobbytimer:{league_id}", _lobby_countdown(league_id, seconds))

            elif kind == "cancel_countdown" and is_commissioner:
                hub.cancel(f"lobbytimer:{league_id}")
                await hub.room(league_id, "lobby").broadcast(
                    {"type": "lobby_countdown", "remaining": None}
                )

            elif kind == "start_minigame" and is_commissioner:
                await _start_minigame(league_id)

            elif kind == "refresh":
                await _broadcast_lobby(league_id)

    except WebSocketDisconnect:
        pass
    finally:
        room.leave(websocket)


def _set_lock(league_id: str, team_id: str, value: bool) -> None:
    with db.closing_conn() as conn:
        leagues_svc.set_locked_in(conn, league_id, team_id, value)


async def _broadcast_lobby(league_id: str) -> None:
    def load() -> dict[str, Any]:
        with db.closing_conn() as conn:
            league = leagues_svc.require_league(conn, league_id)
            return leagues_svc.lobby_state(conn, league)

    state = await run_in_threadpool(load)
    await hub.room(league_id, "lobby").broadcast({"type": "lobby_state", **state})


async def _lobby_countdown(league_id: str, seconds: float | None = None) -> None:
    """Close the lobby automatically when the commissioner's timer runs out.

    Waiting on stragglers is the most common way a league stalls, so the
    commissioner can start a countdown instead of chasing people: when it
    expires the empty seats become bots and the season year is drawn.
    """
    room = hub.room(league_id, "lobby")
    if seconds is None:
        with db.closing_conn() as conn:
            league = leagues_svc.require_league(conn, league_id)
            seconds = leagues_svc.league_config(league).lobby_timeout_seconds

    remaining = float(seconds)
    try:
        while remaining > 0:
            await room.broadcast({"type": "lobby_countdown", "remaining": round(remaining)})
            await asyncio.sleep(1.0)
            remaining -= 1.0

        def close() -> dict[str, Any]:
            with db.closing_conn() as conn:
                league = leagues_svc.require_league(conn, league_id)
                if league["phase"] != "lobby":
                    return {}
                with db.transaction(conn):
                    result = leagues_svc.start_from_lobby(conn, league)
                league = leagues_svc.require_league(conn, league_id)
                return {**result, "state": leagues_svc.lobby_state(conn, league)}

        outcome = await run_in_threadpool(close)
        await room.broadcast({"type": "lobby_countdown", "remaining": 0})
        if outcome:
            await room.broadcast({"type": "lobby_state", **outcome["state"]})
    except asyncio.CancelledError:  # pragma: no cover - cancel path
        raise


async def _start_minigame(league_id: str) -> None:
    def prepare() -> tuple[Any, str]:
        with db.closing_conn() as conn:
            league = leagues_svc.require_league(conn, league_id)
            cfg = leagues_svc.league_config(league)
            leagues_svc.set_phase(conn, league_id, "minigame")
            if cfg.draft_order_mode == "randomizer":
                order = minigame_svc.randomized_order(conn, league_id)
                return order, "randomizer"
            rnd = minigame_svc.build_round(conn, league_id, cfg.speed_round_seconds)
            return rnd, "speed_round"

    prepared, mode = await run_in_threadpool(prepare)
    if mode == "randomizer":
        # Open the draft room *before* announcing the order, so a manager who
        # acts on the announcement finds a draft board that already exists.
        await _finish_order(league_id)
        await hub.room(league_id, "lobby").broadcast(
            {"type": "draft_order", "mode": "randomizer", "order": prepared}
        )
        return

    prepared.start()
    hub.set_round(league_id, prepared)
    hub.spawn(f"minigame:{league_id}", _run_minigame(league_id))


async def _run_minigame(league_id: str) -> None:
    """Drive the shared clock: tick bots, broadcast state, then settle."""
    room = hub.room(league_id, "lobby")
    try:
        while True:
            rnd = hub.round_for(league_id)
            if rnd is None:
                return
            rnd.tick_bots()
            await room.broadcast(rnd.snapshot())
            if rnd.state == "ended":
                break
            await asyncio.sleep(1 / TICK_HZ)

        order = await run_in_threadpool(_persist_minigame, league_id)
        await _finish_order(league_id)
        await room.broadcast({"type": "draft_order", "mode": "speed_round", "order": order})
    except asyncio.CancelledError:  # pragma: no cover - shutdown path
        raise


def _persist_minigame(league_id: str) -> list[dict[str, Any]]:
    rnd = hub.round_for(league_id)
    with db.closing_conn() as conn:
        with db.transaction(conn):
            return minigame_svc.persist_results(conn, rnd)


async def _finish_order(league_id: str) -> None:
    """Draft order is set — build the board and open the draft room."""
    def prepare() -> dict[str, Any]:
        with db.closing_conn() as conn:
            league = leagues_svc.require_league(conn, league_id)
            with db.transaction(conn):
                draft_svc.initialize(conn, league)
                leagues_svc.set_phase(conn, league_id, "draft")
            league = leagues_svc.require_league(conn, league_id)
            return draft_svc.state(conn, league)

    state = await run_in_threadpool(prepare)
    hub.clear_round(league_id)
    await hub.room(league_id, "draft").broadcast(state)
    hub.spawn(f"draftbot:{league_id}", _bot_pick_loop(league_id))


# ---------------------------------------------------------------------------
# draft room
# ---------------------------------------------------------------------------

@router.websocket("/ws/{code}/draft")
async def draft_socket(
    websocket: WebSocket, code: str, token: str | None = None, commish: str | None = None
) -> None:
    with db.closing_conn() as conn:
        try:
            league, team, is_commissioner = _identify(conn, code, token, commish)
        except LookupError:
            await websocket.close(code=4404)
            return
        league_id = league["id"]

    room = hub.room(league_id, "draft")
    await room.join(websocket)
    # Through the same helper as every broadcast, so the first frame a client
    # sees carries the pick clock like all the others.
    await websocket.send_json(await run_in_threadpool(_draft_state, league_id))
    hub.spawn(f"draftbot:{league_id}", _bot_pick_loop(league_id))

    try:
        while True:
            msg = await websocket.receive_json()
            kind = msg.get("type")

            if kind == "pick" and team:
                await _handle_pick(league_id, team["id"], msg.get("player_id"), auto=False)
            elif kind == "force_pick" and is_commissioner:
                await _force_pick(league_id)
            elif kind == "refresh":
                await websocket.send_json(await run_in_threadpool(_draft_state, league_id))
    except WebSocketDisconnect:
        pass
    finally:
        room.leave(websocket)


def _draft_state(league_id: str) -> dict[str, Any]:
    with db.closing_conn() as conn:
        league = leagues_svc.require_league(conn, league_id)
        cfg = leagues_svc.league_config(league)
        state = draft_svc.state(conn, league)

    # Attach the clock so clients can count down locally rather than being
    # driven by a broadcast every second.
    state["pick_seconds"] = cfg.draft_pick_seconds
    state["seconds_remaining"] = None
    on_clock = state.get("on_clock")
    if on_clock and cfg.draft_pick_seconds:
        deadline = hub.pick_deadline(
            league_id, on_clock["overall"], cfg.draft_pick_seconds)
        state["seconds_remaining"] = max(0.0, round(deadline - time.monotonic(), 1))
    return state


async def _handle_pick(league_id: str, team_id: str, player_id: str | None, auto: bool) -> None:
    if not player_id:
        return
    # One pick at a time per league: the lock is what stops two managers from
    # taking the same player in the same instant.
    async with hub.draft_lock(league_id):
        def commit() -> dict[str, Any]:
            with db.closing_conn() as conn:
                league = leagues_svc.require_league(conn, league_id)
                with db.transaction(conn):
                    return draft_svc.make_pick(conn, league, team_id, player_id, auto=auto)

        try:
            pick = await run_in_threadpool(commit)
        except draft_svc.DraftError as exc:
            await hub.room(league_id, "draft").broadcast(
                {"type": "draft_error", "team_id": team_id, "message": str(exc)}
            )
            return

        await hub.room(league_id, "draft").broadcast({"type": "pick_made", "pick": {
            "overall": pick["overall"], "round": pick["round"],
            "pick_in_round": pick["pick_in_round"], "team_id": pick["team_id"],
            "player_name": pick["player"]["name"], "positions": pick["player"]["positions"],
            "player_id": pick["player"]["player_id"], "auto": pick["auto"],
        }})
        await _push_state(league_id)


async def _push_state(league_id: str) -> None:
    state = await run_in_threadpool(_draft_state, league_id)
    await hub.room(league_id, "draft").broadcast(state)
    if state["progress"]["complete"]:
        await run_in_threadpool(_finish_draft, league_id)
        await hub.room(league_id, "draft").broadcast({"type": "draft_complete"})
        await hub.room(league_id, "lobby").broadcast({"type": "phase", "phase": "season"})


def _finish_draft(league_id: str) -> None:
    hub.clear_deadline(league_id)
    with db.closing_conn() as conn:
        league = leagues_svc.require_league(conn, league_id)
        cfg = leagues_svc.league_config(league)
        with db.transaction(conn):
            replay_svc.start_season(conn, league, cfg)


async def _force_pick(league_id: str) -> None:
    def pick_for() -> tuple[str, str] | None:
        with db.closing_conn() as conn:
            league = leagues_svc.require_league(conn, league_id)
            cfg = leagues_svc.league_config(league)
            current = draft_svc.current_pick(conn, league["id"])
            if not current:
                return None
            choice = bots.choose_draft_pick(conn, league, cfg, current["team_id"])
            return (current["team_id"], choice["player_id"]) if choice else None

    target = await run_in_threadpool(pick_for)
    if target:
        await _handle_pick(league_id, target[0], target[1], auto=True)


async def _bot_pick_loop(league_id: str) -> None:
    """Make picks for bot teams whenever one is on the clock."""
    try:
        while True:
            def next_pick() -> tuple[str, str] | dict[str, str] | None:
                """Who to pick for right now: a bot, or a human out of time."""
                with db.closing_conn() as conn:
                    league = leagues_svc.require_league(conn, league_id)
                    if league["phase"] != "draft":
                        return None
                    cfg = leagues_svc.league_config(league)
                    current = draft_svc.current_pick(conn, league["id"])
                    if not current:
                        return None
                    team = leagues_svc.get_team(conn, league["id"], current["team_id"])
                    if not team:
                        return None
                    if not team["is_bot"]:
                        # A human is on the clock. One manager losing their
                        # connection must not stall thirteen other people, so
                        # the room picks for them when the clock runs out.
                        if not cfg.draft_pick_seconds:
                            return None
                        deadline = hub.pick_deadline(
                            league_id, current["overall"], cfg.draft_pick_seconds)
                        if time.monotonic() < deadline:
                            return None
                    choice = bots.choose_draft_pick(conn, league, cfg, team["id"])
                    if choice is None:
                        # No legal pick exists. Waiting would stall the whole
                        # room in silence, so say so and stand down.
                        return {"stuck": team["id"], "name": team["name"]}
                    return (team["id"], choice["player_id"])

            target = await run_in_threadpool(next_pick)
            if isinstance(target, dict):
                await hub.room(league_id, "draft").broadcast({
                    "type": "draft_error",
                    "team_id": target["stuck"],
                    "message": (
                        f"{target['name']} has no legal pick left — the commissioner "
                        "needs to step in."
                    ),
                })
                return
            if target is None:
                await asyncio.sleep(0.5)  # also the clock's resolution
                if await run_in_threadpool(_draft_over, league_id):
                    return
                continue
            await asyncio.sleep(BOT_PICK_DELAY)
            await _handle_pick(league_id, target[0], target[1], auto=True)
    except asyncio.CancelledError:  # pragma: no cover - shutdown path
        raise


def _draft_over(league_id: str) -> bool:
    with db.closing_conn() as conn:
        league = leagues_svc.get_league(conn, league_id)
        return league is None or league["phase"] != "draft"
