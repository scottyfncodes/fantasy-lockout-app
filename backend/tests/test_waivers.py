"""Blind FAAB waivers: bidding, resolution, drops and the honesty rules."""

from __future__ import annotations

import datetime as dt

import pytest

from app.services import leagues, lineups, timeline, waivers


def teams_of(conn, league):
    return leagues.teams(conn, league["id"])


def a_free_agent(conn, league, cfg, skip=0):
    return waivers.free_agents(conn, league, cfg, limit=10 + skip)[skip]


def test_free_agent_pool_only_shows_stats_through_the_current_date(conn, league, cfg):
    pool = waivers.free_agents(conn, league, cfg, limit=25)
    as_of = timeline.as_of_date(conn, league, cfg)
    # Nothing has been simulated yet, so every free agent must read as zero.
    assert as_of < timeline.week(conn, league["season_year"], cfg, 1).start.isoformat()
    assert all(p["points"] == 0 and p["games"] == 0 for p in pool)


def test_free_agents_exclude_rostered_players(conn, league, cfg):
    rostered = waivers.rostered_ids(conn, league["id"])
    pool = waivers.free_agents(conn, league, cfg, limit=100)
    assert not {p["player_id"] for p in pool} & rostered


def test_highest_blind_bid_wins(conn, league, cfg):
    a, b = teams_of(conn, league)[:2]
    target = a_free_agent(conn, league, cfg)
    drop_a = lineups.roster_players(conn, league, a["id"])[-1]["player_id"]
    drop_b = lineups.roster_players(conn, league, b["id"])[-1]["player_id"]

    waivers.submit_bid(conn, league, cfg, a["id"], target["player_id"], 12, drop_a)
    waivers.submit_bid(conn, league, cfg, b["id"], target["player_id"], 30, drop_b)
    results = waivers.process_week(conn, league, cfg, week=2)

    won = [r for r in results if r["status"] == "won"]
    assert len(won) == 1 and won[0]["team_id"] == b["id"]
    assert target["player_id"] in waivers._team_player_ids(conn, league["id"], b["id"])


def test_winning_bid_is_deducted_from_faab(conn, league, cfg):
    team = teams_of(conn, league)[0]
    target = a_free_agent(conn, league, cfg)
    drop = lineups.roster_players(conn, league, team["id"])[-1]["player_id"]
    waivers.submit_bid(conn, league, cfg, team["id"], target["player_id"], 25, drop)
    waivers.process_week(conn, league, cfg, week=2)
    after = leagues.get_team(conn, league["id"], team["id"])
    assert after["faab_remaining"] == cfg.faab_budget - 25


def test_ties_break_toward_the_worse_team(conn, league, cfg):
    a, b = teams_of(conn, league)[:2]
    conn.execute("UPDATE teams SET wins=6, points_for=900 WHERE id=?", (a["id"],))
    conn.execute("UPDATE teams SET wins=1, points_for=400 WHERE id=?", (b["id"],))
    target = a_free_agent(conn, league, cfg)
    for team in (a, b):
        drop = lineups.roster_players(conn, league, team["id"])[-1]["player_id"]
        waivers.submit_bid(conn, league, cfg, team["id"], target["player_id"], 20, drop)
    results = waivers.process_week(conn, league, cfg, week=2)
    winner = next(r for r in results if r["status"] == "won")
    assert winner["team_id"] == b["id"]


def test_bid_over_budget_is_refused_up_front(conn, league, cfg):
    team = teams_of(conn, league)[0]
    target = a_free_agent(conn, league, cfg)
    with pytest.raises(waivers.WaiverError, match="exceeds your remaining FAAB"):
        waivers.submit_bid(conn, league, cfg, team["id"], target["player_id"],
                           cfg.faab_budget + 1, None)


def test_full_roster_needs_a_drop(conn, league, cfg):
    team = teams_of(conn, league)[0]
    target = a_free_agent(conn, league, cfg)
    with pytest.raises(waivers.WaiverError, match="roster is full"):
        waivers.submit_bid(conn, league, cfg, team["id"], target["player_id"], 5, None)


def test_cannot_bid_on_a_rostered_player(conn, league, cfg):
    team = teams_of(conn, league)[0]
    other = teams_of(conn, league)[1]
    owned = lineups.roster_players(conn, league, other["id"])[0]["player_id"]
    with pytest.raises(waivers.WaiverError, match="already on a roster"):
        waivers.submit_bid(conn, league, cfg, team["id"], owned, 5, None)


def test_dropped_players_sit_on_waivers_before_clearing(conn, league, cfg):
    team = teams_of(conn, league)[0]
    victim = lineups.roster_players(conn, league, team["id"])[-1]["player_id"]
    today = timeline.as_of_date(conn, league, cfg)
    clears = waivers.drop_to_waivers(conn, league, cfg, team["id"], victim, week=1, today=today)

    assert clears == (dt.date.fromisoformat(today)
                      + dt.timedelta(days=cfg.waiver_clear_days)).isoformat()
    assert victim in waivers.blocked_ids(conn, league["id"], today)


