"""Weekly lineups, IL lockouts and the Sunday deadline."""

from __future__ import annotations

import datetime as dt

import pytest

from app.services import il, leagues, lineups, timeline


def a_team(conn, league):
    return leagues.teams(conn, league["id"])[0]


def test_draft_leaves_every_team_able_to_field_a_lineup(conn, league, cfg):
    for team in leagues.teams(conn, league["id"]):
        view = lineups.view(conn, league, cfg, team["id"], 1)
        active = [p for p in view["players"] if p["slot"] not in ("BENCH", "IL")]
        assert len(active) == cfg.active_size, team["name"]


def test_roster_is_the_configured_size(conn, league, cfg):
    for team in leagues.teams(conn, league["id"]):
        assert len(lineups.roster_players(conn, league, team["id"])) == cfg.roster_size


def legal_lineup(conn, league, cfg, team_id, week=2):
    """Start from the auto-filled lineup so mutations stay otherwise valid."""
    lineups.autofill(conn, league, team_id, week, cfg)
    return lineups.stored_lineup(conn, league["id"], team_id, week)


def test_players_on_the_il_cannot_start(conn, league, cfg):
    team = a_team(conn, league)
    roster = lineups.roster_players(conn, league, team["id"])
    week_start = timeline.week(conn, league["season_year"], cfg, 2).start.isoformat()
    injured = il.il_status(conn, league["season_year"], [p["player_id"] for p in roster], week_start)
    if not injured:
        pytest.skip("nobody on this roster was hurt that week")

    assignment = legal_lineup(conn, league, cfg, team["id"])
    hurt_id = next(iter(injured))
    hurt = next(p for p in roster if p["player_id"] == hurt_id)
    slot = lineups.players_svc.eligible_slots(hurt, cfg.active_slots.keys())[0]
    incumbent = next(pid for pid, s in assignment.items() if s == slot)

    assignment[incumbent] = "BENCH"
    assignment[hurt_id] = slot
    with pytest.raises(lineups.LineupError, match="bench him or use an IL slot"):
        lineups.save(conn, league, cfg, team["id"], 2, assignment)


def test_il_slot_only_accepts_actually_injured_players(conn, league, cfg):
    team = a_team(conn, league)
    roster = lineups.roster_players(conn, league, team["id"])
    week_start = timeline.week(conn, league["season_year"], cfg, 2).start.isoformat()
    injured = il.il_status(conn, league["season_year"], [p["player_id"] for p in roster], week_start)

    assignment = legal_lineup(conn, league, cfg, team["id"])
    healthy = next(pid for pid, slot in assignment.items()
                   if slot == "BENCH" and pid not in injured)
    assignment[healthy] = "IL"
    with pytest.raises(lineups.LineupError, match="not on the injured list"):
        lineups.save(conn, league, cfg, team["id"], 2, assignment)


def test_position_eligibility_is_enforced(conn, league, cfg):
    team = a_team(conn, league)
    assignment = legal_lineup(conn, league, cfg, team["id"])
    roster = {p["player_id"]: p for p in lineups.roster_players(conn, league, team["id"])}
    pitcher = next(pid for pid, slot in assignment.items()
                   if slot == "BENCH" and roster[pid]["is_pitcher"])
    util = next(pid for pid, slot in assignment.items() if slot == "UTIL")
    assignment[util] = "BENCH"
    assignment[pitcher] = "UTIL"
    with pytest.raises(lineups.LineupError, match="not eligible"):
        lineups.save(conn, league, cfg, team["id"], 2, assignment)


def test_slot_limits_are_enforced(conn, league, cfg):
    team = a_team(conn, league)
    assignment = legal_lineup(conn, league, cfg, team["id"])
    roster = {p["player_id"]: p for p in lineups.roster_players(conn, league, team["id"])}
    spare_catcher = next(
        (pid for pid, slot in assignment.items()
         if slot == "BENCH" and "C" in roster[pid]["positions"].split(",")),
        None,
    )
    if spare_catcher is None:
        pytest.skip("only one catcher rostered")
    assignment[spare_catcher] = "C"
    with pytest.raises(lineups.LineupError, match="too many players in C"):
        lineups.save(conn, league, cfg, team["id"], 2, assignment)


