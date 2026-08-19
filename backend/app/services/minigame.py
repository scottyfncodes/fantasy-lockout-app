"""Draft-order mini-game: the Green Light.

Everyone stares at the same pad. It counts three, two, one, holds for a beat
nobody can predict, and turns green. The order people tap after that is the
draft order — fastest reaction picks first.

Tapping before green is a false start, and it costs you: jumpers go behind
everyone who waited. Otherwise the winning move is to hammer the pad from the
countdown, which is not a game.

The server is authoritative, as it has to be. It owns the countdown, chooses
the green-light moment, and — crucially — timestamps arrivals itself rather
than trusting a client's claim about its own reaction time. Nobody sends a
score; they send a tap, and the server decides what it was worth.

Bots never compete. A bot has no reaction time worth simulating and no feelings
about picking last, so they line up at the back in a random order among
themselves, and every human beats every bot.
"""

from __future__ import annotations

import random
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

COUNTDOWN_SECONDS = 3.0

# How long after the countdown the light actually turns green. Randomised, or
# the game becomes a stopwatch exercise rather than a reaction.
MIN_HOLD_SECONDS = 0.8
MAX_HOLD_SECONDS = 3.2

# How long the pad stays live before anyone still asleep is counted as absent.
REACTION_WINDOW_SECONDS = 5.0


@dataclass
class Contestant:
    team_id: str
    name: str
    is_bot: bool
    reaction: float | None = None   # seconds after green; None = never tapped
    jumped: float | None = None     # seconds *before* green, if they false started


@dataclass
class GreenLight:
    league_id: str
    contestants: dict[str, Contestant] = field(default_factory=dict)
    starts_at: float | None = None   # monotonic: when the countdown began
    green_at: float | None = None    # monotonic: when the pad turns green
    finished: bool = False
    seed: int = 0

    # ---- lifecycle ----------------------------------------------------
    def start(self, now: float | None = None) -> None:
        now = now if now is not None else time.monotonic()
        rng = random.Random(self.seed)
        self.starts_at = now
        hold = rng.uniform(MIN_HOLD_SECONDS, MAX_HOLD_SECONDS)
        self.green_at = now + COUNTDOWN_SECONDS + hold

    def is_green(self, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        return self.green_at is not None and now >= self.green_at

    def is_over(self, now: float | None = None) -> bool:
        """Everyone has answered, or the window has closed."""
        now = now if now is not None else time.monotonic()
        if self.green_at is None:
            return False
        if now >= self.green_at + REACTION_WINDOW_SECONDS:
            return True
        return all(
            c.reaction is not None or c.jumped is not None
            for c in self.contestants.values() if not c.is_bot
        )

    # ---- play ---------------------------------------------------------
    def tap(self, team_id: str, now: float | None = None) -> dict[str, Any]:
        """Record one tap. The server decides what it was worth, not the client."""
        now = now if now is not None else time.monotonic()
        c = self.contestants.get(team_id)
        if c is None or self.green_at is None:
            return {"ok": False}
        if c.reaction is not None or c.jumped is not None:
            return {"ok": False, "already": True}   # one tap each, first counts

        if now < self.green_at:
            c.jumped = self.green_at - now
            return {"ok": True, "false_start": True, "early_by": round(c.jumped, 3)}
        c.reaction = now - self.green_at
        return {"ok": True, "false_start": False, "reaction": round(c.reaction, 3)}

    # ---- result -------------------------------------------------------
    def standings(self) -> list[dict[str, Any]]:
        """Draft order: clean reactions, then jumpers, then no-shows, then bots.

        Ranking jumpers by how early they went puts the wildest guess last,
        which is the right incentive: a hair-trigger jump should cost more than
        a near miss.
        """
        humans = [c for c in self.contestants.values() if not c.is_bot]
        bots = [c for c in self.contestants.values() if c.is_bot]

        clean = sorted((c for c in humans if c.reaction is not None),
                       key=lambda c: c.reaction)
        jumped = sorted((c for c in humans if c.jumped is not None),
                        key=lambda c: c.jumped)          # least early first
        asleep = [c for c in humans
                  if c.reaction is None and c.jumped is None]
        random.Random(self.seed + 1).shuffle(bots)

        order: list[dict[str, Any]] = []
        for pick, c in enumerate(clean + jumped + asleep + bots, start=1):
            order.append({
                "team_id": c.team_id,
                "name": c.name,
                "is_bot": c.is_bot,
                "pick": pick,
                "reaction": round(c.reaction, 3) if c.reaction is not None else None,
                "false_start": c.jumped is not None,
                "no_show": c.reaction is None and c.jumped is None and not c.is_bot,
                # Milliseconds, so the scoreboard has something to show. A bot
                # or a no-show has no time and gets none.
                "score": int(c.reaction * 1000) if c.reaction is not None else 0,
            })
        return order

    def state(self, now: float | None = None) -> dict[str, Any]:
        now = now if now is not None else time.monotonic()
        green = self.is_green(now)
        return {
            "type": "minigame_state",
            "game": "green_light",
            "green": green,
            # Before green this is the countdown; clients must not be told when
            # green arrives, or the honest ones lose to the ones reading it.
            "counts_down": (
                max(0.0, round(self.starts_at + COUNTDOWN_SECONDS - now, 1))
                if self.starts_at is not None else None
            ),
            "finished": self.finished,
            "taps": [
                {"team_id": c.team_id, "name": c.name, "is_bot": c.is_bot,
                 "done": c.reaction is not None or c.jumped is not None,
                 "false_start": c.jumped is not None}
                for c in self.contestants.values()
            ],
        }


def build_round(
    conn: sqlite3.Connection, league_id: str, duration: float = 0.0,
    seed: int | None = None,
) -> GreenLight:
    """``duration`` is accepted and ignored: this game ends when people react."""
    rows = conn.execute(
        "SELECT id, name, is_bot FROM teams WHERE league_id = ? ORDER BY seat", (league_id,)
    ).fetchall()
    rnd = GreenLight(
        league_id=league_id,
        seed=seed if seed is not None else random.randrange(1_000_000),
    )
    for r in rows:
        rnd.contestants[r["id"]] = Contestant(
            team_id=r["id"], name=r["name"], is_bot=bool(r["is_bot"])
        )
    return rnd


def persist_results(conn: sqlite3.Connection, rnd: GreenLight) -> list[dict[str, Any]]:
    """Write reaction times and assign draft slots."""
    order = rnd.standings()
    conn.execute("DELETE FROM minigame_scores WHERE league_id = ?", (rnd.league_id,))
    for entry in order:
        conn.execute(
            "INSERT INTO minigame_scores (league_id, team_id, score, finished, tiebreak) "
            "VALUES (?,?,?,1,?)",
            (rnd.league_id, entry["team_id"], entry["score"], float(entry["pick"])),
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
    """Fallback draft-order mode: a plain shuffle, bots still at the back."""
    rng = rng or random.SystemRandom()
    rows = [dict(r) for r in conn.execute(
        "SELECT id, name, is_bot FROM teams WHERE league_id = ? ORDER BY seat", (league_id,)
    )]
    humans = [t for t in rows if not t["is_bot"]]
    bots = [t for t in rows if t["is_bot"]]
    rng.shuffle(humans)
    rng.shuffle(bots)

    result = []
    for i, t in enumerate(humans + bots, start=1):
        conn.execute(
            "UPDATE teams SET draft_slot = ? WHERE league_id = ? AND id = ?",
            (i, league_id, t["id"]),
        )
        result.append({"team_id": t["id"], "name": t["name"],
                       "is_bot": bool(t["is_bot"]), "score": 0, "pick": i})
    return result
