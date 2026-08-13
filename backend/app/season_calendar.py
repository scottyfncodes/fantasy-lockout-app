"""Mapping fantasy weeks onto the replayed season's real calendar.

The rules, made unambiguous:

* Fantasy weeks run Monday -> Sunday.
* Week 1 starts on the first Monday on or after real Opening Day.  Any real
  games played before that Monday are not replayed (they belong to no fantasy
  week); Opening Day itself is included whenever it falls on a Monday.
* The calendar week containing the All-Star break is skipped entirely.  No
  games process, and it does not consume one of the 22 fantasy weeks.
* We take the first 22 qualifying weeks and stop.  The real season's final
  weeks are simply unused — the 22-week window is anchored at the *front* of
  the season, which is what makes the fantasy-week -> date mapping total and
  unambiguous.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True)
class FantasyWeek:
    week: int
    start: dt.date  # Monday
    end: dt.date    # Sunday
    is_playoff: bool
    label: str

    def contains(self, day: dt.date) -> bool:
        return self.start <= day <= self.end


def monday_of(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


def first_monday_on_or_after(day: dt.date) -> dt.date:
    return day if day.weekday() == 0 else day + dt.timedelta(days=7 - day.weekday())


def default_all_star_week(year: int) -> dt.date:
    """Monday of the All-Star week when game data can't tell us.

    The All-Star Game is conventionally the Tuesday of the second full week of
    July, so we take the second Tuesday of July and snap to its Monday.
    """
    d = dt.date(year, 7, 1)
    while d.weekday() != 1:  # Tuesday
        d += dt.timedelta(days=1)
    return monday_of(d + dt.timedelta(days=7))


def detect_all_star_week(game_dates: list[dt.date], year: int) -> dt.date:
    """Find the Monday of the All-Star break from the real schedule.

    The break is the only multi-day gap in a season, so the July week with the
    fewest game days is it.  Falls back to the calendar heuristic if the data
    is too sparse to be conclusive.
    """
    july = [d for d in game_dates if d.year == year and d.month == 7]
    if not july:
        return default_all_star_week(year)

    by_week: dict[dt.date, set[dt.date]] = {}
    for d in july:
        by_week.setdefault(monday_of(d), set()).add(d)
    # Only weeks fully inside July are candidates; edge weeks look sparse for
    # the wrong reason.
    candidates = {
        wk: days for wk, days in by_week.items()
        if wk.month == 7 and (wk + dt.timedelta(days=6)).month == 7
    }
    if not candidates:
        return default_all_star_week(year)

    quietest = min(candidates.items(), key=lambda kv: (len(kv[1]), kv[0]))
    # A normal week has 6-7 game days; the break week has 3-4.
    if len(quietest[1]) <= 5:
        return quietest[0]
    return default_all_star_week(year)


def build_calendar(
    opening_day: dt.date,
    total_weeks: int,
    regular_season_weeks: int,
    all_star_monday: dt.date,
) -> list[FantasyWeek]:
    weeks: list[FantasyWeek] = []
    cursor = first_monday_on_or_after(opening_day)
    n = 1
    guard = 0
    while n <= total_weeks:
        guard += 1
        if guard > 60:  # pragma: no cover - impossible for a real calendar
            raise RuntimeError("calendar failed to converge")
        if cursor == all_star_monday:
            cursor += dt.timedelta(days=7)
            continue
        is_playoff = n > regular_season_weeks
        weeks.append(
            FantasyWeek(
                week=n,
                start=cursor,
                end=cursor + dt.timedelta(days=6),
                is_playoff=is_playoff,
                label=playoff_label(n, regular_season_weeks, total_weeks),
            )
        )
        cursor += dt.timedelta(days=7)
        n += 1
    return weeks


def playoff_label(week: int, regular_season_weeks: int, total_weeks: int) -> str:
    if week <= regular_season_weeks:
        return f"Week {week}"
    offset = week - regular_season_weeks
    if week >= total_weeks - 1:
        leg = week - (total_weeks - 1) + 1
        return f"Finals (leg {leg} of 2)"
    if offset == 1:
        return "Quarterfinals"
    if offset == 2:
        return "Semifinals"
    return f"Playoff round {offset}"


def week_for_date(weeks: list[FantasyWeek], day: dt.date) -> FantasyWeek | None:
    for w in weeks:
        if w.contains(day):
            return w
    return None


def season_fits(
    opening_day: dt.date,
    final_game_day: dt.date,
    total_weeks: int,
    regular_season_weeks: int,
    all_star_monday: dt.date,
) -> tuple[bool, str]:
    """Does the real season have room for the full fantasy calendar?"""
    weeks = build_calendar(opening_day, total_weeks, regular_season_weeks, all_star_monday)
    last = weeks[-1].end
    if final_game_day >= last:
        slack = (final_game_day - last).days
        return True, f"fits with {slack} days of slack after week {total_weeks}"
    short = (last - final_game_day).days
    return False, f"real season ends {short} days before fantasy week {total_weeks} closes"
