"""The replay's clock: fantasy weeks, real dates, and where we are right now."""

from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Any

from ..config import LeagueConfig
from ..season_calendar import FantasyWeek, build_calendar, week_for_date


def season_row(conn: sqlite3.Connection, year: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM seasons WHERE year = ?", (year,)).fetchone()
    if row is None:
        raise LookupError(f"season {year} is not cached — run the data pipeline first")
    return dict(row)


def calendar(conn: sqlite3.Connection, year: int, cfg: LeagueConfig) -> list[FantasyWeek]:
    season = season_row(conn, year)
    return build_calendar(
        opening_day=dt.date.fromisoformat(season["opening_day"]),
        total_weeks=cfg.total_weeks,
        regular_season_weeks=cfg.regular_season_weeks,
        all_star_monday=dt.date.fromisoformat(season["all_star_monday"]),
    )


def week(conn: sqlite3.Connection, year: int, cfg: LeagueConfig, number: int) -> FantasyWeek:
    for w in calendar(conn, year, cfg):
        if w.week == number:
            return w
    raise LookupError(f"week {number} is outside this season's {cfg.total_weeks}-week calendar")


def week_containing(
    conn: sqlite3.Connection, year: int, cfg: LeagueConfig, day: dt.date
) -> FantasyWeek | None:
    return week_for_date(calendar(conn, year, cfg), day)


def next_sim_date(conn: sqlite3.Connection, league: dict[str, Any], cfg: LeagueConfig) -> dt.date | None:
    """The next real date the nightly job should process, or None when done."""
    cal = calendar(conn, league["season_year"], cfg)
    first, last = cal[0].start, cal[-1].end
    if league["last_simulated_date"]:
        candidate = dt.date.fromisoformat(league["last_simulated_date"]) + dt.timedelta(days=1)
    else:
        candidate = first
    while candidate <= last:
        if week_for_date(cal, candidate):
            return candidate
        candidate += dt.timedelta(days=1)  # skip the All-Star week
    return None


def week_bounds(conn: sqlite3.Connection, year: int, cfg: LeagueConfig, number: int) -> tuple[str, str]:
    w = week(conn, year, cfg, number)
    return w.start.isoformat(), w.end.isoformat()


def as_of_date(conn: sqlite3.Connection, league: dict[str, Any], cfg: LeagueConfig) -> str:
    """The last real date the replay has processed — the visibility cut-off.

    Every in-season stat view is capped here.  Before the first sim runs it is
    the day before Opening Day, so the free-agent pool starts genuinely blank.
    """
    if league["last_simulated_date"]:
        return league["last_simulated_date"]
    cal = calendar(conn, league["season_year"], cfg)
    return (cal[0].start - dt.timedelta(days=1)).isoformat()


def replay_window(
    conn: sqlite3.Connection, league: dict[str, Any], cfg: LeagueConfig
) -> tuple[str, str]:
    """``(since, through)`` — the only games an in-season stat view may show.

    ``since`` is the Monday week 1 starts on, not real Opening Day: a season
    that opens on a Thursday plays several games before the fantasy calendar
    begins, and those games are never replayed.  Counting them would show
    managers production from games their league did not play.
    """
    cal = calendar(conn, league["season_year"], cfg)
    return cal[0].start.isoformat(), as_of_date(conn, league, cfg)


def describe(conn: sqlite3.Connection, league: dict[str, Any], cfg: LeagueConfig) -> dict[str, Any]:
    cal = calendar(conn, league["season_year"], cfg)
    current = league["current_week"] or 1
    active = next((w for w in cal if w.week == current), cal[-1])
    return {
        "season_year": league["season_year"],
        "current_week": current,
        "label": active.label,
        "week_start": active.start.isoformat(),
        "week_end": active.end.isoformat(),
        "as_of": as_of_date(conn, league, cfg),
        "regular_season_weeks": cfg.regular_season_weeks,
        "total_weeks": cfg.total_weeks,
        "all_star_week_skipped": season_row(conn, league["season_year"])["all_star_monday"],
        "weeks": [
            {"week": w.week, "start": w.start.isoformat(), "end": w.end.isoformat(),
             "label": w.label, "is_playoff": w.is_playoff}
            for w in cal
        ],
    }
