"""Seasons are fetched when a league draws them, not stockpiled in advance."""

from __future__ import annotations

import pytest

from app.pipeline import build as build_mod
from app.services import leagues, season_cache
from tests.conftest import TEST_YEAR


def test_a_year_can_be_drawn_before_it_is_cached(conn, cfg):
    """The draw offers the configured range, not merely what is on disk.

    Requiring a cache first would mean a deployment could only ever offer the
    years it happened to have warmed, which is the thing on-demand fetching
    exists to avoid.
    """
    wide = cfg.merged({"eligible_year_min": 2001, "eligible_year_max": 2017})
    years = leagues.eligible_years(conn, wide)
    assert len(years) > 1, "the draw must not be limited to cached seasons"
    assert TEST_YEAR in years
    uncached = [y for y in years if not season_cache.is_cached(conn, y)]
    assert uncached, "this test is meaningless if everything is already cached"


def test_a_season_rejected_by_the_ingest_is_never_drawn_again(conn, cfg):
    wide = cfg.merged({"eligible_year_min": 2001, "eligible_year_max": 2017})
    victim = next(y for y in leagues.eligible_years(conn, wide) if y != TEST_YEAR)
    conn.execute(
        "INSERT INTO seasons (year, source, opening_day, final_game_day, "
        "all_star_monday, eligible, ineligible_reason, ingested_at) "
        "VALUES (?,'retrosheet','2011-04-01','2011-09-28','2011-07-11',0,'too short','now')",
        (victim,),
    )
    assert victim not in leagues.eligible_years(conn, wide)


def test_status_tells_a_waiting_league_what_is_happening(conn):
    assert season_cache.status(conn, None)["state"] == "undrawn"
    assert season_cache.status(conn, TEST_YEAR) == {
        "year": TEST_YEAR, "ready": True, "state": "ready",
    }
    pending = season_cache.status(conn, 2003)
    assert pending["ready"] is False and pending["state"] == "pending"


def test_a_cached_season_is_never_refetched(conn, monkeypatch):
    """The second league to draw a year must not pay for it again."""
    called = []
    monkeypatch.setattr(season_cache, "_ingest", lambda year: called.append(year))
    season_cache.ensure(TEST_YEAR)
    assert called == [], "a season already on disk was fetched a second time"


def test_the_draft_will_not_start_on_a_season_that_has_not_landed(conn):
    """Drafting from a season that is still downloading means an empty pool."""
    league_row = conn.execute("SELECT id FROM leagues LIMIT 1").fetchone()
    league = leagues.require_league(conn, league_row["id"])
    conn.execute("UPDATE leagues SET season_year = 2003 WHERE id = ?", (league["id"],))
    conn.commit()

    status = season_cache.status(conn, 2003)
    assert not status["ready"], "2003 is not in the fixture"
    # This is the check _start_minigame makes before it will open a draft.
    assert status["state"] in ("pending", "loading")


def test_a_year_cached_on_demand_survives_a_restart(conn, cfg):
    """Pruning must not undo on-demand caching.

    Boot prunes cached seasons that fall outside the configured range, which is
    right — a disk outlives any one config. But it used to be handed the
    *warm-up* list instead, so every year a league actually drew and cached was
    struck off the draw on the next restart, walking the pool back down to
    whatever was warmed up front.
    """
    wide = cfg.merged({"eligible_year_min": 2001, "eligible_year_max": 2017})
    warmed_up_front = [TEST_YEAR]
    drawn_on_demand = next(y for y in wide.eligible_years() if y != TEST_YEAR)
    conn.execute(
        "INSERT INTO seasons (year, source, opening_day, final_game_day, "
        "all_star_monday, eligible, ineligible_reason, ingested_at) "
        "VALUES (?,'retrosheet','2007-04-01','2007-09-30','2007-07-09',1,NULL,'now')",
        (drawn_on_demand,),
    )
    assert drawn_on_demand in leagues.eligible_years(conn, wide)

    build_mod.prune_to(conn, warmed_up_front)
    assert drawn_on_demand not in leagues.eligible_years(conn, wide), (
        "this test is meaningless unless pruning to the warm-up list would drop it"
    )

    conn.execute("UPDATE seasons SET eligible = 1, ineligible_reason = NULL WHERE year = ?",
                 (drawn_on_demand,))
    build_mod.prune_to(conn, wide.eligible_years())
    assert drawn_on_demand in leagues.eligible_years(conn, wide)


def test_pruning_still_drops_a_year_the_config_no_longer_allows(conn, cfg):
    """The reason pruning exists at all: a narrowed range has to take effect."""
    narrow = cfg.merged({"eligible_year_min": 2016, "eligible_year_max": 2017})
    stale = 2004
    conn.execute(
        "INSERT INTO seasons (year, source, opening_day, final_game_day, "
        "all_star_monday, eligible, ineligible_reason, ingested_at) "
        "VALUES (?,'retrosheet','2004-04-04','2004-10-03','2004-07-12',1,NULL,'now')",
        (stale,),
    )
    dropped = dict(build_mod.prune_to(conn, narrow.eligible_years()))
    assert stale in dropped
    assert stale not in leagues.eligible_years(conn, narrow)
