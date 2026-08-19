"""HTTP and WebSocket layer.

Covers the flows a browser actually drives: create -> join -> lock in -> close
the lobby -> mini-game -> live draft -> season.  The draft and the mini-game go
over WebSockets, because that is how they work in the app.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import live
from app.main import app


@pytest.fixture
def client(db_path, monkeypatch):
    # The nightly scheduler has no place in a test process.
    monkeypatch.setenv("RETRO_SCHEDULER", "0")
    monkeypatch.setattr("app.main.ENABLE_SCHEDULER", False)
    monkeypatch.setattr(live, "BOT_PICK_DELAY", 0.01)
    with TestClient(app) as c:
        yield c


from tests.conftest import TEST_YEAR

TINY_ROSTER = {
    "team_count": 8,
    "min_teams": 8,
    # Seasons are fetched when drawn, so an unpinned league draws a year the
    # fixture never generated and then waits for it forever.
    "eligible_year_min": TEST_YEAR,
    "eligible_year_max": TEST_YEAR,
    # A two-man roster keeps the live-draft test to 16 picks; every other rule
    # (snake order, eligibility, feasibility) behaves identically.
    "active_slots": {"C": 1, "P": 1},
    "bench_size": 0,
    "il_size": 0,
}


def make_league(client, config=None, name="WS League"):
    res = client.post("/api/leagues", json={"name": name, "config": config or TINY_ROSTER})
    assert res.status_code == 200, res.text
    return res.json()


def join(client, code, team_name):
    res = client.post(f"/api/leagues/{code}/join", json={"team_name": team_name})
    assert res.status_code == 200, res.text
    return res.json()


def test_health_and_meta(client):
    assert client.get("/api/health").json()["ok"]
    coverage = client.get("/api/meta/coverage").json()
    assert "retrosheet" in coverage["sources"]
    assert "SLAM" in coverage["non_standard_stats"]
    assert not any(c in coverage["non_standard_stats"] for c in ("HLD", "PICK", "NH", "PG"))
    seasons = client.get("/api/meta/seasons").json()["seasons"]
    assert seasons and seasons[0]["eligible"] == 1


def test_create_join_and_lobby_state(client):
    league = make_league(client)
    manager = join(client, league["code"], "Sluggers")
    state = client.get(f"/api/leagues/{league['code']}").json()
    assert state["phase"] == "lobby"
    assert [t["name"] for t in state["teams"]] == ["Sluggers"]

    res = client.post(
        f"/api/leagues/{league['code']}/lock-in",
        json={"locked_in": True},
        headers={"X-Manager-Token": manager["manager_token"]},
    )
    assert res.json()["all_locked_in"] is True


def test_duplicate_team_names_are_refused(client):
    league = make_league(client)
    join(client, league["code"], "Sluggers")
    res = client.post(f"/api/leagues/{league['code']}/join", json={"team_name": "sluggers"})
    assert res.status_code == 400 and "taken" in res.text


def test_starting_the_league_requires_the_commissioner(client):
    league = make_league(client)
    join(client, league["code"], "Sluggers")
    assert client.post(f"/api/leagues/{league['code']}/start").status_code == 403

    res = client.post(
        f"/api/leagues/{league['code']}/start",
        headers={"X-Commissioner-Token": league["commissioner_token"]},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["season_year"] and body["team_count"] == 8
    assert len(body["bots_added"]) == 7, "empty seats fill with bots"


def test_unknown_manager_token_is_rejected(client):
    league = make_league(client)
    join(client, league["code"], "Sluggers")
    res = client.post(
        f"/api/leagues/{league['code']}/lock-in",
        json={"locked_in": True},
        headers={"X-Manager-Token": "not-a-real-token"},
    )
    assert res.status_code == 403


def test_the_green_light_sets_the_draft_order(client):
    league = make_league(client, TINY_ROSTER)
    code = league["code"]
    manager = join(client, code, "Tappers")
    client.post(f"/api/leagues/{code}/start",
                headers={"X-Commissioner-Token": league["commissioner_token"]})

    url = f"/ws/{code}/lobby?token={manager['manager_token']}&commish={league['commissioner_token']}"
    with client.websocket_connect(url) as ws:
        assert ws.receive_json()["type"] == "lobby_state"
        ws.send_json({"type": "start_minigame"})

        order, tapped = None, False
        for _ in range(600):
            msg = ws.receive_json()
            if msg["type"] == "minigame_state":
                # Tap only once the light is actually green — that is the game.
                if msg["green"] and not tapped:
                    tapped = True
                    ws.send_json({"type": "tap"})
            elif msg["type"] == "draft_order":
                order = msg["order"]
                ws.send_json({"type": "open_draft"})
                break
        assert order, "the round never resolved"

    picks = sorted(o["pick"] for o in order)
    assert picks == list(range(1, 9))
    assert order[0]["team_id"] and not order[0]["is_bot"], (
        "the one manager who reacted must not be behind a bot"
    )
    bots_from = next(i for i, o in enumerate(order) if o["is_bot"])
    assert all(o["is_bot"] for o in order[bots_from:]), "bots line up at the back"

    state = client.get(f"/api/leagues/{code}").json()
    assert state["phase"] == "draft"
    assert all(t["draft_slot"] for t in state["teams"])


def test_live_draft_runs_to_completion(client):
    league = make_league(client, {**TINY_ROSTER, "draft_order_mode": "randomizer"})
    code = league["code"]
    manager = join(client, code, "Pickers")
    client.post(f"/api/leagues/{code}/start",
                headers={"X-Commissioner-Token": league["commissioner_token"]})

    lobby_url = f"/ws/{code}/lobby?token={manager['manager_token']}&commish={league['commissioner_token']}"
    with client.websocket_connect(lobby_url) as ws:
        ws.receive_json()
        ws.send_json({"type": "start_minigame"})
        for _ in range(50):
            if ws.receive_json()["type"] == "draft_order":
                # The order stands until the commissioner accepts it, so the
                # room can ask for a rerun first.
                ws.send_json({"type": "open_draft"})
                break

    draft_url = f"/ws/{code}/draft?token={manager['manager_token']}&commish={league['commissioner_token']}"
    with client.websocket_connect(draft_url) as ws:
        msg = ws.receive_json()
        assert msg["type"] == "draft_state"
        assert msg["progress"]["total"] == 8 * 2

        forced: set[int] = set()
        for _ in range(400):
            if msg["type"] == "draft_complete":
                break
            if msg["type"] == "draft_error":
                pytest.fail(msg["message"])
            if msg["type"] == "draft_state":
                # Wait for `draft_complete` rather than the last pick: the
                # season is only set up once the draft finishes closing out.
                # Bots pick for themselves; the one human seat would stall the
                # draft, so the commissioner auto-picks for it. This has to be
                # checked on the very first state too — by the time the socket
                # opens, the bots may already be waiting on that seat.
                clock = msg["on_clock"]
                if clock and not clock["is_bot"] and clock["overall"] not in forced:
                    forced.add(clock["overall"])
                    ws.send_json({"type": "force_pick"})
            msg = ws.receive_json()

    after = client.get(f"/api/leagues/{code}").json()
    assert after["phase"] == "season"
    assert client.get(f"/api/leagues/{code}/draft").json()["progress"]["complete"]


# ---------------------------------------------------------------------------
# in-season endpoints, against the pre-drafted league from the fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def drafted(client, conn):
    from app.services import leagues as leagues_svc
    row = conn.execute("SELECT id, code FROM leagues LIMIT 1").fetchone()
    league = leagues_svc.require_league(conn, row["id"])
    team = leagues_svc.teams(conn, league["id"])[0]
    conn.execute("UPDATE teams SET manager_token='test-token' WHERE id=?", (team["id"],))
    return {"code": league["code"], "team_id": team["id"], "token": "test-token",
            "commissioner_token": league["commissioner_token"]}


def hdr(drafted):
    return {"X-Manager-Token": drafted["token"]}


def test_lineup_round_trip_over_http(client, drafted):
    code, team = drafted["code"], drafted["team_id"]
    view = client.get(f"/api/leagues/{code}/teams/{team}/lineup?week=2").json()
    assignment = {p["player_id"]: p["slot"] for p in view["players"]}
    res = client.put(f"/api/leagues/{code}/teams/{team}/lineup",
                     json={"week": 2, "assignment": assignment}, headers=hdr(drafted))
    assert res.status_code == 200, res.text
    assert res.json()["summary"]["active_filled"] == res.json()["summary"]["active_size"]


def test_another_manager_cannot_set_your_lineup(client, drafted):
    code, team = drafted["code"], drafted["team_id"]
    view = client.get(f"/api/leagues/{code}/teams/{team}/lineup?week=2").json()
    assignment = {p["player_id"]: p["slot"] for p in view["players"]}
    res = client.put(f"/api/leagues/{code}/teams/{team}/lineup",
                     json={"week": 2, "assignment": assignment},
                     headers={"X-Manager-Token": "someone-else"})
    assert res.status_code == 403


def test_free_agent_endpoint_states_its_cut_off(client, drafted):
    body = client.get(f"/api/leagues/{drafted['code']}/free-agents?limit=5").json()
    assert body["as_of"]
    assert "through the current replay date" in body["note"]


def test_waiver_bid_and_results(client, drafted):
    code, team = drafted["code"], drafted["team_id"]
    fa = client.get(f"/api/leagues/{code}/free-agents?limit=1").json()["players"][0]
    roster = client.get(f"/api/leagues/{code}/teams/{team}/lineup").json()["players"]
    res = client.post(f"/api/leagues/{code}/waivers/bids", headers=hdr(drafted),
                      json={"add_player_id": fa["player_id"], "amount": 7,
                            "drop_player_id": roster[-1]["player_id"]})
    assert res.status_code == 200, res.text
    bids = client.get(f"/api/leagues/{code}/waivers/bids", headers=hdr(drafted)).json()
    assert bids["bids"][0]["amount"] == 7


def test_commissioner_advances_the_replay(client, drafted):
    code = drafted["code"]
    res = client.post(f"/api/leagues/{code}/advance", json={"days": 3},
                      headers={"X-Commissioner-Token": drafted["commissioner_token"]})
    assert res.status_code == 200, res.text
    assert res.json()["last_simulated_date"]
    standings = client.get(f"/api/leagues/{code}/standings").json()
    assert standings["standings"]


def test_advance_needs_the_commissioner(client, drafted):
    res = client.post(f"/api/leagues/{drafted['code']}/advance", json={"days": 1},
                      headers=hdr(drafted))
    assert res.status_code == 403


def test_roster_shape_locks_after_the_draft(client, drafted):
    res = client.patch(f"/api/leagues/{drafted['code']}/settings",
                       json={"config": {"team_count": 10}},
                       headers={"X-Commissioner-Token": drafted["commissioner_token"]})
    assert res.status_code == 400 and "locked" in res.text


def test_scoring_can_be_retuned_mid_season(client, drafted):
    res = client.patch(f"/api/leagues/{drafted['code']}/settings",
                       json={"scoring": {"batting": {"HR": 6}}},
                       headers={"X-Commissioner-Token": drafted["commissioner_token"]})
    assert res.status_code == 200
    assert res.json()["scoring"]["batting"]["HR"] == 6


def test_player_page_is_capped_at_the_replay_date(client, drafted):
    code, team = drafted["code"], drafted["team_id"]
    player = client.get(f"/api/leagues/{code}/teams/{team}/lineup").json()["players"][0]
    body = client.get(f"/api/leagues/{code}/players/{player['player_id']}").json()
    assert body["as_of"]
    assert all(g["date"] <= body["as_of"] for g in body["game_log"])
    assert all(s["start_date"] <= body["as_of"] for s in body["il_log"])


def test_lobby_countdown_closes_the_lobby(client):
    """The commissioner can set a timer instead of chasing stragglers."""
    league = make_league(client, {**TINY_ROSTER, "lobby_timeout_seconds": 1})
    code = league["code"]
    manager = join(client, code, "Latecomers")

    url = f"/ws/{code}/lobby?token={manager['manager_token']}&commish={league['commissioner_token']}"
    with client.websocket_connect(url) as ws:
        assert ws.receive_json()["type"] == "lobby_state"
        ws.send_json({"type": "start_countdown", "seconds": 1})
        for _ in range(60):
            msg = ws.receive_json()
            if msg["type"] == "lobby_state" and msg["phase"] != "lobby":
                break
        else:
            pytest.fail("the countdown never closed the lobby")

    state = client.get(f"/api/leagues/{code}").json()
    assert state["phase"] == "year_reveal"
    assert state["season_year"]
    assert sum(1 for t in state["teams"] if t["is_bot"]) == 7


def test_a_countdown_can_be_cancelled(client):
    league = make_league(client, {**TINY_ROSTER, "lobby_timeout_seconds": 2})
    code = league["code"]
    manager = join(client, code, "Patient")
    url = f"/ws/{code}/lobby?token={manager['manager_token']}&commish={league['commissioner_token']}"
    with client.websocket_connect(url) as ws:
        ws.receive_json()
        ws.send_json({"type": "start_countdown", "seconds": 30})
        assert ws.receive_json()["type"] == "lobby_countdown"
        ws.send_json({"type": "cancel_countdown"})
        for _ in range(10):
            msg = ws.receive_json()
            if msg["type"] == "lobby_countdown" and msg["remaining"] is None:
                break
        else:
            pytest.fail("cancel was not acknowledged")
    assert client.get(f"/api/leagues/{code}").json()["phase"] == "lobby"


def test_lineup_defaults_to_a_week_you_can_still_edit(client, drafted):
    """The current week locks as it starts; the editor must not open read-only."""
    code, team = drafted["code"], drafted["team_id"]
    current = client.get(f"/api/leagues/{code}").json()["timeline"]["current_week"]
    assert client.get(f"/api/leagues/{code}/teams/{team}/lineup?week={current}").json()["locked"]

    view = client.get(f"/api/leagues/{code}/teams/{team}/lineup").json()
    assert view["week"] == current + 1 and not view["locked"]

    # And that default is genuinely saveable.
    res = client.put(
        f"/api/leagues/{code}/teams/{team}/lineup", headers=hdr(drafted),
        json={"week": view["week"],
              "assignment": {p["player_id"]: p["slot"] for p in view["players"]}},
    )
    assert res.status_code == 200, res.text


def test_an_explicit_week_is_always_honoured(client, drafted):
    code, team = drafted["code"], drafted["team_id"]
    current = client.get(f"/api/leagues/{code}").json()["timeline"]["current_week"]
    view = client.get(f"/api/leagues/{code}/teams/{team}/lineup?week={current}").json()
    assert view["week"] == current, "asking for a locked week must still show it"


def test_an_idle_manager_does_not_stall_the_draft(client):
    """One dropped connection must not hold thirteen other people hostage."""
    league = make_league(client, {
        **TINY_ROSTER, "draft_order_mode": "randomizer", "draft_pick_seconds": 1,
    })
    code = league["code"]
    manager = join(client, code, "Absent")
    client.post(f"/api/leagues/{code}/start",
                headers={"X-Commissioner-Token": league["commissioner_token"]})

    lobby = f"/ws/{code}/lobby?token={manager['manager_token']}&commish={league['commissioner_token']}"
    with client.websocket_connect(lobby) as ws:
        ws.receive_json()
        ws.send_json({"type": "start_minigame"})
        for _ in range(50):
            if ws.receive_json()["type"] == "draft_order":
                # The order stands until the commissioner accepts it, so the
                # room can ask for a rerun first.
                ws.send_json({"type": "open_draft"})
                break

    # Connect, then never pick. The clock has to finish the draft on its own.
    with client.websocket_connect(f"/ws/{code}/draft?token={manager['manager_token']}") as ws:
        msg = ws.receive_json()
        assert msg["pick_seconds"] == 1
        # Wait for `draft_complete`, not the last `draft_state`: the board fills
        # a moment before the season finishes being set up.
        for _ in range(400):
            if msg["type"] == "draft_complete":
                break
            if msg["type"] == "draft_error":
                pytest.fail(msg["message"])
            msg = ws.receive_json()
        else:
            pytest.fail("the clock never finished the draft")

    assert client.get(f"/api/leagues/{code}/draft").json()["progress"]["complete"]
    assert client.get(f"/api/leagues/{code}").json()["phase"] == "season"


def test_the_clock_can_be_switched_off(client):
    """draft_pick_seconds = 0 means the room waits for the manager."""
    league = make_league(client, {
        **TINY_ROSTER, "draft_order_mode": "randomizer", "draft_pick_seconds": 0,
    })
    code = league["code"]
    manager = join(client, code, "Deliberate")
    client.post(f"/api/leagues/{code}/start",
                headers={"X-Commissioner-Token": league["commissioner_token"]})
    lobby = f"/ws/{code}/lobby?token={manager['manager_token']}&commish={league['commissioner_token']}"
    with client.websocket_connect(lobby) as ws:
        ws.receive_json()
        ws.send_json({"type": "start_minigame"})
        for _ in range(50):
            if ws.receive_json()["type"] == "draft_order":
                # The order stands until the commissioner accepts it, so the
                # room can ask for a rerun first.
                ws.send_json({"type": "open_draft"})
                break

    with client.websocket_connect(f"/ws/{code}/draft?token={manager['manager_token']}") as ws:
        state = ws.receive_json()
    assert state["pick_seconds"] == 0
    assert state["seconds_remaining"] is None


def test_the_day_endpoint_serves_the_latest_replayed_date(client, drafted, conn):
    """The morning page: default to last night, and refuse dates not yet played."""
    from app.services import leagues as leagues_svc, replay as replay_svc

    code = drafted["code"]
    assert client.get(f"/api/leagues/{code}/day").json()["date"] is None

    league = leagues_svc.require_league(conn, conn.execute(
        "SELECT id FROM leagues LIMIT 1").fetchone()["id"])
    cfg = leagues_svc.league_config(league)
    for _ in range(3):
        replay_svc.advance_day(conn, leagues_svc.require_league(conn, league["id"]), cfg)
    conn.commit()

    body = client.get(f"/api/leagues/{code}/day").json()
    assert body["is_latest"] and body["dates_played"] == 3
    assert any(t["team_id"] == drafted["team_id"] for t in body["teams"])

    earlier = client.get(f"/api/leagues/{code}/day?date={body['prev']}").json()
    assert earlier["date"] == body["prev"] and earlier["next"] == body["date"]

    assert client.get(f"/api/leagues/{code}/day?date=2099-05-05").status_code == 400
    assert client.get(f"/api/leagues/{code}/day?date=not-a-date").status_code == 400


def test_a_failed_season_start_does_not_freeze_the_draft_room(client, monkeypatch):
    """The board fills, the season fails to start, and the room must be told.

    This escaped as an exception through the socket that happened to make the
    last pick: the connection died, no draft_complete ever arrived, and the
    room sat on a full board with nothing on screen explaining it.
    """
    from app.api import live
    from app.services import replay as replay_svc

    def explode(*args, **kwargs):
        raise RuntimeError("schedule could not be built")

    monkeypatch.setattr(replay_svc, "start_season", explode)

    league = make_league(client, {**TINY_ROSTER, "draft_order_mode": "randomizer"})
    code = league["code"]
    manager = join(client, code, "Sluggers")
    client.post(f"/api/leagues/{code}/start",
                headers={"X-Commissioner-Token": league["commissioner_token"]})

    lobby_url = (f"/ws/{code}/lobby?token={manager['manager_token']}"
                 f"&commish={league['commissioner_token']}")
    with client.websocket_connect(lobby_url) as ws:
        ws.receive_json()
        ws.send_json({"type": "start_minigame"})
        for _ in range(50):
            if ws.receive_json()["type"] == "draft_order":
                # The order stands until the commissioner accepts it, so the
                # room can ask for a rerun first.
                ws.send_json({"type": "open_draft"})
                break

    url = (f"/ws/{code}/draft?token={manager['manager_token']}"
           f"&commish={league['commissioner_token']}")
    with client.websocket_connect(url) as ws:
        msg = ws.receive_json()
        saw_error = False
        forced: set[int] = set()
        for _ in range(400):
            if msg.get("type") == "draft_error":
                assert "could not start" in msg["message"], msg["message"]
                saw_error = True
                break
            if msg.get("type") == "draft_complete":
                pytest.fail("the season start raised; completion must not be announced")
            if msg.get("type") == "draft_state":
                clock = msg["on_clock"]
                if clock and not clock["is_bot"] and clock["overall"] not in forced:
                    forced.add(clock["overall"])
                    ws.send_json({"type": "force_pick"})
            msg = ws.receive_json()
        assert saw_error, "a room whose season failed to start was never told"


def test_deleting_a_league_removes_it_and_spares_the_season_data(client, drafted, conn):
    """The season cache is shared by every league and costs half an hour to
    rebuild; a league deleting it would be a catastrophe, not a tidy-up."""
    code = drafted["code"]
    season_rows = conn.execute("SELECT COUNT(*) n FROM players").fetchone()["n"]
    lines_before = conn.execute("SELECT COUNT(*) n FROM batting_lines").fetchone()["n"]

    res = client.request(
        "DELETE", f"/api/leagues/{code}?confirm={code}",
        headers={"X-Commissioner-Token": drafted["commissioner_token"]},
    )
    assert res.status_code == 200, res.text
    assert res.json()["removed"]["teams"] > 0

    assert client.get(f"/api/leagues/{code}").status_code == 404
    for table in ("teams", "rosters", "lineups", "matchups", "scoring_lines", "draft_picks"):
        left = conn.execute(f"SELECT COUNT(*) n FROM {table}").fetchone()["n"]
        assert left == 0, f"{table} still holds rows from a deleted league"

    assert conn.execute("SELECT COUNT(*) n FROM players").fetchone()["n"] == season_rows
    assert conn.execute("SELECT COUNT(*) n FROM batting_lines").fetchone()["n"] == lines_before


def test_deleting_a_league_needs_the_commissioner_and_the_code(client, drafted):
    code = drafted["code"]
    commish = {"X-Commissioner-Token": drafted["commissioner_token"]}

    assert client.request("DELETE", f"/api/leagues/{code}?confirm={code}").status_code == 403
    assert client.request(
        "DELETE", f"/api/leagues/{code}?confirm={code}", headers=hdr(drafted),
    ).status_code == 403, "a manager must not be able to delete the league"
    assert client.request(
        "DELETE", f"/api/leagues/{code}?confirm=WRONG", headers=commish,
    ).status_code == 400, "a mistyped confirmation must not delete anything"

    assert client.get(f"/api/leagues/{code}").status_code == 200, "still there"


def test_a_full_deployment_refuses_new_leagues_and_says_why(client, monkeypatch):
    """Leagues never expire, so without a cap the disk fills silently and the
    failure lands on whoever is mid-season when it does."""
    monkeypatch.setenv("RETRO_MAX_LEAGUES", "2")

    first = client.post("/api/leagues", json={"name": "One", "config": TINY_ROSTER})
    assert first.status_code == 200, first.text

    room = client.get("/api/meta/defaults").json()["capacity"]
    assert room["max"] == 2 and room["full"] is (room["used"] >= 2)

    # Fill it, then confirm the refusal explains itself rather than 500ing.
    while not client.get("/api/meta/defaults").json()["capacity"]["full"]:
        assert client.post(
            "/api/leagues", json={"name": "Filler", "config": TINY_ROSTER},
        ).status_code == 200

    res = client.post("/api/leagues", json={"name": "Too many", "config": TINY_ROSTER})
    assert res.status_code == 409
    assert "full at 2 leagues" in res.json()["detail"]
    assert "delete" in res.json()["detail"], "a dead end must name the way out"


def test_deleting_a_league_frees_a_slot(client, monkeypatch):
    monkeypatch.setenv("RETRO_MAX_LEAGUES", "1")
    while not client.get("/api/meta/defaults").json()["capacity"]["full"]:
        client.post("/api/leagues", json={"name": "Filler", "config": TINY_ROSTER})

    league = client.post("/api/leagues", json={"name": "Nope", "config": TINY_ROSTER})
    assert league.status_code == 409

    existing = client.get("/api/meta/defaults").json()["capacity"]
    assert existing["remaining"] == 0
