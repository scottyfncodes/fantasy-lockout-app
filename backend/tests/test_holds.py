"""Deriving holds and pickoffs from event data.

The hold is the one scoring category no source supplies as a column, so the
derivation is tested directly against hand-built event streams — no network,
no Chadwick binaries.
"""

from __future__ import annotations

from app.pipeline.holds import (
    apply_to_pitching_lines,
    derive_holds,
    derive_pickoffs,
    scan_game,
)

GAME = "BOS201905010"


def ev(pitcher, *, inning=1, home_bats=1, away=0, home=0, outs=1, code=2,
       runners=(), game=GAME):
    """One cwevent-shaped row. `home_bats=1` means the away team is pitching."""
    row = {
        "GAME_ID": game, "PIT_ID": pitcher, "INN_CT": inning,
        "BAT_HOME_ID": home_bats, "AWAY_SCORE_CT": away, "HOME_SCORE_CT": home,
        "EVENT_OUTS_CT": outs, "EVENT_CD": code,
        "BASE1_RUN_ID": "", "BASE2_RUN_ID": "", "BASE3_RUN_ID": "",
    }
    for base in runners:
        row[f"BASE{base}_RUN_ID"] = "runner"
    return row


def starter_then(*relief_events, starter="starterA"):
    """A starter's outing followed by the given relief events."""
    return [ev(starter, inning=i, away=3, home=0, outs=3) for i in range(1, 7)] + list(relief_events)


# ---------------------------------------------------------------------------
# the hold rule
# ---------------------------------------------------------------------------

def test_reliever_protecting_a_two_run_lead_earns_a_hold():
    events = starter_then(
        ev("setup", inning=7, away=3, home=1, outs=3),
        ev("closer", inning=9, away=3, home=1, outs=3),
    )
    holds = derive_holds(events, winning_pitcher_id="starterA", saving_pitcher_id="closer")
    assert holds == {"setup": 1}


def test_the_starter_never_earns_a_hold():
    events = starter_then(ev("setup", inning=7, away=3, home=1, outs=3))
    assert "starterA" not in derive_holds(events)


def test_the_saving_pitcher_does_not_also_earn_a_hold():
    events = starter_then(
        ev("setup", inning=8, away=2, home=1, outs=3),
        ev("closer", inning=9, away=2, home=1, outs=3),
    )
    holds = derive_holds(events, winning_pitcher_id="starterA", saving_pitcher_id="closer")
    assert "closer" not in holds and holds["setup"] == 1


def test_the_winning_pitcher_does_not_also_earn_a_hold():
    events = starter_then(ev("setup", inning=7, away=3, home=1, outs=3))
    assert derive_holds(events, winning_pitcher_id="setup") == {}


def test_a_reliever_who_records_no_outs_earns_nothing():
    events = starter_then(ev("shaky", inning=7, away=3, home=1, outs=0))
    assert derive_holds(events) == {}


def test_entering_with_a_big_lead_is_not_a_save_situation():
    """Nine runs up with the bases empty: mop-up work, not a hold."""
    events = starter_then(ev("mopup", inning=8, away=9, home=0, outs=3))
    assert derive_holds(events) == {}


def test_entering_with_a_big_lead_but_the_tying_run_close_still_counts():
    """Bases loaded and a three-run lead keeps the tying run in play."""
    events = starter_then(
        ev("fireman", inning=8, away=3, home=0, outs=1, runners=(1, 2, 3)),
        ev("fireman", inning=8, away=3, home=0, outs=2),
    )
    assert derive_holds(events) == {"fireman": 1}


def test_entering_with_the_game_tied_is_not_a_hold():
    events = starter_then(ev("middle", inning=7, away=2, home=2, outs=3))
    assert derive_holds(events) == {}


def test_entering_while_behind_is_not_a_hold():
    events = starter_then(ev("trailing", inning=7, away=1, home=4, outs=3))
    assert derive_holds(events) == {}


def test_blowing_the_lead_forfeits_the_hold():
    events = starter_then(
        ev("blown", inning=8, away=3, home=1, outs=1),
        ev("blown", inning=8, away=3, home=3, outs=1),  # lead surrendered
    )
    assert derive_holds(events) == {}


