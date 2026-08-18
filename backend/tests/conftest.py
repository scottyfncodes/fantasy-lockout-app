from __future__ import annotations

import os
import random
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.config import LeagueConfig  # noqa: E402
from app.pipeline import build as build_mod, synthetic  # noqa: E402
from app.scoring import ScoringConfig  # noqa: E402
from app.services import bots, draft, leagues, minigame, players, replay  # noqa: E402

TEST_YEAR = 2016


@pytest.fixture(scope="session")
def template_db(tmp_path_factory) -> Path:
    """A synthetic season plus a fully drafted 8-team league, built once.

    Drafting 8 x 42 players takes a couple of seconds, so it happens once per
    session; each test works on a copy of the finished database and is free to
    mutate it.
    """
    path = tmp_path_factory.mktemp("template") / "template.sqlite3"
    db.init_db(path)
    # Pin the draw to the one season this fixture generates. Seasons are now
    # fetched when drawn, so an unpinned league would draw some other year and
    # find an empty player pool — which is the real behaviour, not a bug.
    cfg = LeagueConfig.load().merged({
        "team_count": 8, "min_teams": 8,
        "eligible_year_min": TEST_YEAR, "eligible_year_max": TEST_YEAR,
    })
    data = synthetic.generate_season(TEST_YEAR, seed=99)

    with db.closing_conn(path) as conn:
        build_mod.store(conn, data, build_mod.assess(data, cfg, None), {"source": "synthetic"})

        created = leagues.create_league(conn, "Test League", cfg, ScoringConfig.load())
        row = leagues.require_league(conn, created["id"])
        for name in ("Alpha", "Bravo", "Charlie"):
            leagues.join(conn, row, name)
        leagues.start_from_lobby(conn, row, rng=random.Random(3))
        row = leagues.require_league(conn, created["id"])

        minigame.randomized_order(conn, row["id"], rng=random.Random(5))
        draft.initialize(conn, row)
        leagues.set_phase(conn, row["id"], "draft")
        row = leagues.require_league(conn, created["id"])
        while (pick := draft.current_pick(conn, row["id"])) is not None:
            assert bots.autopick(conn, row, cfg, pick["team_id"]) is not None
        replay.start_season(conn, row, cfg)
    return path


@pytest.fixture
def db_path(template_db, tmp_path) -> Path:
    copy = tmp_path / "replay.sqlite3"
    shutil.copyfile(template_db, copy)
    os.environ["RETRO_REPLAY_DB"] = str(copy)
    players.invalidate_cache()
    return copy


@pytest.fixture
def conn(db_path):
    connection = db.connect(db_path)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def cfg() -> LeagueConfig:
    return LeagueConfig.load().merged({
        "team_count": 8, "min_teams": 8,
        "eligible_year_min": TEST_YEAR, "eligible_year_max": TEST_YEAR,
    })


@pytest.fixture
def league(conn):
    row = conn.execute("SELECT id FROM leagues LIMIT 1").fetchone()
    return leagues.require_league(conn, row["id"])
