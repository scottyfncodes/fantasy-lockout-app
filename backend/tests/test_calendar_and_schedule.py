"""Fantasy-week calendar and head-to-head schedule generation."""

from __future__ import annotations

import datetime as dt

import pytest

from app.config import LeagueConfig
from app.season_calendar import (
    build_calendar,
    default_all_star_week,
    detect_all_star_week,
    first_monday_on_or_after,
    season_fits,
)
from app.services import schedule as schedule_svc


def test_week_one_starts_on_the_first_monday():
    opening = dt.date(2019, 3, 28)  # a Thursday
    cal = build_calendar(opening, 22, 18, default_all_star_week(2019))
    assert cal[0].start == dt.date(2019, 4, 1)
    assert cal[0].start.weekday() == 0 and cal[0].end.weekday() == 6


def test_opening_day_on_a_monday_is_week_one():
    assert first_monday_on_or_after(dt.date(2021, 4, 5)) == dt.date(2021, 4, 5)


def test_all_star_week_is_skipped_entirely():
    asb = default_all_star_week(2019)
    cal = build_calendar(dt.date(2019, 3, 28), 22, 18, asb)
    assert all(w.start != asb for w in cal), "no fantasy week may start on the break Monday"
    # Weeks stay consecutive apart from the one-week jump over the break.
    gaps = [(b.start - a.start).days for a, b in zip(cal, cal[1:])]
    assert gaps.count(14) == 1 and gaps.count(7) == len(gaps) - 1


def test_calendar_length_and_playoff_labels():
    cal = build_calendar(dt.date(2019, 3, 28), 22, 18, default_all_star_week(2019))
    assert len(cal) == 22
    assert [w.week for w in cal] == list(range(1, 23))
    assert sum(1 for w in cal if w.is_playoff) == 4
    labels = {w.week: w.label for w in cal}
    assert labels[19] == "Quarterfinals"
    assert labels[20] == "Semifinals"
    assert labels[21] == "Finals (leg 1 of 2)"
    assert labels[22] == "Finals (leg 2 of 2)"


def test_22_weeks_fit_inside_a_real_season():
    ok, detail = season_fits(
        dt.date(2019, 3, 28), dt.date(2019, 9, 29), 22, 18, default_all_star_week(2019)
    )
    assert ok, detail


def test_short_season_is_rejected():
    """A 60-game season (2020) cannot host a 22-week replay."""
    ok, detail = season_fits(
        dt.date(2020, 7, 23), dt.date(2020, 9, 27), 22, 18, default_all_star_week(2020)
    )
    assert not ok and "before" in detail


def test_all_star_break_detected_from_the_schedule():
    """The break is the only multi-day gap, so the quiet July week gives it away."""
    days = []
    d = dt.date(2019, 7, 1)
    break_monday = default_all_star_week(2019)
    while d <= dt.date(2019, 7, 31):
        if not (break_monday <= d < break_monday + dt.timedelta(days=4)):
            days.append(d)
        d += dt.timedelta(days=1)
    assert detect_all_star_week(days, 2019) == break_monday


@pytest.mark.parametrize("n", [8, 10, 12, 14])
def test_round_robin_pairs_every_team_once_per_week(n):
    ids = [f"t{i}" for i in range(n)]
    weeks = schedule_svc.regular_season(ids, 18)
    assert len(weeks) == 18
    for pairs in weeks:
        assert len(pairs) == n // 2
        seen = [t for pair in pairs for t in pair]
        assert sorted(seen) == sorted(ids), "every team plays exactly one game"


@pytest.mark.parametrize("n", [8, 10, 12, 14])
def test_everyone_meets_before_anyone_repeats(n):
    ids = [f"t{i}" for i in range(n)]
    weeks = schedule_svc.regular_season(ids, n - 1)
    pairings = {frozenset(p) for pairs in weeks for p in pairs}
    assert len(pairings) == n * (n - 1) // 2


@pytest.mark.parametrize("n", [8, 10, 12, 14])
def test_home_and_away_stay_balanced(n):
    ids = [f"t{i}" for i in range(n)]
    weeks = schedule_svc.regular_season(ids, 18)
    home = {t: 0 for t in ids}
    for pairs in weeks:
        for h, _a in pairs:
            home[h] += 1
    assert max(home.values()) - min(home.values()) <= 4


def test_an_odd_round_robin_sits_one_team_out(): 
    """Odd leagues are supported now: the third team has the week off."""
    rounds = schedule_svc.round_robin_rounds(["a", "b", "c"])
    assert len(rounds) == 3, "three teams, three rounds"
    for week in rounds:
        assert len(week) == 1, "one game, one team resting"
    # Over a full cycle everybody rests exactly once.
    rests = [set("abc") - {t for pair in week for t in pair} for week in rounds]
    assert sorted(next(iter(r)) for r in rests) == ["a", "b", "c"]


def test_bracket_seeding_is_one_versus_eight():
    seeds = [f"s{i}" for i in range(1, 9)]
    assert schedule_svc.bracket_pairings(seeds) == [
        ("s1", "s8"), ("s2", "s7"), ("s3", "s6"), ("s4", "s5"),
    ]


def test_playoff_weeks_land_on_19_through_22():
    cfg = LeagueConfig.load()
    plan = schedule_svc.playoff_week_plan(cfg)
    assert [(s["stage"], s["weeks"]) for s in plan] == [
        ("quarterfinal", [19]), ("semifinal", [20]), ("final", [21, 22]),
    ]


def test_finals_span_two_weeks():
    cfg = LeagueConfig.load()
    final = schedule_svc.playoff_week_plan(cfg)[-1]
    assert len(final["weeks"]) == 2, "the final is decided on combined points"


# ---------------------------------------------------------------------------
# odd leagues
# ---------------------------------------------------------------------------

def test_an_odd_league_gets_a_rotating_bye():
    """9 to 15 teams are allowed, so somebody has the week off."""
    import collections
    from app.services.schedule import regular_season

    for size in (9, 11, 13, 15):
        ids = [f"t{i}" for i in range(size)]
        weeks = regular_season(ids, 18)
        byes: collections.Counter = collections.Counter()
        for week in weeks:
            assert len(week) == (size - 1) // 2, "one team sits out each week"
            playing = {t for pair in week for t in pair}
            assert len(playing) == size - 1
            for team in set(ids) - playing:
                byes[team] += 1
        # Evenly shared: nobody sits out twice before everybody has sat once.
        assert max(byes.values()) - min(byes.values()) <= 1, f"{size}: {byes}"


def test_no_phantom_opponent_leaks_into_a_schedule():
    """The bye is an implementation device; it must never reach a matchup."""
    from app.services.schedule import BYE, regular_season

    weeks = regular_season([f"t{i}" for i in range(11)], 6)
    for week in weeks:
        for home, away in week:
            assert BYE not in (home, away)


def test_an_even_league_never_has_a_bye():
    from app.services.schedule import regular_season

    for size in (8, 10, 12, 14):
        for week in regular_season([f"t{i}" for i in range(size)], 8):
            assert len(week) == size // 2
