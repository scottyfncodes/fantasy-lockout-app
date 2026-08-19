"""The point values are the league's rules; these tests pin them exactly."""

from __future__ import annotations

import pytest

from app.scoring import (
    ScoringConfig,
    is_quality_start,
    score_batting,
    score_day,
    score_pitching,
)


@pytest.fixture
def cfg() -> ScoringConfig:
    return ScoringConfig.load()


def bat(**kw):
    base = dict(r=0, b1=0, b2=0, b3=0, hr=0, rbi=0, bb=0, ibb=0, hbp=0, so=0, sb=0, slam=0)
    base.update(kw)
    return base


def pit(**kw):
    base = dict(gs=0, outs=0, bf=0, h=0, er=0, bb=0, hbp=0, so=0, w=0, sv=0, cg=0)
    base.update(kw)
    return base


@pytest.mark.parametrize("stat,line,expected", [
    ("R", bat(r=1), 2),
    ("1B", bat(b1=1), 1),
    ("2B", bat(b2=1), 2),
    ("3B", bat(b3=1), 3),
    ("HR", bat(hr=1), 4),
    ("RBI", bat(rbi=1), 2),
    ("SB", bat(sb=1), 2.5),
    ("BB", bat(bb=1), 1),
    ("HBP", bat(hbp=1), 1),
    ("K", bat(so=1), -0.5),
])
def test_batting_values(stat, line, expected, cfg):
    assert score_batting(line, cfg).points == expected


@pytest.mark.parametrize("stat,line,expected", [
    ("IP", pit(outs=3), 1.5),
    ("W", pit(w=1), 4),
    ("CG", pit(cg=1, outs=27, h=5), 10 + 13.5),
    ("SV", pit(sv=1), 10),
    ("ER", pit(er=1), -0.5),
    ("K", pit(so=1), 1.5),
])
def test_pitching_values(stat, line, expected, cfg):
    assert score_pitching(line, cfg).points == expected


def test_intentional_walk_stacks_with_walk_by_default(cfg):
    """Box-score BB includes IBB, so one IBB scores BB(1) + IBB(1)."""
    assert score_batting(bat(bb=1, ibb=1), cfg).points == 2


def test_intentional_walk_can_be_unstacked():
    cfg = ScoringConfig.from_dict({
        "batting": {"BB": 1, "IBB": 1}, "pitching": {},
        "options": {"ibb_stacks_with_bb": False},
    })
    assert score_batting(bat(bb=1, ibb=1), cfg).points == 1


def test_partial_innings_are_prorated(cfg):
    """5.2 IP is 17 outs, worth 17/3 * 1.5, not 5 * 1.5."""
    assert score_pitching(pit(outs=17), cfg).points == pytest.approx(8.5)


def test_a_cycle_is_worth_only_its_parts(cfg):
    """The cycle bonus was removed; the hits still score, nothing on top."""
    scored = score_batting(bat(b1=1, b2=1, b3=1, hr=1), cfg)
    assert scored.points == 1 + 2 + 3 + 4
    assert "CYC" not in scored.breakdown


def test_grand_slam_scores_on_top_of_the_home_run(cfg):
    scored = score_batting(bat(hr=1, rbi=4, slam=1), cfg)
    assert scored.breakdown["SLAM"] == 10
    assert scored.points == 4 + 8 + 10


def test_quality_start_rule(cfg):
    assert is_quality_start(pit(gs=1, outs=18, er=3), cfg)
    assert not is_quality_start(pit(gs=1, outs=17, er=3), cfg)
    assert not is_quality_start(pit(gs=1, outs=18, er=4), cfg)
    assert not is_quality_start(pit(gs=0, outs=18, er=0), cfg), "relievers can't earn a QS"


def test_two_way_player_gets_both_halves(cfg):
    scored = score_day(bat(hr=1), pit(gs=1, outs=21, so=9, w=1), cfg)
    assert scored.points == pytest.approx(4 + 10.5 + 13.5 + 4 + 4)  # HR, IP, K, W, QS
    assert set(scored.breakdown) >= {"HR", "IP", "K", "W", "QS"}


def test_summed_lines_skip_per_game_bonuses(cfg):
    """A season total contains grand slams already; counting them again would
    pay twice for the same swing."""
    season = bat(b1=90, b2=30, b3=4, hr=25, slam=3)
    assert "SLAM" not in score_batting(season, cfg, include_derived=False).breakdown


def test_config_is_editable_without_touching_logic():
    cfg = ScoringConfig.from_dict({"batting": {"HR": 10}, "pitching": {"K": 3}})
    assert score_batting(bat(hr=2), cfg).points == 20
    assert score_pitching(pit(so=2), cfg).points == 6


# ---------------------------------------------------------------------------
# categories the league removed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("category", ["HLD", "PICK", "NH", "PG"])
def test_retired_categories_are_gone_from_the_config(category, cfg):
    """Holds, pickoffs, no-hitters and perfect games no longer score."""
    assert category not in cfg.pitching


def test_a_hitless_complete_game_scores_only_the_ordinary_categories(cfg):
    """What used to be worth 150 bonus points is now just a very good start."""
    scored = score_pitching(
        pit(gs=1, outs=27, cg=1, h=0, bb=0, hbp=0, bf=27, so=10, w=1), cfg,
    )
    assert set(scored.breakdown) == {"IP", "W", "CG", "K", "QS"}
    assert scored.points == 13.5 + 4 + 10 + 15 + 4


def test_stray_hold_or_pickoff_data_cannot_score(cfg):
    """Even if a source supplied them, there is no category to pay them out."""
    assert score_pitching(pit(hld=3, pick=2), cfg).points == 0
