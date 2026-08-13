"""The replay engine end to end: daily scoring, weekly H2H, playoffs."""

from __future__ import annotations

import json

import pytest

from app.services import draft, leagues, lineups, replay, standings, timeline


def advance(conn, league, days):
    for _ in range(days):
        row = leagues.require_league(conn, league["id"])
        step = replay.advance_day(conn, row, cfg_of(row))
        if step.get("status") == "complete":
            return step
    return leagues.require_league(conn, league["id"])


def cfg_of(league):
    return leagues.league_config(league)


def test_a_simulated_day_scores_only_active_players(conn, league, cfg):
    replay.advance_day(conn, league, cfg)
    league = leagues.require_league(conn, league["id"])
    rows = conn.execute(
        "SELECT DISTINCT slot FROM scoring_lines WHERE league_id=?", (league["id"],)
    ).fetchall()
    slots = {r["slot"] for r in rows}
    assert slots and not slots & {"BENCH", "IL"}


def test_points_match_the_scoring_config(conn, league, cfg):
    replay.advance_day(conn, league, cfg)
    row = conn.execute(
        "SELECT points, breakdown_json FROM scoring_lines WHERE league_id=? LIMIT 20",
        (league["id"],),
    ).fetchall()
    for r in row:
        assert r["points"] == pytest.approx(sum(json.loads(r["breakdown_json"]).values()))


def test_simulation_starts_on_the_first_monday(conn, league, cfg):
    step = replay.advance_day(conn, league, cfg)
    week1 = timeline.week(conn, league["season_year"], cfg, 1)
    assert step["date"] == week1.start.isoformat()


def test_the_all_star_week_processes_no_games(conn, league, cfg):
    season = timeline.season_row(conn, league["season_year"])
    asb = season["all_star_monday"]
    cal = timeline.calendar(conn, league["season_year"], cfg)
    assert all(w.start.isoformat() != asb for w in cal)


def test_week_close_awards_the_head_to_head_win(conn, league, cfg):
    for _ in range(9):
        row = leagues.require_league(conn, league["id"])
        replay.advance_day(conn, row, cfg)
    row = leagues.require_league(conn, league["id"])
    replay.close_week(conn, row, cfg, 1)

    matchups = conn.execute(
        "SELECT * FROM matchups WHERE league_id=? AND week=1", (row["id"],)
    ).fetchall()
    assert matchups
    for m in matchups:
        assert m["complete"]
        if m["home_points"] != m["away_points"]:
            expected = m["home_team_id"] if m["home_points"] > m["away_points"] else m["away_team_id"]
            assert m["winner_team_id"] == expected

    table = standings.table(conn, row)
    assert sum(t["wins"] + t["losses"] + t["ties"] for t in table) == len(matchups) * 2


def test_standings_rank_on_record_then_points(conn, league):
    for i, team in enumerate(leagues.teams(conn, league["id"])):
        conn.execute("UPDATE teams SET wins=?, losses=?, points_for=? WHERE id=?",
                     (5, 5, 100 + i, team["id"]))
    table = standings.table(conn, league)
    assert [t["points_for"] for t in table] == sorted(
        (t["points_for"] for t in table), reverse=True
    )


def test_full_season_completes_with_a_champion(conn, league, cfg):
    steps = 0
    while steps < 220:
        row = leagues.require_league(conn, league["id"])
        step = replay.advance_day(conn, row, cfg)
        steps += 1
        if step.get("status") == "complete":
            break
    else:
        pytest.fail("season did not finish")

    row = leagues.require_league(conn, league["id"])
    assert row["phase"] == "complete"
    champ = standings.champion(conn, row, cfg)
    assert champ and champ["name"]

    bracket = standings.bracket(conn, row, cfg)
    stages = [r["stage"] for r in bracket["rounds"]]
    assert stages == ["quarterfinal", "semifinal", "final"]
    assert len(bracket["seeds"]) == cfg.playoff_teams
    assert len(bracket["rounds"][0]["series"]) == cfg.playoff_teams // 2


def test_finals_are_decided_on_two_weeks_combined(conn, league, cfg):
    while True:
        row = leagues.require_league(conn, league["id"])
        step = replay.advance_day(conn, row, cfg)
        if step.get("status") == "complete":
            break
    row = leagues.require_league(conn, league["id"])
    final = next(r for r in standings.bracket(conn, row, cfg)["rounds"] if r["stage"] == "final")
    series = final["series"][0]
    assert sorted(series["weeks"]) == [cfg.total_weeks - 1, cfg.total_weeks]

    legs = conn.execute(
        "SELECT SUM(home_points) h, SUM(away_points) a FROM matchups "
        "WHERE league_id=? AND stage='final'", (row["id"],)
    ).fetchone()
    assert series["home"]["points"] + series["away"]["points"] == pytest.approx(
        (legs["h"] or 0) + (legs["a"] or 0)
    )


