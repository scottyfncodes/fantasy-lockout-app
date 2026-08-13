"""WebSocket rooms for the two places managers act at the same moment.

Real-time is used deliberately and narrowly: the draft-order mini-game and the
draft room.  Lineups, waivers, standings and box scores are plain
request/response — they have no concurrent-editing problem, and pushing them
over a socket would buy nothing but reconnect logic.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from fastapi import WebSocket

from .services.minigame import SpeedRound


class Room:
    def __init__(self, key: str) -> None:
        self.key = key
        self.sockets: set[WebSocket] = set()
        self.lock = asyncio.Lock()

    async def join(self, ws: WebSocket) -> None:
        await ws.accept()
        self.sockets.add(ws)

    def leave(self, ws: WebSocket) -> None:
        self.sockets.discard(ws)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for ws in list(self.sockets):
            try:
                await ws.send_json(payload)
            except Exception:  # noqa: BLE001 - a dropped client is not an error
                dead.append(ws)
        for ws in dead:
            self.sockets.discard(ws)


class Hub:
    """Rooms, live mini-game state and the per-league draft lock."""

    def __init__(self) -> None:
        self._rooms: dict[str, Room] = {}
        self._rounds: dict[str, SpeedRound] = {}
        self._draft_locks: dict[str, asyncio.Lock] = {}
        self._pick_deadlines: dict[str, tuple[int, float]] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def room(self, league_id: str, name: str) -> Room:
        key = f"{league_id}:{name}"
        if key not in self._rooms:
            self._rooms[key] = Room(key)
        return self._rooms[key]

    # ---- mini-game ----------------------------------------------------
    def round_for(self, league_id: str) -> SpeedRound | None:
        return self._rounds.get(league_id)

    def set_round(self, league_id: str, rnd: SpeedRound) -> None:
        self._rounds[league_id] = rnd

    def clear_round(self, league_id: str) -> None:
        self._rounds.pop(league_id, None)

    # ---- draft --------------------------------------------------------
    def draft_lock(self, league_id: str) -> asyncio.Lock:
        if league_id not in self._draft_locks:
            self._draft_locks[league_id] = asyncio.Lock()
        return self._draft_locks[league_id]

    def pick_deadline(self, league_id: str, overall: int, seconds: float) -> float:
        """When the pick currently on the clock expires (monotonic).

        Keyed by the overall pick number so reconnecting, or a second client
        opening the room, cannot hand a manager a fresh clock.
        """
        now = time.monotonic()
        existing = self._pick_deadlines.get(league_id)
        if existing is None or existing[0] != overall:
            self._pick_deadlines[league_id] = (overall, now + seconds)
        return self._pick_deadlines[league_id][1]

    def clear_deadline(self, league_id: str) -> None:
        self._pick_deadlines.pop(league_id, None)

    # ---- background tasks ---------------------------------------------
    def spawn(self, key: str, coro) -> None:
        """Run one named background task per league, replacing any predecessor."""
        self.cancel(key)
        self._tasks[key] = asyncio.create_task(coro)

    def cancel(self, key: str) -> None:
        task = self._tasks.pop(key, None)
        if task and not task.done():
            task.cancel()

    async def shutdown(self) -> None:
        for key in list(self._tasks):
            task = self._tasks.pop(key)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


hub = Hub()
