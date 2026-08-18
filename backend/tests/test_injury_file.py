"""IL stints read from a transaction export instead of the live feed."""

from __future__ import annotations

import pytest

from app.pipeline import build as build_mod, injury_file
from app.services import il as il_svc

HEADER = "Date,Team,Acquired,Relinquished,Notes,Injury,DL_length,Injury_Type\n"


def write(tmp_path, *lines):
    path = tmp_path / "injuries.csv"
    path.write_text(HEADER + "".join(line + "\n" for line in lines))
    return path


def test_placements_are_read_with_their_nominal_length(tmp_path):
    path = write(
        tmp_path,
        "2016-04-05, Cardinals, , • Matt Holliday, placed on 15 day DL,1,15, back",
        "2016-05-01, Angels, , • Mike Trout, placed on 60 day DL,1,60, wrist",
        "2015-04-05, Cubs, , • Someone Else, placed on 15 day DL,1,15, knee",
    )
    stints = injury_file.parse_placements(injury_file.read_rows(path), 2016)
    assert [s["name"] for s in stints] == ["Matt Holliday", "Mike Trout"]
    assert stints[0]["kind"] == "15-day IL" and stints[1]["kind"] == "60-day IL"
    assert stints[0]["end_date"] is None, "the export has no activations"


def test_a_stint_ends_the_day_the_player_next_appears(tmp_path):
    """The export gives no return date, so the box scores have to supply it."""
    stints = [{"player_id": "holl001", "start_date": "2016-04-05", "end_date": None}]
    appearances = {"holl001": ["2016-04-03", "2016-04-05", "2016-05-02", "2016-05-03"]}

    closed = injury_file.close_stints(stints, appearances)
    assert closed[0]["end_date"] == "2016-05-02", "back on the day he next played"

    # Playing on the day he was placed does not end the stint: the move
    # follows the game.
    assert closed[0]["end_date"] != "2016-04-05"


def test_a_player_who_never_returns_is_out_for_the_season(tmp_path):
    stints = [{"player_id": "done001", "start_date": "2016-08-01", "end_date": None}]
    closed = injury_file.close_stints(stints, {"done001": ["2016-07-30"]})
    assert closed[0]["end_date"] is None

    # And the IL engine reads that as covering every later day.
    assert il_svc.on_il(closed, "2016-09-15") is not None


def test_the_derived_stint_makes_a_player_unstartable_then_startable(tmp_path):
    stint = injury_file.close_stints(
        [{"player_id": "p1", "start_date": "2016-05-04", "end_date": None,
          "kind": "15-day IL", "note": None}],
        {"p1": ["2016-05-04", "2016-06-20"]},
    )
    assert il_svc.on_il(stint, "2016-05-03") is None, "not yet hurt"
    assert il_svc.on_il(stint, "2016-05-10") is not None, "out"
    assert il_svc.on_il(stint, "2016-06-19") is not None, "still out — 15 days is a floor"
    assert il_svc.on_il(stint, "2016-06-20") is None, "back the day he plays"


def test_an_unusable_file_is_refused_rather_than_silently_empty(tmp_path):
    with pytest.raises(injury_file.InjuryFileUnusable):
        injury_file.read_rows(tmp_path / "nope.csv")

    junk = tmp_path / "junk.csv"
    junk.write_text("a,b\n1,2\n")
    with pytest.raises(injury_file.InjuryFileUnusable):
        injury_file.read_rows(junk)

    path = write(tmp_path, "2016-04-05, Cards, , • A Player, placed on 15 day DL,1,15, back")
    with pytest.raises(injury_file.InjuryFileUnusable, match="no rows for 1999"):
        injury_file.build(1999, [], path, [], [])


def test_names_that_cannot_be_matched_are_reported_not_guessed(tmp_path):
    path = write(
        tmp_path,
        "2016-04-05, Cardinals, , • Matt Holliday, placed on 15 day DL,1,15, back",
        "2016-04-06, Cardinals, , • Nobody Here, placed on 15 day DL,1,15, knee",
    )
    players = [{"player_id": "holl001", "name": "Matt Holliday"}]
    stints, report = injury_file.build(
        2016, players, path,
        [{"player_id": "holl001", "date": "2016-04-20"}], [],
    )
    assert [s["player_id"] for s in stints] == ["holl001"]
    assert stints[0]["end_date"] == "2016-04-20"
    assert report["unmatched_names"] == 1
    assert report["ended_by_return"] == 1


def test_prune_takes_unwanted_seasons_out_of_the_draw(conn):
    kept = build_mod.prune_to(conn, keep=[2016])
    remaining = {r["year"] for r in conn.execute(
        "SELECT year FROM seasons WHERE eligible = 1")}
    assert remaining <= {2016}, "only the configured years may still be drawn"
    if kept:
        assert all("configured season range" in reason for _y, reason in kept)
