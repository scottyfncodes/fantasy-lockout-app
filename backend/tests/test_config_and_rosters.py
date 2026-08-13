"""League configuration and roster/eligibility mechanics."""

from __future__ import annotations

import pytest

from app.config import ConfigError, LeagueConfig, pool_depth_check
from app.services import rosters
from app.services.players import eligible_slots

SLOTS = LeagueConfig.load().active_slots


def player(pid, positions, pitcher=False):
    return {"player_id": pid, "name": pid, "positions": positions, "is_pitcher": int(pitcher)}


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("n", [8, 10, 12, 14])
def test_supported_team_counts(n):
    assert LeagueConfig.load().merged({"team_count": n}).team_count == n


def test_team_count_outside_the_supported_range_is_rejected():
    with pytest.raises(ConfigError):
        LeagueConfig.load().merged({"team_count": 16})
    with pytest.raises(ConfigError):
        LeagueConfig.load().merged({"team_count": 6})


def test_odd_team_count_is_rejected():
    with pytest.raises(ConfigError):
        LeagueConfig.load().merged({"team_count": 9})


def test_playoff_field_cannot_exceed_the_league():
    with pytest.raises(ConfigError):
        LeagueConfig.load().merged({"team_count": 8, "min_teams": 8, "playoff_teams": 16})


def test_bench_and_il_are_configurable():
    cfg = LeagueConfig.load().merged({"bench_size": 12, "il_size": 3})
    assert cfg.roster_size == cfg.active_size + 15


def test_roster_discrepancy_is_reported_not_hidden():
    """The rules quote 23 active / 45 total but itemise only 20 slots."""
    cfg = LeagueConfig.load()
    gap = cfg.roster_discrepancy()
    assert gap is not None
    assert gap["itemised_active_size"] == 20 and gap["declared_active_size"] == 23


def test_discrepancy_clears_when_the_slots_add_up():
    cfg = LeagueConfig.load().merged({"active_slots": {
        "C": 1, "1B": 1, "2B": 1, "3B": 1, "SS": 1, "OF": 3, "UTIL": 5, "SP": 2, "RP": 3, "P": 5,
    }})
    assert cfg.active_size == 23 and cfg.roster_size == 45
    assert cfg.roster_discrepancy() is None


def test_pool_depth_check_flags_a_thin_season():
    cfg = LeagueConfig.load().merged({"team_count": 14})
    assert pool_depth_check(cfg, 960)["ok"]
    assert not pool_depth_check(cfg, 640)["ok"]


def test_eligible_years_exclude_2020():
    years = LeagueConfig.load().merged({"eligible_year_max": 2021}).eligible_years()
    assert 2020 not in years and 2019 in years and min(years) == 2000


# ---------------------------------------------------------------------------
# eligibility + matching
# ---------------------------------------------------------------------------

def test_multi_position_players_are_eligible_everywhere_they_played():
    p = player("x", "2B,OF")
    assert set(eligible_slots(p, SLOTS)) == {"2B", "OF", "UTIL"}


def test_util_takes_any_batter_and_p_takes_any_pitcher():
    assert "UTIL" in eligible_slots(player("b", "C"), SLOTS)
    assert "UTIL" not in eligible_slots(player("p", "SP", pitcher=True), SLOTS)
    assert set(eligible_slots(player("p", "SP", pitcher=True), SLOTS)) == {"SP", "P"}
    assert set(eligible_slots(player("r", "RP", pitcher=True), SLOTS)) == {"RP", "P"}


def test_matching_does_not_strand_a_scarce_slot():
    """A greedy fill would put the C/1B man at 1B and leave catcher empty."""
    squad = [player("dual", "C,1B"), player("firstonly", "1B")]
    slots = {"C": 1, "1B": 1}
    assigned = rosters.max_matching(squad, slots)
    assert sorted(assigned.values()) == ["dual", "firstonly"]
    assert not rosters.unfilled_slots(squad, slots)


def test_unfillable_slot_is_reported():
    squad = [player("of1", "OF"), player("of2", "OF")]
    assert set(rosters.unfilled_slots(squad, {"C": 1, "OF": 2})) == {"C"}


def test_draft_feasibility_blocks_painting_yourself_into_a_corner():
    slots = {"C": 1, "OF": 1}
    roster = [player("of1", "OF")]
    ok, why = rosters.draft_feasible(roster, player("of2", "OF"), slots, picks_remaining_after=0)
    assert not ok and "C" in why


def test_draft_feasibility_allows_it_with_picks_to_spare():
    slots = {"C": 1, "OF": 1}
    ok, _ = rosters.draft_feasible([player("of1", "OF")], player("of2", "OF"), slots,
                                   picks_remaining_after=3)
    assert ok


def test_forced_assignment_is_honoured():
    squad = [player("a", "2B,OF"), player("b", "OF")]
    slots = {"2B": 1, "OF": 1}
    assigned = rosters.max_matching(squad, slots, forced={"a": "OF"})
    slot_names = rosters.expand_slots(slots)
    assert assigned[slot_names.index("OF")] == "a"


def test_forcing_an_ineligible_slot_is_refused():
    with pytest.raises(ValueError):
        rosters.max_matching([player("a", "OF")], {"C": 1, "OF": 1}, forced={"a": "C"})


# ---------------------------------------------------------------------------
# lobby sizing
# ---------------------------------------------------------------------------

def test_lobby_shrinks_toward_the_minimum_rather_than_stuffing_bots():
    from app.services.leagues import planned_size
    cfg = LeagueConfig.load().merged({"team_count": 12, "min_teams": 8})
    assert planned_size(cfg, 3) == 8, "three humans should not seat nine bots"
    assert planned_size(cfg, 9) == 10, "sizes round up to keep the count even"
    assert planned_size(cfg, 12) == 12
    assert planned_size(cfg, 14) == 12, "never exceeds the commissioner's target"


def test_final_size_is_written_back_to_the_league_config(conn, cfg):
    """The schedule generator must build fixtures for teams that exist."""
    import random
    from app.scoring import ScoringConfig
    from app.services import leagues, schedule as schedule_svc

    big = LeagueConfig.load().merged({"team_count": 12, "min_teams": 8})
    created = leagues.create_league(conn, "Shrinker", big, ScoringConfig.load())
    row = leagues.require_league(conn, created["id"])
    leagues.join(conn, row, "Only Human")
    leagues.start_from_lobby(conn, row, rng=random.Random(1))

    row = leagues.require_league(conn, created["id"])
    stored = leagues.league_config(row)
    team_ids = [t["id"] for t in leagues.teams(conn, row["id"])]
    assert stored.team_count == len(team_ids) == 8
    schedule_svc.validate(stored, team_ids)  # must not raise


def test_bracket_shrinks_when_the_league_is_smaller_than_the_playoff_field(conn):
    import random
    from app.scoring import ScoringConfig
    from app.services import leagues

    tiny = LeagueConfig.load().merged({"team_count": 8, "min_teams": 4, "playoff_teams": 8})
    created = leagues.create_league(conn, "Tiny", tiny, ScoringConfig.load())
    row = leagues.require_league(conn, created["id"])
    leagues.join(conn, row, "Solo")
    leagues.start_from_lobby(conn, row, rng=random.Random(2))
    stored = leagues.league_config(leagues.require_league(conn, created["id"]))
    assert stored.team_count == 4 and stored.playoff_teams == 4
