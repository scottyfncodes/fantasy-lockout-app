"""Draft-order mini-game: the Speed Round.

Every manager gets the same fixed window (default 10 seconds) to tap a moving
baseball as many times as they can.  Highest count picks first.

This is the one part of the app besides the draft room where people act at the
same moment, so the server is authoritative: it owns the clock, counts the
taps, moves the target (everyone sees the same ball in the same place), and
broadcasts the running scoreboard.  A client that lies about its own score
cannot, because it never sends one — only taps, which are rate-limited.

Bots tap too, at a plausible human rate, so a lobby with empty seats still
resolves.

The commissioner can switch ``draft_order_mode`` to ``randomizer`` for the
simpler slot-machine reveal; ``randomized_order`` implements that path.
"""

from __future__ import annotations

import math
import random
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

# A human can sustain roughly 8 taps/second; anything above this is discarded
# rather than rejected outright, so a laggy burst isn't punished.
MAX_TAPS_PER_SECOND = 12
COUNTDOWN_SECONDS = 3


@dataclass
class Contestant:
    team_id: str
    name: str
    is_bot: bool
    score: int = 0
    tiebreak: float = 0.0
    _bot_rate: float = 0.0
    _bot_credited: float = 0.0
    _tap_times: list[float] = field(default_factory=list)


@dataclass
class SpeedRound:
    league_id: str
    duration: float
    contestants: dict[str, Contestant] = field(default_factory=dict)
    starts_at: float | None = None       # monotonic clock
    ends_at: float | None = None
    finished: bool = False
    seed: int = 0

    # ---- lifecycle ----------------------------------------------------
    def start(self, now: float | None = None) -> None:
        now = now if now is not None else time.monotonic()
        rng = random.Random(self.seed)
        self.starts_at = now + COUNTDOWN_SECONDS
        self.ends_at = self.starts_at + self.duration
        for c in self.contestants.values():
            c.tiebreak = rng.random()
            if c.is_bot:
                c._bot_rate = rng.uniform(3.2, 6.4)

    @property
    def state(self) -> str:
        if self.finished:
            return "finished"
        if self.starts_at is None:
            return "waiting"
        now = time.monotonic()
        if now < self.starts_at:
            return "countdown"
        if now < (self.ends_at or 0):
            return "running"
        return "ended"

    def remaining(self, now: float | None = None) -> float:
        now = now if now is not None else time.monotonic()
        if self.starts_at is None:
            return self.duration
        if now < self.starts_at:
            return self.duration
        return max(0.0, (self.ends_at or 0) - now)

    def countdown(self, now: float | None = None) -> float:
        now = now if now is not None else time.monotonic()
        if self.starts_at is None:
            return float(COUNTDOWN_SECONDS)
        return max(0.0, self.starts_at - now)

    # ---- play ---------------------------------------------------------
    def tap(self, team_id: str, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        c = self.contestants.get(team_id)
        if c is None or self.state != "running":
            return False
        window = [t for t in c._tap_times if now - t < 1.0]
        if len(window) >= MAX_TAPS_PER_SECOND:
            c._tap_times = window
            return False
        window.append(now)
        c._tap_times = window
        c.score += 1
        return True

    def tick_bots(self, now: float | None = None) -> None:
        """Credit bot taps for the time that has elapsed."""
        now = now if now is not None else time.monotonic()
        if self.starts_at is None:
            return
        elapsed = max(0.0, min(now, self.ends_at or 0) - self.starts_at)
        for c in self.contestants.values():
            if not c.is_bot:
                continue
            owed = elapsed * c._bot_rate
            gain = int(owed - c._bot_credited)
            if gain > 0:
                c.score += gain
                c._bot_credited += gain

    def target_position(self, now: float | None = None) -> dict[str, float]:
        """Where the ball is, in 0..1 box coordinates.

        Driven by the shared clock and seed so every client renders the same
        ball in the same place — the round is genuinely simultaneous.
        """
        now = now if now is not None else time.monotonic()
        t = 0.0 if self.starts_at is None else max(0.0, now - self.starts_at)
        s = (self.seed % 997) / 997.0
        x = 0.5 + 0.42 * math.sin(t * 1.9 + s * 6.3)
        y = 0.5 + 0.34 * math.sin(t * 2.7 + 1.3 + s * 4.1)
        size = 0.10 + 0.03 * math.sin(t * 3.3)
        return {"x": round(x, 4), "y": round(y, 4), "size": round(size, 4)}

    # ---- results ------------------------------------------------------
    def standings(self) -> list[dict[str, Any]]:
        ordered = sorted(
            self.contestants.values(), key=lambda c: (-c.score, -c.tiebreak, c.team_id)
        )
        return [
            {"team_id": c.team_id, "name": c.name, "is_bot": c.is_bot,
             "score": c.score, "pick": i + 1}
            for i, c in enumerate(ordered)
        ]

    def snapshot(self) -> dict[str, Any]:
        return {
            "type": "minigame_state",
            "state": self.state,
            "countdown": round(self.countdown(), 2),
            "remaining": round(self.remaining(), 2),
            "duration": self.duration,
            "target": self.target_position(),
            "standings": self.standings(),
        }


def build_round(
    conn: sqlite3.Connection, league_id: str, duration: float, seed: int | None = None
) -> SpeedRound:
    rows = conn.execute(
        "SELECT id, name, is_bot FROM teams WHERE league_id = ? ORDER BY seat", (league_id,)
    ).fetchall()
    rnd = SpeedRound(
        league_id=league_id,
        duration=duration,
        seed=seed if seed is not None else random.randrange(1_000_000),
    )
    for r in rows:
        rnd.contestants[r["id"]] = Contestant(
            team_id=r["id"], name=r["name"], is_bot=bool(r["is_bot"])
        )
    return rnd


def persist_results(conn: sqlite3.Connection, rnd: SpeedRound) -> list[dict[str, Any]]:
    """Write scores and assign draft slots. Highest score picks first."""
    order = rnd.standings()
    conn.execute("DELETE FROM minigame_scores WHERE league_id = ?", (rnd.league_id,))
    for entry in order:
        c = rnd.contestants[entry["team_id"]]
        conn.execute(
            "INSERT INTO minigame_scores (league_id, team_id, score, finished, tiebreak) "
            "VALUES (?,?,?,1,?)",
            (rnd.league_id, entry["team_id"], entry["score"], c.tiebreak),
        )
        conn.execute(
            "UPDATE teams SET draft_slot = ? WHERE league_id = ? AND id = ?",
            (entry["pick"], rnd.league_id, entry["team_id"]),
        )
    rnd.finished = True
    return order


def randomized_order(
    conn: sqlite3.Connection, league_id: str, rng: random.Random | None = None
) -> list[dict[str, Any]]:
    """Fallback draft-order mode: a plain provably-fair shuffle."""
    rng = rng or random.SystemRandom()
    rows = conn.execute(
        "SELECT id, name, is_bot FROM teams WHERE league_id = ? ORDER BY seat", (league_id,)
    ).fetchall()
    order = [dict(r) for r in rows]
    rng.shuffle(order)
    result = []
    for i, t in enumerate(order, start=1):
        conn.execute(
            "UPDATE teams SET draft_slot = ? WHERE league_id = ? AND id = ?",
            (i, league_id, t["id"]),
        )
        result.append({"team_id": t["id"], "name": t["name"],
                       "is_bot": bool(t["is_bot"]), "score": 0, "pick": i})
    return result