def test_every_rostered_player_needs_a_slot(conn, league, cfg):
    team = a_team(conn, league)
    assignment = legal_lineup(conn, league, cfg, team["id"])
    assignment.pop(next(iter(assignment)))
    with pytest.raises(lineups.LineupError, match="needs a slot"):
        lineups.save(conn, league, cfg, team["id"], 2, assignment)


def test_bench_overflow_is_rejected(conn, league, cfg):
    """Unused IL slots hold healthy players, but the roster limit still binds."""
    team = a_team(conn, league)
    assignment = legal_lineup(conn, league, cfg, team["id"])
    for pid in list(assignment):
        assignment[pid] = "BENCH"
    with pytest.raises(lineups.LineupError, match="bench holds"):
        lineups.save(conn, league, cfg, team["id"], 2, assignment)


def test_unused_il_slots_count_as_bench(conn, league, cfg):
    """The whole roster is drafted, so a healthy week must still be legal."""
    team = a_team(conn, league)
    assignment = legal_lineup(conn, league, cfg, team["id"])
    il_used = sum(1 for s in assignment.values() if s == "IL")
    bench = sum(1 for s in assignment.values() if s == "BENCH")
    assert bench <= cfg.bench_size + (cfg.il_size - il_used)
    lineups.save(conn, league, cfg, team["id"], 2, assignment)


def test_a_valid_lineup_saves_and_round_trips(conn, league, cfg):
    team = a_team(conn, league)
    lineups.autofill(conn, league, team["id"], 2, cfg)
    assignment = lineups.stored_lineup(conn, league["id"], team["id"], 2)
    result = lineups.save(conn, league, cfg, team["id"], 2, assignment)
    assert result["active_filled"] == cfg.active_size
    assert lineups.stored_lineup(conn, league["id"], team["id"], 2) == assignment


def test_locked_weeks_reject_edits(conn, league, cfg):
    team = a_team(conn, league)
    lineups.lock_week(conn, league, cfg, 2)
    assignment = lineups.stored_lineup(conn, league["id"], team["id"], 2)
    with pytest.raises(lineups.LineupError, match="locked"):
        lineups.save(conn, league, cfg, team["id"], 2, assignment)


def test_past_weeks_cannot_be_edited(conn, league, cfg):
    conn.execute("UPDATE leagues SET current_week = 4 WHERE id = ?", (league["id"],))
    league = leagues.require_league(conn, league["id"])
    team = a_team(conn, league)
    assignment = lineups.stored_lineup(conn, league["id"], team["id"], 1)
    with pytest.raises(lineups.LineupError, match="in the past"):
        lineups.save(conn, league, cfg, team["id"], 2, assignment)


def test_lock_autofills_managers_who_missed_the_deadline(conn, league, cfg):
    team = a_team(conn, league)
    conn.execute("DELETE FROM lineups WHERE league_id=? AND team_id=? AND week=3",
                 (league["id"], team["id"]))
    lineups.lock_week(conn, league, cfg, 3)
    assignment = lineups.stored_lineup(conn, league["id"], team["id"], 3)
    active = [s for s in assignment.values() if s not in ("BENCH", "IL")]
    assert len(active) == cfg.active_size


def test_il_status_uses_the_monday_of_the_week(conn, league):
    """A stint that starts mid-week does not retroactively lock the week."""
    stints = [{"start_date": "2016-05-04", "end_date": "2016-05-20"}]
    assert il.on_il(stints, "2016-05-02") is None
    assert il.on_il(stints, "2016-05-04") is not None
    assert il.on_il(stints, "2016-05-20") is None


def test_open_ended_stint_runs_to_the_end_of_the_season(conn, league):
    stints = [{"start_date": "2016-08-01", "end_date": None}]
    assert il.on_il(stints, "2016-09-28") is not None


def test_player_il_log_hides_future_stints(conn, league):
    """A player page must not warn you about an injury that hasn't happened."""
    season = league["season_year"]
    row = conn.execute(
        "SELECT player_id, start_date FROM il_stints WHERE season=? ORDER BY start_date DESC LIMIT 1",
        (season,),
    ).fetchone()
    day_before = (dt.date.fromisoformat(row["start_date"]) - dt.timedelta(days=1)).isoformat()
    visible = il.player_il_log(conn, season, row["player_id"], through=day_before)
    assert all(s["start_date"] <= day_before for s in visible)
