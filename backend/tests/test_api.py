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


TINY_ROSTER = {
    "team_count": 8,
    "min_teams": 8,
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


def test_speed_round_sets_the_draft_order(client):
    league = make_league(client, {**TINY_ROSTER, "speed_round_seconds": 0.4})
    code = league["code"]
    manager = join(client, code, "Tappers")
    client.post(f"/api/leagues/{code}/start",
                headers={"X-Commissioner-Token": league["commissioner_token"]})

    url = f"/ws/{code}/lobby?token={manager['manager_token']}&commish={league['commissioner_token']}"
    with client.websocket_connect(url) as ws:
        assert ws.receive_json()["type"] == "lobby_state"
        ws.send_json({"type": "start_minigame"})

        order = None
        for _ in range(400):
            msg = ws.receive_json()
            if msg["type"] == "minigame_state":
                if msg["state"] == "running":
                    ws.send_json({"type": "tap"})
                assert 0 <= msg["target"]["x"] <= 1
            elif msg["type"] == "draft_order":
                order = msg["order"]
                break
        assert order, "the round never resolved"

    picks = sorted(o["pick"] for o in order)
    assert picks == list(range(1, 9))
    scores = [o["score"] for o in order]
    assert scores == sorted(scores, reverse=True), "highest score picks first"

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
