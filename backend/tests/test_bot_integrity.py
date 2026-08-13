"""The integrity rule: bots must not know how the season turned out.

A replay league's distinctive failure mode is an agent that reads the finished
season and sets a perfect lineup every week.  Bots are allowed full-season data
while *drafting* (symmetric information — humans are choosing from the same
finished season) and nothing but pre-week data once the replay starts.

These tests fail loudly if anyone wires ``season_totals`` into an in-season
code path.
"""

from __future__ import annotations

import pytest

from app.services import bots, leagues, lineups, players, replay, timeline, waivers


@pytest.fixture
def no_hindsight(monkeypatch):
    """Make any full-season lookup an error for the duration of a test."""
    def boom(*_a, **_kw):
        raise AssertionError(
            "in-season code called players.season_totals — that is hindsight; "
            "use players.stats_through(as_of) instead"
        )
    monkeypatch.setattr(players, "season_totals", boom)
    monkeypatch.setattr(lineups.players_svc, "season_totals", boom)
    monkeypatch.setattr(waivers.players_svc, "season_totals", boom)
    return boom


def test_bot_lineups_use_no_full_season_data(conn, league, cfg, no_hindsight):
    bots.set_all_bot_lineups(conn, league, cfg, week=2)
    bot = next(t for t in leagues.teams(conn, league["id"]) if t["is_bot"])
    assignment = lineups.stored_lineup(conn, league["id"], bot["id"], 2)
    active = [s for s in assignment.values() if s not in ("BENCH", "IL")]
    assert len(active) == cfg.active_size


def test_bot_waiver_bids_use_no_full_season_data(conn, league, cfg, no_hindsight):
    bots.submit_bot_bids(conn, league, cfg, week=2)  # must not raise


def test_autofill_for_humans_uses_no_full_season_data(conn, league, cfg, no_hindsight):
    team = leagues.teams(conn, league["id"])[0]
    lineups.autofill(conn, league, team["id"], 2, cfg)


def test_weekly_rollover_uses_no_full_season_data(conn, league, cfg, no_hindsight):
    replay.rollover(conn, league, cfg, new_week=2)


def test_free_agent_pool_uses_no_full_season_data(conn, league, cfg, no_hindsight):
    waivers.free_agents(conn, league, cfg, limit=20)


def test_pre_week_ranking_is_blind_before_anything_is_played(conn, league, cfg):
    assert lineups.pre_week_ranking(conn, league, cfg, week=1) == {}


def test_pre_week_ranking_stops_at_the_previous_week(conn, league, cfg):
    """After simulating into week 2, week 2's ranking sees week 1 only."""
    for _ in range(10):
        league = leagues.require_league(conn, league["id"])
        replay.advance_day(conn, league, cfg)
    league = leagues.require_league(conn, league["id"])

    scoring = leagues.league_scoring(league)
    week2_start = timeline.week(conn, league["season_year"], cfg, 2).start
    cutoff = (week2_start.replace()).isoformat()
    ranking = lineups.pre_week_ranking(conn, league, cfg, week=2)
    visible = players.stats_through(conn, league["season_year"], scoring, cutoff)
    later = players.stats_through(conn, league["season_year"], scoring,
                                  timeline.week(conn, league["season_year"], cfg, 3).end.isoformat())
    assert ranking, "week 2 should see week 1's results"
    assert len(visible) < len(later), "the fixture must have games after the cut-off"


def test_drafting_may_use_full_season_data(conn, league, cfg):
    """Explicitly allowed: the draft happens before the replay starts."""
    ranking = lineups.draft_order_ranking(conn, league, cfg)
    assert ranking and max(ranking.values()) > 0
