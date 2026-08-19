"""The replay engine: one real day at a time.

Every night the scheduler advances each live league by one day of the replayed
season.  For that date we pull the real box-score lines of every player sitting
in an active lineup slot, score them with the league's config, and bank the
points against that week's head-to-head matchup.

Week rollover (crossing into a new fantasy Monday) does the housekeeping in a
fixed order:

    close last week -> build the next playoff round if we just left the regular
    season -> bots submit FAAB bids -> waivers process -> lineups auto-fill and
    lock for the new week

Waivers deliberately run *before* the new week's lineups lock, so a claim you
win is a player you can actually start.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from typing import Any

from ..config import LeagueConfig
from ..scoring import score_batting, score_day, score_pitching
from . import bots, leagues, lineups, schedule as schedule_svc, standings, timeline, waivers

ACTIVE_EXCLUDED = {lineups.BENCH, lineups.IL_SLOT}


def start_season(conn: sqlite3.Connection, league: dict[str, Any], cfg: LeagueConfig) -> None:
    standings.create_regular_season(conn, league, cfg)
    conn.execute(
        "UPDATE leagues SET phase='season', current_week=1, last_simulated_date=NULL WHERE id=?",
        (league["id"],),
    )
    league = leagues.require_league(conn, league["id"])
    bots.set_all_bot_lineups(conn, league, cfg, week=1)
    lineups.lock_week(conn, league, cfg, 1)


# ---------------------------------------------------------------------------
# one day
# ---------------------------------------------------------------------------

def simulate_date(
    conn: sqlite3.Connection, league: dict[str, Any], cfg: LeagueConfig, day: dt.date
) -> dict[str, Any]:
    season = league["season_year"]
    scoring = leagues.league_scoring(league)
    iso = day.isoformat()
    week_obj = timeline.week_containing(conn, season, cfg, day)
    if week_obj is None:
        return {"date": iso, "skipped": "not part of the fantasy calendar", "points": 0.0}

    bat = {r["player_id"]: dict(r) for r in conn.execute(
        "SELECT * FROM batting_lines WHERE season=? AND date=?", (season, iso))}
    pit = {r["player_id"]: dict(r) for r in conn.execute(
        "SELECT * FROM pitching_lines WHERE season=? AND date=?", (season, iso))}

    rows = conn.execute(
        "SELECT team_id, player_id, slot FROM lineups WHERE league_id=? AND week=?",
        (league["id"], week_obj.week),
    ).fetchall()

    conn.execute("DELETE FROM scoring_lines WHERE league_id=? AND date=?", (league["id"], iso))
    inserts: list[tuple] = []
    highlights: list[dict[str, Any]] = []
    total = 0.0
    for r in rows:
        if r["slot"] in ACTIVE_EXCLUDED:
            continue
        b, p = bat.get(r["player_id"]), pit.get(r["player_id"])
        if not b and not p:
            continue
        line = None
        if b:
            line = score_batting(b, scoring)
        if p:
            scored_p = score_pitching(p, scoring)
            line = scored_p if line is None else line.merge(scored_p)
        if line is None or (line.points == 0 and not line.breakdown):
            continue
        inserts.append((league["id"], week_obj.week, iso, r["team_id"], r["player_id"],
                        r["slot"], line.points, json.dumps(line.breakdown)))
        total += line.points
        for bonus in ("SLAM",):
            if bonus in line.breakdown:
                highlights.append({"player_id": r["player_id"], "team_id": r["team_id"],
                                   "bonus": bonus, "points": line.breakdown[bonus]})

    conn.executemany(
        """INSERT OR REPLACE INTO scoring_lines
           (league_id, week, date, team_id, player_id, slot, points, breakdown_json)
           VALUES (?,?,?,?,?,?,?,?)""",
        inserts,
    )
    _refresh_week_points(conn, league, week_obj.week)
    conn.execute("UPDATE leagues SET last_simulated_date=? WHERE id=?", (iso, league["id"]))
    return {"date": iso, "week": week_obj.week, "scored_players": len(inserts),
            "points": round(total, 2), "highlights": highlights}


def _refresh_week_points(conn: sqlite3.Connection, league: dict[str, Any], week: int) -> None:
    """Roll the week's scoring lines up into that week's matchup totals."""
    totals = {r["team_id"]: r["pts"] for r in conn.execute(
        "SELECT team_id, ROUND(SUM(points), 2) pts FROM scoring_lines "
        "WHERE league_id=? AND week=? GROUP BY team_id",
        (league["id"], week))}
    for m in conn.execute(
        "SELECT slot, home_team_id, away_team_id FROM matchups WHERE league_id=? AND week=?",
        (league["id"], week),
    ).fetchall():
        conn.execute(
            "UPDATE matchups SET home_points=?, away_points=? WHERE league_id=? AND week=? AND slot=?",
            (totals.get(m["home_team_id"], 0.0), totals.get(m["away_team_id"], 0.0),
             league["id"], week, m["slot"]),
        )


# ---------------------------------------------------------------------------
# week close
# ---------------------------------------------------------------------------

def close_week(
    conn: sqlite3.Connection, league: dict[str, Any], cfg: LeagueConfig, week: int
) -> list[dict[str, Any]]:
    _refresh_week_points(conn, league, week)
    plan = {s["stage"]: s for s in schedule_svc.playoff_week_plan(cfg)}
    results: list[dict[str, Any]] = []

    for m in conn.execute(
        "SELECT * FROM matchups WHERE league_id=? AND week=? AND complete=0",
        (league["id"], week),
    ).fetchall():
        stage = m["stage"]
        weeks_in_series = plan.get(stage, {}).get("weeks", [week]) if stage != "regular" else [week]
        is_last_leg = week == weeks_in_series[-1]

        conn.execute(
            "UPDATE matchups SET complete=1 WHERE league_id=? AND week=? AND slot=?",
            (league["id"], week, m["slot"]),
        )
        if stage == "regular":
            home, away = m["home_points"], m["away_points"]
            winner = m["home_team_id"] if home > away else (m["away_team_id"] if away > home else None)
            _apply_regular_result(conn, m, winner)
            results.append({"week": week, "stage": stage, "home": m["home_team_id"],
                            "away": m["away_team_id"], "home_points": home,
                            "away_points": away, "winner": winner})
        elif is_last_leg:
            # Series total across every leg (the final spans two weeks).
            agg = conn.execute(
                "SELECT SUM(home_points) h, SUM(away_points) a FROM matchups "
                "WHERE league_id=? AND stage=? AND slot=?",
                (league["id"], stage, m["slot"]),
            ).fetchone()
            home_total, away_total = agg["h"] or 0.0, agg["a"] or 0.0
            winner = m["home_team_id"] if home_total >= away_total else m["away_team_id"]
            conn.execute(
                "UPDATE matchups SET winner_team_id=? WHERE league_id=? AND stage=? AND slot=?",
                (winner, league["id"], stage, m["slot"]),
            )
            loser = m["away_team_id"] if winner == m["home_team_id"] else m["home_team_id"]
            conn.execute("UPDATE teams SET eliminated=1 WHERE id=?", (loser,))
            results.append({"week": week, "stage": stage, "home": m["home_team_id"],
                            "away": m["away_team_id"], "home_points": round(home_total, 2),
                            "away_points": round(away_total, 2), "winner": winner})
    return results


def _apply_regular_result(conn: sqlite3.Connection, m: sqlite3.Row, winner: str | None) -> None:
    conn.execute(
        "UPDATE matchups SET winner_team_id=? WHERE league_id=? AND week=? AND slot=?",
        (winner, m["league_id"], m["week"], m["slot"]),
    )
    for team_id, points in ((m["home_team_id"], m["home_points"]),
                            (m["away_team_id"], m["away_points"])):
        if not team_id:
            continue
        if winner is None:
            conn.execute("UPDATE teams SET ties=ties+1, points_for=points_for+? WHERE id=?",
                         (points, team_id))
        elif team_id == winner:
            conn.execute("UPDATE teams SET wins=wins+1, points_for=points_for+? WHERE id=?",
                         (points, team_id))
        else:
            conn.execute("UPDATE teams SET losses=losses+1, points_for=points_for+? WHERE id=?",
                         (points, team_id))


# ---------------------------------------------------------------------------
# rollover + advance
# ---------------------------------------------------------------------------

def rollover(
    conn: sqlite3.Connection, league: dict[str, Any], cfg: LeagueConfig, new_week: int
) -> dict[str, Any]:
    events: dict[str, Any] = {"closed": [], "playoffs": None, "waivers": [], "locked": []}
    for week in range(league["current_week"] or 1, new_week):
        events["closed"].extend(close_week(conn, league, cfg, week))

    plan = schedule_svc.playoff_week_plan(cfg)
    for index, stage in enumerate(plan, start=1):
        if stage["weeks"][0] == new_week:
            if index == 1:
                leagues.set_phase(conn, league["id"], "playoffs")
            league = leagues.require_league(conn, league["id"])
            events["playoffs"] = standings.build_playoff_round(conn, league, cfg, index)
            break

    conn.execute("UPDATE leagues SET current_week=? WHERE id=?", (new_week, league["id"]))
    league = leagues.require_league(conn, league["id"])

    if new_week <= cfg.regular_season_weeks:
        if cfg.bots_use_waivers:
            bots.submit_bot_bids(conn, league, cfg, new_week)
        events["waivers"] = waivers.process_week(conn, league, cfg, new_week)

    bots.set_all_bot_lineups(conn, league, cfg, new_week)
    events["locked"] = lineups.lock_week(conn, league, cfg, new_week)
    return events


def advance_day(
    conn: sqlite3.Connection, league: dict[str, Any], cfg: LeagueConfig
) -> dict[str, Any]:
    """Process the next unplayed day. This is what the 8pm CST job calls."""
    if league["phase"] not in ("season", "playoffs"):
        return {"status": "idle", "reason": f"league is in the {league['phase']} phase"}

    day = timeline.next_sim_date(conn, league, cfg)
    if day is None:
        return finish_season(conn, league, cfg)

    week_obj = timeline.week_containing(conn, league["season_year"], cfg, day)
    rolled = None
    if week_obj and week_obj.week != (league["current_week"] or 1):
        rolled = rollover(conn, league, cfg, week_obj.week)
        league = leagues.require_league(conn, league["id"])

    result = simulate_date(conn, league, cfg, day)
    result["rollover"] = rolled
    result["status"] = "advanced"
    return result


def finish_season(
    conn: sqlite3.Connection, league: dict[str, Any], cfg: LeagueConfig
) -> dict[str, Any]:
    for week in range(1, cfg.total_weeks + 1):
        close_week(conn, league, cfg, week)
    leagues.set_phase(conn, league["id"], "complete")
    league = leagues.require_league(conn, league["id"])
    return {"status": "complete", "champion": standings.champion(conn, league, cfg)}


def catch_up(
    conn: sqlite3.Connection, league: dict[str, Any], cfg: LeagueConfig, days: int
) -> list[dict[str, Any]]:
    """Advance several days at once (commissioner tool / test harness)."""
    out = []
    for _ in range(days):
        league = leagues.require_league(conn, league["id"])
        step = advance_day(conn, league, cfg)
        out.append(step)
        if step.get("status") == "complete":
            break
    return out


# ---------------------------------------------------------------------------
# recaps
# ---------------------------------------------------------------------------

def week_recap(
    conn: sqlite3.Connection, league: dict[str, Any], cfg: LeagueConfig, week: int
) -> dict[str, Any]:
    names = {t["id"]: t["name"] for t in leagues.teams(conn, league["id"])}
    matchups = [dict(r) for r in conn.execute(
        "SELECT * FROM matchups WHERE league_id=? AND week=? ORDER BY slot", (league["id"], week))]

    top_rows = conn.execute(
        """SELECT s.team_id, s.player_id, s.slot, SUM(s.points) pts, p.name, p.positions
             FROM scoring_lines s
             JOIN players p ON p.player_id = s.player_id AND p.season = ?
            WHERE s.league_id = ? AND s.week = ?
            GROUP BY s.team_id, s.player_id
            ORDER BY pts DESC""",
        (league["season_year"], league["id"], week),
    ).fetchall()

    by_team: dict[str, list[dict[str, Any]]] = {}
    for r in top_rows:
        by_team.setdefault(r["team_id"], []).append(
            {"player_id": r["player_id"], "name": r["name"], "positions": r["positions"],
             "slot": r["slot"], "points": round(r["pts"], 2)})

    bonuses = []
    for r in conn.execute(
        """SELECT s.team_id, s.player_id, s.date, s.breakdown_json, p.name
             FROM scoring_lines s
             JOIN players p ON p.player_id = s.player_id AND p.season = ?
            WHERE s.league_id = ? AND s.week = ?""",
        (league["season_year"], league["id"], week),
    ):
        breakdown = json.loads(r["breakdown_json"])
        for key, label in (("SLAM", "grand slam"),):
            if key in breakdown:
                bonuses.append({"team": names.get(r["team_id"]), "player": r["name"],
                                "date": r["date"], "bonus": key, "label": label,
                                "points": breakdown[key]})

    week_obj = timeline.week(conn, league["season_year"], cfg, week)
    return {
        "week": week, "label": week_obj.label,
        "start": week_obj.start.isoformat(), "end": week_obj.end.isoformat(),
        "matchups": [
            {**m,
             "home_name": names.get(m["home_team_id"]), "away_name": names.get(m["away_team_id"]),
             "home_top": by_team.get(m["home_team_id"], [])[:5],
             "away_top": by_team.get(m["away_team_id"], [])[:5]}
            for m in matchups
        ],
        "bonuses": sorted(bonuses, key=lambda b: -b["points"]),
        "top_performers": [
            {**{k: v for k, v in row.items() if k != "team_id"},
             "team": names.get(row["team_id"])}
            for row in [
                {"team_id": r["team_id"], "player_id": r["player_id"], "name": r["name"],
                 "positions": r["positions"], "points": round(r["pts"], 2)}
                for r in top_rows[:12]
            ]
        ],
    }


# ---------------------------------------------------------------------------
# last night
# ---------------------------------------------------------------------------

def replayed_dates(
    conn: sqlite3.Connection, league: dict[str, Any], cfg: LeagueConfig
) -> list[str]:
    """Every date the replay has actually played, oldest first.

    Built from the fantasy calendar rather than from ``scoring_lines`` so a day
    on which nobody scored is still a day you can look at — an empty Monday is
    a real result, not a missing one.
    """
    if not league["season_year"]:
        return []
    through = dt.date.fromisoformat(timeline.as_of_date(conn, league, cfg))
    out: list[str] = []
    for w in timeline.calendar(conn, league["season_year"], cfg):
        day = w.start
        while day <= w.end and day <= through:
            out.append(day.isoformat())
            day += dt.timedelta(days=1)
    return out


def day_recap(
    conn: sqlite3.Connection, league: dict[str, Any], cfg: LeagueConfig,
    day: dt.date | None = None,
) -> dict[str, Any]:
    """One replayed date, from every manager's point of view.

    The league simulates a day a night, so this is the view most managers open
    first: what my starters did, what the bench did instead, and which way the
    week's matchup moved because of it.
    """
    dates = replayed_dates(conn, league, cfg)
    if not dates:
        return {"date": None, "dates_played": 0, "teams": [], "matchups": [],
                "top_performers": [], "bonuses": []}

    iso = day.isoformat() if day else dates[-1]
    if iso not in dates:
        raise LookupError(f"{iso} has not been replayed yet")
    index = dates.index(iso)
    season = league["season_year"]
    week_obj = timeline.week_containing(conn, season, cfg, dt.date.fromisoformat(iso))
    week = week_obj.week if week_obj else 0
    teams = leagues.teams(conn, league["id"])
    names = {t["id"]: t["name"] for t in teams}

    started: dict[str, list[dict[str, Any]]] = {}
    bonuses: list[dict[str, Any]] = []
    performers: list[dict[str, Any]] = []
    for r in conn.execute(
        """SELECT s.team_id, s.player_id, s.slot, s.points, s.breakdown_json,
                  p.name, p.positions, p.mlb_team
             FROM scoring_lines s
             JOIN players p ON p.player_id = s.player_id AND p.season = ?
            WHERE s.league_id = ? AND s.date = ?
            ORDER BY s.points DESC""",
        (season, league["id"], iso),
    ):
        breakdown = json.loads(r["breakdown_json"])
        entry = {"player_id": r["player_id"], "name": r["name"], "positions": r["positions"],
                 "mlb_team": r["mlb_team"], "slot": r["slot"], "points": round(r["points"], 2),
                 "breakdown": breakdown}
        started.setdefault(r["team_id"], []).append(entry)
        performers.append({**entry, "team": names.get(r["team_id"])})
        for key, label in (("SLAM", "hit a grand slam"),):
            if key in breakdown:
                bonuses.append({"team": names.get(r["team_id"]), "player": r["name"],
                                "bonus": key, "label": label, "points": breakdown[key]})

    bench = _bench_day(conn, league, cfg, iso, week)

    day_points = {tid: round(sum(e["points"] for e in rows), 2) for tid, rows in started.items()}
    return {
        "date": iso,
        "week": week,
        "week_label": week_obj.label if week_obj else None,
        "prev": dates[index - 1] if index > 0 else None,
        "next": dates[index + 1] if index + 1 < len(dates) else None,
        "is_latest": index == len(dates) - 1,
        "day_number": index + 1,
        "dates_played": len(dates),
        "active_slots": sum(cfg.active_slots.values()),
        "league_points": round(sum(day_points.values()), 2),
        "matchups": _day_matchups(conn, league, week, day_points, names),
        "teams": sorted(
            [{"team_id": t["id"], "name": t["name"], "is_bot": bool(t["is_bot"]),
              "points": day_points.get(t["id"], 0.0),
              "started": started.get(t["id"], []),
              "bench": bench.get(t["id"], [])}
             for t in teams],
            key=lambda t: -t["points"],
        ),
        "top_performers": performers[:12],
        "bonuses": sorted(bonuses, key=lambda b: -b["points"]),
    }


def _bench_day(
    conn: sqlite3.Connection, league: dict[str, Any], cfg: LeagueConfig, iso: str, week: int
) -> dict[str, list[dict[str, Any]]]:
    """What each team's benched players did on a date nobody started them.

    This is hindsight a manager is allowed — the day has already been replayed,
    and every rival can see the same thing.  ``acquired_week`` keeps a player
    picked up later from showing on a bench he was not on yet; a player since
    dropped is simply absent, which is the honest limit of a roster table that
    stores the present.
    """
    scoring = leagues.league_scoring(league)
    season = league["season_year"]
    bat = {r["player_id"]: dict(r) for r in conn.execute(
        "SELECT * FROM batting_lines WHERE season=? AND date=?", (season, iso))}
    pit = {r["player_id"]: dict(r) for r in conn.execute(
        "SELECT * FROM pitching_lines WHERE season=? AND date=?", (season, iso))}
    if not bat and not pit:
        return {}

    out: dict[str, list[dict[str, Any]]] = {}
    for r in conn.execute(
        """SELECT r.team_id, r.player_id, p.name, p.positions, p.mlb_team,
                  COALESCE(l.slot, ?) AS slot
             FROM rosters r
             JOIN players p ON p.player_id = r.player_id AND p.season = ?
             LEFT JOIN lineups l ON l.league_id = r.league_id AND l.team_id = r.team_id
                                AND l.player_id = r.player_id AND l.week = ?
            WHERE r.league_id = ? AND r.acquired_week <= ?""",
        (lineups.BENCH, season, week, league["id"], week),
    ):
        if r["slot"] not in ACTIVE_EXCLUDED:
            continue
        b, p = bat.get(r["player_id"]), pit.get(r["player_id"])
        if not b and not p:
            continue
        line = score_day(b, p, scoring)
        if line.points == 0 and not line.breakdown:
            continue
        out.setdefault(r["team_id"], []).append(
            {"player_id": r["player_id"], "name": r["name"], "positions": r["positions"],
             "mlb_team": r["mlb_team"], "slot": r["slot"], "points": round(line.points, 2),
             "breakdown": line.breakdown})
    for rows in out.values():
        rows.sort(key=lambda e: -e["points"])
    return out


def _day_matchups(
    conn: sqlite3.Connection, league: dict[str, Any], week: int,
    day_points: dict[str, float], names: dict[str, str],
) -> list[dict[str, Any]]:
    """The week's matchups with the day's swing broken out of the running total."""
    return [
        {"slot": m["slot"], "stage": m["stage"], "complete": bool(m["complete"]),
         "home_team_id": m["home_team_id"], "home_name": names.get(m["home_team_id"]),
         "home_day": day_points.get(m["home_team_id"], 0.0), "home_week": m["home_points"],
         "away_team_id": m["away_team_id"], "away_name": names.get(m["away_team_id"]),
         "away_day": day_points.get(m["away_team_id"], 0.0), "away_week": m["away_points"]}
        for m in conn.execute(
            "SELECT * FROM matchups WHERE league_id=? AND week=? ORDER BY slot",
            (league["id"], week))
    ]
