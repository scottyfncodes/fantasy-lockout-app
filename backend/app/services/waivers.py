"""Blind FAAB waivers.

Design constraints, all of them about keeping a replay league honest:

* **Blind bidding, not first-come-first-served.**  Nobody sees anyone else's
  bid, so being awake at the right moment is worth nothing.
* **Weekly processing only**, run at the week rollover just before lineups
  lock.  There are no same-day pickups, so a manager cannot chase a player who
  is in the middle of a big week.
* **The free-agent pool shows stats through the last simulated date only.**
  The app never surfaces full-season or future-week numbers.  It cannot erase
  what a manager personally remembers about the season — that is an honour
  system limitation the rules page states plainly — but the app itself hands
  out no hindsight.
* **Drops go through waivers.**  A dropped player sits for
  ``waiver_clear_days`` before becoming a free agent, so a manager cannot drop
  and instantly re-add someone to dodge a rival's bid.
* **Rosters freeze when the playoffs begin.** An add at that point is pure
  memory sniping, and the bracket should be decided by the team a manager
  built. ``freeze_adds_final_weeks`` optionally extends the freeze back into
  the last N weeks of the regular season.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Any

from ..config import LeagueConfig
from . import il, leagues, players as players_svc, rosters, timeline


class WaiverError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# pool
# ---------------------------------------------------------------------------

def rostered_ids(conn: sqlite3.Connection, league_id: str) -> set[str]:
    return {r["player_id"] for r in conn.execute(
        "SELECT player_id FROM rosters WHERE league_id = ?", (league_id,))}


def blocked_ids(conn: sqlite3.Connection, league_id: str, today: str) -> dict[str, str]:
    """Players still on the waiver wire (dropped, not yet cleared)."""
    rows = conn.execute(
        "SELECT player_id, clears_on FROM waiver_wire WHERE league_id = ? AND clears_on > ?",
        (league_id, today),
    ).fetchall()
    return {r["player_id"]: r["clears_on"] for r in rows}


def free_agents(
    conn: sqlite3.Connection,
    league: dict[str, Any],
    cfg: LeagueConfig,
    limit: int = 100,
    search: str | None = None,
    position: str | None = None,
    include_pending: bool = True,
) -> list[dict[str, Any]]:
    since, as_of = timeline.replay_window(conn, league, cfg)
    scoring = leagues.league_scoring(league)
    taken = rostered_ids(conn, league["id"])
    pending = blocked_ids(conn, league["id"], as_of)
    totals = players_svc.stats_through(conn, league["season_year"], scoring, as_of, since=since)
    injured = il.il_status(conn, league["season_year"], list(totals.keys()), as_of)

    pool = []
    for p in players_svc.list_players(conn, league["season_year"]):
        pid = p["player_id"]
        if pid in taken:
            continue
        if pid in pending and not include_pending:
            continue
        if search and search.lower() not in p["name"].lower():
            continue
        if position:
            allowed = set(p["positions"].split(",")) | {"P" if p["is_pitcher"] else "UTIL"}
            if position not in allowed:
                continue
        t = totals.get(pid)
        games = 0
        if t:
            games = (t["batting"]["g"] if t["batting"] else 0) or (
                t["pitching"]["g"] if t["pitching"] else 0)
        pool.append({
            **p,
            "points": t["points"] if t else 0.0,
            "games": games,
            "points_per_game": round((t["points"] / games), 2) if t and games else 0.0,
            "on_waivers_until": pending.get(pid),
            "il": injured.get(pid),
        })
    pool.sort(key=lambda r: (-r["points"], r["name"]))
    return pool[:limit]


# ---------------------------------------------------------------------------
# bidding
# ---------------------------------------------------------------------------

def adds_frozen(cfg: LeagueConfig, week: int) -> bool:
    """Are free-agent adds closed for ``week``?

    Rosters freeze when the playoffs begin. By then an add is pure memory
    sniping — the eight teams still alive know exactly who caught fire in
    September — and the bracket should be decided by the roster a manager
    built. It also closes a real hole: the weekly rollover never *processed*
    waivers in playoff weeks, so a bid placed then used to sit pending for
    ever instead of being refused.
    """
    if cfg.freeze_adds_in_playoffs and week > cfg.regular_season_weeks:
        return True
    if cfg.freeze_adds_final_weeks > 0:
        return week > cfg.regular_season_weeks - cfg.freeze_adds_final_weeks
    return False


def freeze_reason(cfg: LeagueConfig, week: int) -> str | None:
    """Why adds are closed, phrased for a manager rather than a log."""
    if not adds_frozen(cfg, week):
        return None
    if cfg.freeze_adds_in_playoffs and week > cfg.regular_season_weeks:
        return ("rosters are frozen for the playoffs — the bracket is decided by "
                "the team you built")
    return (f"free-agent adds are frozen for the final {cfg.freeze_adds_final_weeks} "
            "weeks of the regular season")


def submit_bid(
    conn: sqlite3.Connection,
    league: dict[str, Any],
    cfg: LeagueConfig,
    team_id: str,
    add_player_id: str,
    amount: int,
    drop_player_id: str | None = None,
    priority: int = 1,
) -> int:
    week = (league["current_week"] or 1) + 1  # bids process for the upcoming week
    reason = freeze_reason(cfg, week)
    if reason:
        raise WaiverError(reason)
    team = leagues.get_team(conn, league["id"], team_id)
    if team is None:
        raise WaiverError("unknown team")
    if amount < 0:
        raise WaiverError("bid cannot be negative")
    if amount > team["faab_remaining"]:
        raise WaiverError(f"bid of {amount} exceeds your remaining FAAB ({team['faab_remaining']})")

    player = players_svc.get_player(conn, league["season_year"], add_player_id)
    if player is None:
        raise WaiverError("unknown player")
    if add_player_id in rostered_ids(conn, league["id"]):
        raise WaiverError(f"{player['name']} is already on a roster")

    roster = conn.execute(
        "SELECT player_id FROM rosters WHERE league_id=? AND team_id=?",
        (league["id"], team_id),
    ).fetchall()
    if drop_player_id and drop_player_id not in {r["player_id"] for r in roster}:
        raise WaiverError("you cannot drop a player you do not roster")
    if len(roster) >= cfg.roster_size and not drop_player_id:
        raise WaiverError(f"roster is full ({cfg.roster_size}); name a player to drop")

    cur = conn.execute(
        """INSERT INTO waiver_bids (league_id, week, team_id, add_player_id,
                                    drop_player_id, amount, priority, status, created_at)
           VALUES (?,?,?,?,?,?,?,'pending',?)""",
        (league["id"], week, team_id, add_player_id, drop_player_id, amount, priority,
         dt.datetime.utcnow().isoformat(timespec="seconds")),
    )
    return int(cur.lastrowid or 0)


def cancel_bid(conn: sqlite3.Connection, league_id: str, team_id: str, bid_id: int) -> None:
    cur = conn.execute(
        "UPDATE waiver_bids SET status='cancelled' WHERE id=? AND league_id=? AND team_id=? "
        "AND status='pending'",
        (bid_id, league_id, team_id),
    )
    if cur.rowcount == 0:
        raise WaiverError("no pending bid with that id")


def my_bids(
    conn: sqlite3.Connection, league_id: str, team_id: str, week: int | None = None
) -> list[dict[str, Any]]:
    sql = ("SELECT b.*, p.name AS add_name, d.name AS drop_name FROM waiver_bids b "
           "LEFT JOIN players p ON p.player_id = b.add_player_id "
           "LEFT JOIN players d ON d.player_id = b.drop_player_id "
           "WHERE b.league_id=? AND b.team_id=?")
    params: list[Any] = [league_id, team_id]
    if week is not None:
        sql += " AND b.week = ?"
        params.append(week)
    sql += " ORDER BY b.week DESC, b.priority, b.id"
    return [dict(r) for r in conn.execute(sql, params)]


# ---------------------------------------------------------------------------
# processing
# ---------------------------------------------------------------------------

def _tiebreak_key(team: dict[str, Any]) -> tuple:
    """Ties go to the team that needs the help most: worse record, fewer points."""
    return (team["wins"], team["points_for"], team["seat"])


def process_week(
    conn: sqlite3.Connection,
    league: dict[str, Any],
    cfg: LeagueConfig,
    week: int,
    today: str | None = None,
) -> list[dict[str, Any]]:
    """Resolve all pending bids for ``week``. Highest bid wins, blind."""
    today = today or timeline.as_of_date(conn, league, cfg)
    clear_expired(conn, league["id"], today)

    bids = [dict(r) for r in conn.execute(
        "SELECT * FROM waiver_bids WHERE league_id=? AND week=? AND status='pending' "
        "ORDER BY amount DESC, id",
        (league["id"], week),
    )]
    if not bids:
        return []

    teams = {t["id"]: t for t in leagues.teams(conn, league["id"])}
    taken = rostered_ids(conn, league["id"])
    pending_wire = blocked_ids(conn, league["id"], today)
    roster_counts = {
        r["team_id"]: r["n"] for r in conn.execute(
            "SELECT team_id, COUNT(*) n FROM rosters WHERE league_id=? GROUP BY team_id",
            (league["id"],))
    }
    budget = {tid: t["faab_remaining"] for tid, t in teams.items()}
    results: list[dict[str, Any]] = []

    # Group by player: every bid on a player is compared at once, so the
    # outcome does not depend on submission order.
    by_player: dict[str, list[dict[str, Any]]] = {}
    for bid in bids:
        by_player.setdefault(bid["add_player_id"], []).append(bid)

    # Award the most-contested players first so a team's FAAB is spent where
    # the league actually competed for it.
    player_order = sorted(by_player, key=lambda pid: (-max(b["amount"] for b in by_player[pid]), pid))

    for player_id in player_order:
        contenders = sorted(
            by_player[player_id],
            key=lambda b: (-b["amount"], _tiebreak_key(teams[b["team_id"]])),
        )
        awarded = False
        for bid in contenders:
            team_id = bid["team_id"]
            reason = None
            if awarded:
                reason = "outbid"
            elif player_id in taken:
                reason = "player was claimed by another bid this run"
            elif player_id in pending_wire:
                reason = f"player is on waivers until {pending_wire[player_id]}"
            elif bid["amount"] > budget[team_id]:
                reason = "not enough FAAB left after earlier claims"
            elif bid["drop_player_id"] and bid["drop_player_id"] not in _team_player_ids(
                    conn, league["id"], team_id):
                reason = "the player you offered to drop is no longer on your roster"
            elif not bid["drop_player_id"] and roster_counts.get(team_id, 0) >= cfg.roster_size:
                reason = "roster full and no drop specified"

            if reason:
                _finish_bid(conn, bid["id"], "lost" if reason == "outbid" else "invalid", reason)
                results.append({**bid, "status": "lost", "reason": reason})
                continue

            _award(conn, league, cfg, bid, week)
            budget[team_id] -= bid["amount"]
            taken.add(player_id)
            if bid["drop_player_id"]:
                taken.discard(bid["drop_player_id"])
                pending_wire[bid["drop_player_id"]] = _clears_on(today, cfg)
            else:
                roster_counts[team_id] = roster_counts.get(team_id, 0) + 1
            results.append({**bid, "status": "won", "reason": None})
            awarded = True
    return results


def _team_player_ids(conn: sqlite3.Connection, league_id: str, team_id: str) -> set[str]:
    return {r["player_id"] for r in conn.execute(
        "SELECT player_id FROM rosters WHERE league_id=? AND team_id=?", (league_id, team_id))}


def _clears_on(today: str, cfg: LeagueConfig) -> str:
    return (dt.date.fromisoformat(today) + dt.timedelta(days=cfg.waiver_clear_days)).isoformat()


def _finish_bid(conn: sqlite3.Connection, bid_id: int, status: str, reason: str | None) -> None:
    conn.execute(
        "UPDATE waiver_bids SET status=?, reason=?, processed_at=? WHERE id=?",
        (status, reason, dt.datetime.utcnow().isoformat(timespec="seconds"), bid_id),
    )


def _award(
    conn: sqlite3.Connection, league: dict[str, Any], cfg: LeagueConfig,
    bid: dict[str, Any], week: int,
) -> None:
    team_id = bid["team_id"]
    if bid["drop_player_id"]:
        drop_to_waivers(conn, league, cfg, team_id, bid["drop_player_id"], week)
    conn.execute(
        "INSERT INTO rosters (league_id, team_id, player_id, acquired_week, acquired_via) "
        "VALUES (?,?,?,?,'faab')",
        (league["id"], team_id, bid["add_player_id"], week),
    )
    conn.execute(
        "UPDATE teams SET faab_remaining = faab_remaining - ? WHERE id = ?",
        (bid["amount"], team_id),
    )
    conn.execute("DELETE FROM waiver_wire WHERE league_id=? AND player_id=?",
                 (league["id"], bid["add_player_id"]))
    _finish_bid(conn, bid["id"], "won", None)
    leagues.log(conn, league["id"], week, "faab_win", team_id, bid["add_player_id"],
                f"won for {bid['amount']} FAAB")


def drop_to_waivers(
    conn: sqlite3.Connection, league: dict[str, Any], cfg: LeagueConfig,
    team_id: str, player_id: str, week: int, today: str | None = None,
) -> str:
    """Remove a player from a roster; he sits on waivers before clearing."""
    today = today or timeline.as_of_date(conn, league, cfg)
    cur = conn.execute(
        "DELETE FROM rosters WHERE league_id=? AND team_id=? AND player_id=?",
        (league["id"], team_id, player_id),
    )
    if cur.rowcount == 0:
        raise WaiverError("that player is not on your roster")
    clears = _clears_on(today, cfg)
    conn.execute(
        "INSERT OR REPLACE INTO waiver_wire (league_id, player_id, dropped_by, dropped_on, clears_on) "
        "VALUES (?,?,?,?,?)",
        (league["id"], player_id, team_id, today, clears),
    )
    conn.execute(
        "DELETE FROM lineups WHERE league_id=? AND team_id=? AND player_id=? AND week >= ?",
        (league["id"], team_id, player_id, week),
    )
    leagues.log(conn, league["id"], week, "drop", team_id, player_id, f"clears {clears}")
    return clears


def clear_expired(conn: sqlite3.Connection, league_id: str, today: str) -> int:
    cur = conn.execute(
        "DELETE FROM waiver_wire WHERE league_id=? AND clears_on <= ?", (league_id, today)
    )
    return cur.rowcount


def summary(
    conn: sqlite3.Connection, league: dict[str, Any], cfg: LeagueConfig, week: int
) -> dict[str, Any]:
    rows = conn.execute(
        """SELECT b.*, t.name AS team_name, p.name AS add_name, d.name AS drop_name
             FROM waiver_bids b
             JOIN teams t ON t.id = b.team_id
             LEFT JOIN players p ON p.player_id = b.add_player_id AND p.season = ?
             LEFT JOIN players d ON d.player_id = b.drop_player_id AND d.season = ?
            WHERE b.league_id = ? AND b.week = ? AND b.status IN ('won','lost','invalid')
            ORDER BY b.amount DESC, b.id""",
        (league["season_year"], league["season_year"], league["id"], week),
    ).fetchall()
    # Losing bid amounts are revealed only after processing, which is standard
    # FAAB practice and keeps the blind period genuinely blind.
    return {
        "week": week,
        "frozen": adds_frozen(cfg, week),
        "frozen_reason": freeze_reason(cfg, week),
        "results": [dict(r) for r in rows],
    }


def roster_space(
    conn: sqlite3.Connection, league: dict[str, Any], cfg: LeagueConfig, team_id: str
) -> dict[str, Any]:
    count = conn.execute(
        "SELECT COUNT(*) n FROM rosters WHERE league_id=? AND team_id=?",
        (league["id"], team_id),
    ).fetchone()["n"]
    roster = [dict(r) for r in conn.execute(
        """SELECT p.player_id, p.name, p.positions, p.is_pitcher FROM rosters r
             JOIN players p ON p.player_id = r.player_id AND p.season = ?
            WHERE r.league_id=? AND r.team_id=?""",
        (league["season_year"], league["id"], team_id))]
    return {
        "roster_size": cfg.roster_size,
        "used": count,
        "open": max(0, cfg.roster_size - count),
        "gaps": rosters.unfilled_slots(roster, cfg.active_slots),
    }
