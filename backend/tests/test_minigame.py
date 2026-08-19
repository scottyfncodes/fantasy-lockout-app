"""The Green Light: draft order by reaction, bots at the back."""

from __future__ import annotations

from app.services import minigame


def build(conn, league):
    return minigame.build_round(conn, league["id"], seed=7)


def test_the_light_is_not_green_during_the_countdown(conn, league):
    rnd = build(conn, league)
    rnd.start(now=0.0)
    assert not rnd.is_green(now=0.0)
    assert not rnd.is_green(now=minigame.COUNTDOWN_SECONDS)
    assert rnd.is_green(now=100.0)


def test_reaction_order_decides_the_draft(conn, league):
    rnd = build(conn, league)
    rnd.start(now=0.0)
    green = rnd.green_at
    humans = [c for c in rnd.contestants.values() if not c.is_bot]
    assert len(humans) >= 2, "the fixture needs more than one manager"

    slow, quick = humans[0], humans[1]
    rnd.tap(quick.team_id, now=green + 0.20)
    rnd.tap(slow.team_id, now=green + 0.55)

    order = rnd.standings()
    picks = {e["team_id"]: e["pick"] for e in order}
    assert picks[quick.team_id] < picks[slow.team_id], "faster reaction picks first"
    assert order[0]["reaction"] == 0.2  # rounded by standings()


def test_a_false_start_goes_behind_everyone_who_waited(conn, league):
    rnd = build(conn, league)
    rnd.start(now=0.0)
    green = rnd.green_at
    humans = [c for c in rnd.contestants.values() if not c.is_bot]
    jumper, waiter = humans[0], humans[1]

    rnd.tap(jumper.team_id, now=green - 0.30)
    rnd.tap(waiter.team_id, now=green + 0.90)

    picks = {e["team_id"]: e["pick"] for e in rnd.standings()}
    assert picks[waiter.team_id] < picks[jumper.team_id], (
        "a slow honest tap must still beat a jumped gun, or hammering wins"
    )
    entry = next(e for e in rnd.standings() if e["team_id"] == jumper.team_id)
    assert entry["false_start"] and entry["reaction"] is None


def test_only_the_first_tap_counts(conn, league):
    rnd = build(conn, league)
    rnd.start(now=0.0)
    green = rnd.green_at
    who = next(c for c in rnd.contestants.values() if not c.is_bot)

    assert rnd.tap(who.team_id, now=green + 0.4)["ok"]
    again = rnd.tap(who.team_id, now=green + 0.1)
    assert again["ok"] is False and again["already"]
    assert round(who.reaction, 3) == 0.4, "a second tap must not improve a time"


def test_bots_line_up_at_the_back(conn, league):
    rnd = build(conn, league)
    rnd.start(now=0.0)
    # Nobody taps at all: even then, every human outranks every bot.
    order = rnd.standings()
    first_bot = next(i for i, e in enumerate(order) if e["is_bot"])
    assert all(e["is_bot"] for e in order[first_bot:]), "bots must be contiguous at the back"
    assert not any(e["is_bot"] for e in order[:first_bot])


def test_the_round_ends_once_every_manager_has_tapped(conn, league):
    rnd = build(conn, league)
    rnd.start(now=0.0)
    green = rnd.green_at
    assert not rnd.is_over(now=green + 0.1)
    for c in [c for c in rnd.contestants.values() if not c.is_bot]:
        rnd.tap(c.team_id, now=green + 0.3)
    assert rnd.is_over(now=green + 0.4), "no reason to keep bots waiting"


def test_a_sleeping_manager_does_not_hold_the_lobby_forever(conn, league):
    rnd = build(conn, league)
    rnd.start(now=0.0)
    assert rnd.is_over(now=rnd.green_at + minigame.REACTION_WINDOW_SECONDS + 0.1)


def test_the_state_never_leaks_the_green_light_early(conn, league):
    """A client that could read the moment from the payload would always win."""
    rnd = build(conn, league)
    rnd.start(now=0.0)
    before = rnd.state(now=rnd.green_at - 0.05)
    assert before["green"] is False
    assert "green_at" not in before and "counts_down" in before
    assert rnd.state(now=rnd.green_at + 0.01)["green"] is True


def test_persisted_order_matches_the_standings(conn, league):
    rnd = build(conn, league)
    rnd.start(now=0.0)
    who = next(c for c in rnd.contestants.values() if not c.is_bot)
    rnd.tap(who.team_id, now=rnd.green_at + 0.25)

    order = minigame.persist_results(conn, rnd)
    slots = {r["id"]: r["draft_slot"] for r in conn.execute(
        "SELECT id, draft_slot FROM teams WHERE league_id=?", (league["id"],))}
    for entry in order:
        assert slots[entry["team_id"]] == entry["pick"]
    assert slots[who.team_id] == 1


def test_the_shuffle_fallback_also_keeps_bots_at_the_back(conn, league):
    order = minigame.randomized_order(conn, league["id"])
    first_bot = next((i for i, e in enumerate(order) if e["is_bot"]), len(order))
    assert not any(e["is_bot"] for e in order[:first_bot])