def test_regular_season_points_exclude_playoff_weeks(conn, league, cfg):
    """Seeding must not shift once the bracket starts."""
    while True:
        row = leagues.require_league(conn, league["id"])
        step = replay.advance_day(conn, row, cfg)
        if step.get("status") == "complete":
            break
    row = leagues.require_league(conn, league["id"])
    for team in standings.table(conn, row):
        regular = conn.execute(
            "SELECT ROUND(SUM(points),2) p FROM scoring_lines "
            "WHERE league_id=? AND team_id=? AND week <= ?",
            (row["id"], team["id"], cfg.regular_season_weeks),
        ).fetchone()["p"] or 0
        assert team["points_for"] == pytest.approx(regular, abs=0.05)


def test_recap_reports_bonuses_and_top_performers(conn, league, cfg):
    for _ in range(9):
        row = leagues.require_league(conn, league["id"])
        replay.advance_day(conn, row, cfg)
    row = leagues.require_league(conn, league["id"])
    recap = replay.week_recap(conn, row, cfg, 1)
    assert recap["matchups"] and recap["top_performers"]
    assert all("points" in p for p in recap["top_performers"])
    assert all(b["bonus"] in {"CYC", "SLAM"} for b in recap["bonuses"])


def test_snake_draft_order_reverses_every_round():
    order = draft.snake_order(["a", "b", "c"], rounds=4)
    assert order == ["a", "b", "c", "c", "b", "a", "a", "b", "c", "c", "b", "a"]


def test_draft_board_is_the_configured_size(conn, league, cfg):
    total = conn.execute(
        "SELECT COUNT(*) n FROM draft_picks WHERE league_id=?", (league["id"],)
    ).fetchone()["n"]
    assert total == cfg.team_count * cfg.roster_size


def test_bench_and_il_players_score_nothing(conn, league, cfg):
    replay.advance_day(conn, league, cfg)
    league = leagues.require_league(conn, league["id"])
    benched = conn.execute(
        """SELECT COUNT(*) n FROM scoring_lines s
             JOIN lineups l ON l.league_id = s.league_id AND l.team_id = s.team_id
                           AND l.week = s.week AND l.player_id = s.player_id
            WHERE s.league_id = ? AND l.slot IN ('BENCH','IL')""",
        (league["id"],),
    ).fetchone()["n"]
    assert benched == 0


def test_only_the_team_on_the_clock_may_pick(conn, league, cfg):
    """Deterministic guard behind the draft room's one-pick-at-a-time lock."""
    conn.execute("DELETE FROM draft_picks WHERE league_id=?", (league["id"],))
    conn.execute("DELETE FROM rosters WHERE league_id=?", (league["id"],))
    draft.initialize(conn, league)
    pick = draft.current_pick(conn, league["id"])
    other = next(t for t in leagues.teams(conn, league["id"]) if t["id"] != pick["team_id"])
    scoring = leagues.league_scoring(league)
    target = draft.available(conn, league, scoring, limit=1)[0]["player_id"]

    with pytest.raises(draft.DraftError, match="not your pick"):
        draft.make_pick(conn, league, other["id"], target)
    assert draft.make_pick(conn, league, pick["team_id"], target)["player"]["player_id"] == target


def test_the_same_player_cannot_be_drafted_twice(conn, league, cfg):
    taken = conn.execute(
        "SELECT player_id FROM rosters WHERE league_id=? LIMIT 1", (league["id"],)
    ).fetchone()["player_id"]
    conn.execute("DELETE FROM draft_picks WHERE league_id=?", (league["id"],))
    draft.initialize(conn, league)
    pick = draft.current_pick(conn, league["id"])
    with pytest.raises(draft.DraftError, match="already drafted"):
        draft.make_pick(conn, league, pick["team_id"], taken)


def test_bot_finds_a_scarce_position_outside_the_top_of_the_board(conn, league, cfg):
    """Best-available is ranked by points, so the top can be all wrong positions.

    A bot one pick from the end with only a catcher slot open must still find a
    catcher, even if none is ranked highly enough to appear in the default pool.
    """
    from app.services import bots, rosters

    team = leagues.teams(conn, league["id"])[0]
    roster = draft.team_roster(conn, league, team["id"])
    keep = [p for p in roster if "C" not in p["positions"].split(",")][: cfg.roster_size - 1]
    conn.execute("DELETE FROM rosters WHERE league_id=? AND team_id=?", (league["id"], team["id"]))
    conn.executemany(
        "INSERT INTO rosters (league_id, team_id, player_id, acquired_week, acquired_via) "
        "VALUES (?,?,?,0,'draft')",
        [(league["id"], team["id"], p["player_id"]) for p in keep],
    )
    gaps = rosters.unfilled_slots(
        draft.team_roster(conn, league, team["id"]), cfg.active_slots
    )
    if "C" not in gaps:
        pytest.skip("this roster still covers catcher")

    choice = bots.choose_draft_pick(conn, league, cfg, team["id"])
    assert choice is not None, "a bot must never run out of legal picks while one exists"
    assert "C" in choice["positions"].split(",")
