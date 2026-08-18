"""Bot managers.

**Integrity rule — read before changing anything here.**

A replay league has a unique failure mode: the whole season already happened,
so any agent with access to the full-season data can set a perfect lineup every
week and pick up exactly the player who is about to get hot.  A bot that did
that would be unbeatable, and not in an interesting way.

So bots are split down the middle:

* **Drafting** may use full-season production.  The draft happens before the
  replay starts and every human is choosing from the same finished season, so
  this is symmetric information, not hindsight.
* **Lineups and waivers** may use *only* production through the last simulated
  date — the same cut-off the UI enforces for human managers.  Every function
  below that runs in-season goes through :func:`lineups.pre_week_ranking` or
  :func:`players.stats_through`, never :func:`players.season_totals`.

``tests/test_bot_integrity.py`` pins this down.
"""

from __future__ import annotations

import random
import sqlite3
from typing import Any

from ..config import LeagueConfig
from . import draft as draft_svc, leagues, lineups, players as players_svc, rosters, waivers

# Scarce positions are worth reaching for; four P slots and three UTIL slots
# mean generic bats and arms are replaceable.
SCARCITY = {"C": 1.35, "SS": 1.15, "2B": 1.10, "3B": 1.06, "1B": 1.0, "OF": 1.0,
            "SP": 1.08, "RP": 1.12, "UTIL": 1.0, "P": 1.0}


# ---------------------------------------------------------------------------
# draft
# ---------------------------------------------------------------------------

def choose_draft_pick(
    conn: sqlite3.Connection,
    league: dict[str, Any],
    cfg: LeagueConfig,
    team_id: str,
    pool: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """Best available, weighted by what the roster still needs."""
    scoring = leagues.league_scoring(league)
    pool = pool if pool is not None else draft_svc.available(conn, league, scoring, limit=120)
    if not pool:
        return None

    roster = draft_svc.team_roster(conn, league, team_id)
    picks_left_after = cfg.roster_size - len(roster) - 1
    gaps = set(rosters.unfilled_slots(roster, cfg.active_slots))

    # Feasibility only binds near the end of the draft; checking every
    # candidate on every pick would run the matching thousands of times for
    # nothing. The slack is generous on purpose: engaging late lets a bot keep
    # taking the best bat available until the only way to fill its holes is a
    # pick it no longer has, and a roster that arrives at the last round two
    # slots short cannot be rescued by any single player.
    tight = picks_left_after <= len(gaps) + 6

    if tight and gaps:
        # "Best available" is ranked by points, so the top of the board can be
        # entirely the wrong positions — a team needing a catcher may find none
        # in the top 120. Pull a position-filtered slice for each open slot so a
        # legal pick is always in front of us when one exists.
        seen = {p["player_id"] for p in pool}
        for slot in sorted(gaps):
            for candidate in draft_svc.available(conn, league, scoring, limit=25, position=slot):
                if candidate["player_id"] not in seen:
                    seen.add(candidate["player_id"])
                    pool = pool + [candidate]

    best, best_score = None, float("-inf")
    for candidate in (pool if tight else pool[:80]):
        if tight:
            ok, _why = rosters.draft_feasible(roster, candidate, cfg.active_slots, picks_left_after)
            if not ok:
                continue
        slots = players_svc.eligible_slots(candidate, cfg.active_slots.keys())
        weight = max((SCARCITY.get(s, 1.0) for s in slots), default=1.0)
        fills_gap = bool(gaps & set(slots))
        score = candidate["points"] * weight * (1.25 if fills_gap else 1.0)
        if score > best_score:
            best, best_score = candidate, score

    if best is None:
        # Every top-ranked option would break roster feasibility; fall back to
        # anyone who fills a hole.
        for candidate in pool:
            ok, _ = rosters.draft_feasible(roster, candidate, cfg.active_slots, picks_left_after)
            if ok:
                return candidate

        # Nothing is feasible: this roster can no longer fill every active slot
        # however the rest of the draft goes. Returning None here is what froze
        # a live draft at pick 314 of 320 — the room simply stopped, because a
        # bot would rather pick nothing than pick badly. It is the wrong
        # priority. An imperfect roster costs one manager a slot; a stalled
        # draft costs everybody the season. Take the best player left and let
        # the lineup screen show the hole.
        if pool:
            return max(pool, key=lambda c: c["points"])
    return best


def autopick(
    conn: sqlite3.Connection, league: dict[str, Any], cfg: LeagueConfig, team_id: str
) -> dict[str, Any] | None:
    choice = choose_draft_pick(conn, league, cfg, team_id)
    if choice is None:
        return None
    return draft_svc.make_pick(conn, league, team_id, choice["player_id"], auto=True)


# ---------------------------------------------------------------------------
# lineups
# ---------------------------------------------------------------------------

def set_all_bot_lineups(
    conn: sqlite3.Connection, league: dict[str, Any], cfg: LeagueConfig, week: int
) -> list[str]:
    """Auto-fill every bot's lineup using pre-week information only."""
    done = []
    for team in leagues.teams(conn, league["id"]):
        if not team["is_bot"]:
            continue
        if lineups.is_locked(conn, league["id"], team["id"], week):
            continue
        lineups.autofill(conn, league, team["id"], week, cfg)
        done.append(team["id"])
    return done


# ---------------------------------------------------------------------------
# waivers
# ---------------------------------------------------------------------------

def submit_bot_bids(
    conn: sqlite3.Connection,
    league: dict[str, Any],
    cfg: LeagueConfig,
    week: int,
    rng: random.Random | None = None,
    max_bids: int = 2,
) -> list[int]:
    """Bots bid FAAB on the same restricted view of the pool humans see.

    ``free_agents`` is capped at the last simulated date, so a bot cannot bid on
    a player because of what he is about to do — only because of what he has
    already done.
    """
    if not cfg.bots_use_waivers or waivers.adds_frozen(cfg, week):
        return []
    rng = rng or random.Random(f"{league['id']}:{week}")
    pool = waivers.free_agents(conn, league, cfg, limit=60, include_pending=False)
    pool = [p for p in pool if p["games"] >= 5 and not p["il"]]
    if not pool:
        return []

    submitted: list[int] = []
    for team in leagues.teams(conn, league["id"]):
        if not team["is_bot"] or team["faab_remaining"] <= 0:
            continue
        roster = lineups.roster_players(conn, league, team["id"])
        ranking = lineups.pre_week_ranking(conn, league, cfg, week)
        weakest = sorted(roster, key=lambda p: ranking.get(p["player_id"], 0.0))
        space = waivers.roster_space(conn, league, cfg, team["id"])

        picks = [p for p in pool if p["points_per_game"] > 0][: 12]
        rng.shuffle(picks)
        for candidate in picks[:max_bids]:
            replaces = None
            if space["open"] <= 0:
                if not weakest:
                    break
                replaces = weakest[0]["player_id"]
                if ranking.get(replaces, 0.0) >= candidate["points_per_game"]:
                    continue  # the bot's worst player is still better
            # Spend a slice of what is left, scaled by how good the target is.
            share = min(0.35, 0.04 + candidate["points_per_game"] / 60.0)
            amount = max(1, int(team["faab_remaining"] * share * rng.uniform(0.6, 1.2)))
            amount = min(amount, team["faab_remaining"])
            try:
                bid_id = waivers.submit_bid(
                    conn, {**league, "current_week": week - 1}, cfg, team["id"],
                    candidate["player_id"], amount, drop_player_id=replaces,
                )
            except waivers.WaiverError:
                continue
            submitted.append(bid_id)
            if replaces:
                break  # one swap per bot per week is plenty
    return submitted