def test_a_player_on_waivers_cannot_be_re_added_immediately(conn, league, cfg):
    """The drop deadline stops drop-and-re-add from dodging a rival's bid."""
    a, b = teams_of(conn, league)[:2]
    victim = lineups.roster_players(conn, league, a["id"])[-1]["player_id"]
    today = timeline.as_of_date(conn, league, cfg)
    waivers.drop_to_waivers(conn, league, cfg, a["id"], victim, week=1, today=today)

    waivers.submit_bid(conn, league, cfg, a["id"], victim, 1, None)
    results = waivers.process_week(conn, league, cfg, week=2, today=today)
    assert all(r["status"] != "won" for r in results)
    assert "on waivers" in results[0]["reason"]


def test_waivers_clear_after_the_configured_days(conn, league, cfg):
    team = teams_of(conn, league)[0]
    victim = lineups.roster_players(conn, league, team["id"])[-1]["player_id"]
    today = timeline.as_of_date(conn, league, cfg)
    clears = waivers.drop_to_waivers(conn, league, cfg, team["id"], victim, week=1, today=today)
    waivers.clear_expired(conn, league["id"], clears)
    assert victim not in waivers.blocked_ids(conn, league["id"], clears)


def test_adds_can_be_frozen_for_the_final_weeks(conn, league):
    """The optional guard against memory-based sniping late in the season."""
    frozen_cfg = leagues.league_config(league).merged({"freeze_adds_final_weeks": 4})
    assert not waivers.adds_frozen(frozen_cfg, 14)
    assert waivers.adds_frozen(frozen_cfg, 15)
    team = leagues.teams(conn, league["id"])[0]
    conn.execute("UPDATE leagues SET current_week = 16 WHERE id = ?", (league["id"],))
    refreshed = leagues.require_league(conn, league["id"])
    with pytest.raises(waivers.WaiverError, match="frozen"):
        waivers.submit_bid(conn, refreshed, frozen_cfg, team["id"], "whoever", 5, None)


def test_losing_bids_are_reported_after_processing(conn, league, cfg):
    a, b = teams_of(conn, league)[:2]
    target = a_free_agent(conn, league, cfg)
    for team, amount in ((a, 5), (b, 9)):
        drop = lineups.roster_players(conn, league, team["id"])[-1]["player_id"]
        waivers.submit_bid(conn, league, cfg, team["id"], target["player_id"], amount, drop)
    waivers.process_week(conn, league, cfg, week=2)
    summary = waivers.summary(conn, league, cfg, 2)
    statuses = {r["team_id"]: r["status"] for r in summary["results"]}
    assert statuses[b["id"]] == "won" and statuses[a["id"]] == "lost"


# ---------------------------------------------------------------------------
# the playoff freeze
# ---------------------------------------------------------------------------

def test_rosters_freeze_when_the_playoffs_begin(conn, league, cfg):
    """The bracket is decided by the team you built, not by September memory."""
    assert not waivers.adds_frozen(cfg, cfg.regular_season_weeks)
    assert waivers.adds_frozen(cfg, cfg.regular_season_weeks + 1)
    assert waivers.adds_frozen(cfg, cfg.total_weeks)
    assert "playoffs" in waivers.freeze_reason(cfg, cfg.regular_season_weeks + 1)


def test_a_playoff_bid_is_refused_rather_than_left_pending(conn, league, cfg):
    """The rollover never processes waivers in playoff weeks.

    Accepting a bid there would leave it pending for ever, so it has to be
    refused at submission with a reason a manager can act on.
    """
    team = leagues.teams(conn, league["id"])[0]
    target = a_free_agent(conn, league, cfg)
    conn.execute("UPDATE leagues SET current_week = ? WHERE id = ?",
                 (cfg.regular_season_weeks, league["id"]))
    playoff_league = leagues.require_league(conn, league["id"])

    with pytest.raises(waivers.WaiverError, match="frozen for the playoffs"):
        waivers.submit_bid(conn, playoff_league, cfg, team["id"], target["player_id"], 5, None)

    pending = conn.execute(
        "SELECT COUNT(*) n FROM waiver_bids WHERE league_id=? AND status='pending'",
        (league["id"],),
    ).fetchone()["n"]
    assert pending == 0


def test_bots_do_not_bid_once_rosters_freeze(conn, league, cfg):
    from app.services import bots
    assert bots.submit_bot_bids(conn, league, cfg, cfg.regular_season_weeks + 1) == []


def test_the_playoff_freeze_can_be_turned_off(conn, league):
    open_cfg = leagues.league_config(league).merged({"freeze_adds_in_playoffs": False})
    assert not waivers.adds_frozen(open_cfg, open_cfg.regular_season_weeks + 1)
    assert waivers.freeze_reason(open_cfg, open_cfg.regular_season_weeks + 1) is None