def test_two_relievers_can_both_hold_the_same_lead():
    events = starter_then(
        ev("seventh", inning=7, away=4, home=2, outs=3),
        ev("eighth", inning=8, away=4, home=2, outs=3),
        ev("closer", inning=9, away=4, home=2, outs=3),
    )
    holds = derive_holds(events, winning_pitcher_id="starterA", saving_pitcher_id="closer")
    assert holds == {"seventh": 1, "eighth": 1}


def test_the_home_pitching_side_is_read_correctly():
    """With the home team batting (BAT_HOME_ID=1) the away team is pitching."""
    events = [
        ev("awayStarter", inning=i, home_bats=1, away=3, home=1, outs=3) for i in range(1, 7)
    ] + [ev("awaySetup", inning=7, home_bats=1, away=3, home=1, outs=3)]
    assert derive_holds(events, winning_pitcher_id="awayStarter") == {"awaySetup": 1}

    # Now flip it: the away team bats, so the home team's staff is pitching and
    # a 3-1 away lead means the home pitcher is *behind*.
    flipped = [
        ev("homeStarter", inning=i, home_bats=0, away=3, home=1, outs=3) for i in range(1, 7)
    ] + [ev("homeSetup", inning=7, home_bats=0, away=3, home=1, outs=3)]
    assert derive_holds(flipped) == {}


# ---------------------------------------------------------------------------
# pickoffs
# ---------------------------------------------------------------------------

def test_pickoffs_are_counted_per_pitcher():
    events = [
        ev("p1", code=8), ev("p1", code=2), ev("p1", code=8),
        ev("p2", code=8), ev("p2", code=4),  # 4 is a stolen base, not a pickoff
    ]
    assert derive_pickoffs(events) == {"p1": 2, "p2": 1}


def test_no_pickoffs_reports_nothing():
    assert derive_pickoffs([ev("p1", code=2), ev("p1", code=3)]) == {}


# ---------------------------------------------------------------------------
# wiring into parsed box-score lines
# ---------------------------------------------------------------------------

def test_derived_values_land_on_the_pitching_lines():
    events = starter_then(
        ev("setup", inning=8, away=2, home=1, outs=3, code=8),
        ev("closer", inning=9, away=2, home=1, outs=3),
    )
    lines = [
        {"game_id": GAME, "player_id": "starterA", "w": 1, "sv": 0, "hld": 0, "pick": 0},
        {"game_id": GAME, "player_id": "setup", "w": 0, "sv": 0, "hld": 0, "pick": 0},
        {"game_id": GAME, "player_id": "closer", "w": 0, "sv": 1, "hld": 0, "pick": 0},
    ]
    summary = apply_to_pitching_lines(lines, events)

    by_id = {l["player_id"]: l for l in lines}
    assert by_id["setup"]["hld"] == 1
    assert by_id["setup"]["pick"] == 1
    assert by_id["closer"]["hld"] == 0 and by_id["starterA"]["hld"] == 0
    assert summary == {"games": 1, "holds": 1, "pickoffs": 1}


def test_multiple_games_are_kept_separate():
    other = "NYA201905020"
    events = (
        starter_then(ev("setupA", inning=8, away=2, home=1, outs=3))
        + [ev("starterB", inning=i, away=5, home=0, outs=3, game=other) for i in range(1, 7)]
        + [ev("setupB", inning=8, away=5, home=4, outs=3, game=other)]
    )
    lines = [
        {"game_id": GAME, "player_id": "starterA", "w": 1, "sv": 0, "hld": 0, "pick": 0},
        {"game_id": GAME, "player_id": "setupA", "w": 0, "sv": 0, "hld": 0, "pick": 0},
        {"game_id": other, "player_id": "starterB", "w": 1, "sv": 0, "hld": 0, "pick": 0},
        {"game_id": other, "player_id": "setupB", "w": 0, "sv": 0, "hld": 0, "pick": 0},
    ]
    summary = apply_to_pitching_lines(lines, events)
    by_id = {l["player_id"]: l for l in lines}
    assert by_id["setupA"]["hld"] == 1 and by_id["setupB"]["hld"] == 1
    assert summary["games"] == 2 and summary["holds"] == 2


def test_scan_identifies_one_starter_per_side():
    events = [
        ev("homeSP", home_bats=0, outs=3),
        ev("awaySP", home_bats=1, outs=3),
        ev("homeRP", home_bats=0, outs=3),
    ]
    apps = scan_game(events)
    assert apps["homeSP"].is_starter and apps["awaySP"].is_starter
    assert not apps["homeRP"].is_starter
