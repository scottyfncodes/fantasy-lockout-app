"""The positional-value report.

It is decision-support for the commissioner rather than league machinery, but
a wrong number here would send a rules change in the wrong direction, so the
invariants are worth pinning.
"""

from __future__ import annotations

from app.config import LeagueConfig
from app.scoring import ScoringConfig
from scripts.balance_report import _is_sole, slot_table


def player(pid, positions, pitcher=False):
    return {"player_id": pid, "name": pid, "positions": positions, "is_pitcher": int(pitcher)}


SLOTS = LeagueConfig.load().active_slots


def test_sole_eligibility_ignores_the_catch_all_slots():
    """Every batter is UTIL-eligible; that must not make everyone multi-position."""
    cfg = LeagueConfig.load()
    assert _is_sole(player("c", "C"), "C", cfg)
    assert not _is_sole(player("util", "2B,OF"), "2B", cfg)
    assert _is_sole(player("rp", "RP", pitcher=True), "RP", cfg)
    assert not _is_sole(player("swing", "RP,SP", pitcher=True), "RP", cfg)


def test_report_covers_every_active_slot(conn, league, cfg):
    table = slot_table(conn, league["season_year"], cfg, ScoringConfig.load())
    assert set(table) == set(cfg.active_slots)


def test_starters_outscore_replacement(conn, league, cfg):
    table = slot_table(conn, league["season_year"], cfg, ScoringConfig.load())
    for slot, row in table.items():
        assert row["best"] >= row["starter_avg"] >= row["replacement"], slot
        assert row["value_over_replacement"] >= 0, slot


def test_sole_count_cannot_exceed_the_startable_pool(conn, league, cfg):
    table = slot_table(conn, league["season_year"], cfg, ScoringConfig.load())
    for slot, row in table.items():
        assert 0 <= row["sole"] <= cfg.team_count * cfg.active_slots[slot], slot


def test_team_points_scale_with_slot_count(conn, league, cfg):
    table = slot_table(conn, league["season_year"], cfg, ScoringConfig.load())
    for slot, row in table.items():
        expected = row["starter_avg"] * cfg.active_slots[slot]
        assert row["team_points"] == expected, slot


def test_raising_a_category_raises_the_slots_that_use_it(conn, league, cfg):
    """Doubling saves must lift relievers and leave batters untouched."""
    base = ScoringConfig.load()
    richer = ScoringConfig.from_dict({
        **base.to_dict(), "pitching": {**base.pitching, "SV": base.pitching["SV"] * 2},
    })
    year = league["season_year"]
    before = slot_table(conn, year, cfg, base)
    from app.services import players as players_svc
    players_svc.invalidate_cache()
    after = slot_table(conn, year, cfg, richer)

    assert after["RP"]["starter_avg"] > before["RP"]["starter_avg"]
    assert after["C"]["starter_avg"] == before["C"]["starter_avg"]
