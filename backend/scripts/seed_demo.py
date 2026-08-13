"""Seed a demo league so the UI can be exercised with realistic data.

    python -m scripts.seed_demo --weeks 6        # mid-season
    python -m scripts.seed_demo --weeks all      # played out to a champion

Prints the join code and the commissioner token.  Handy for manual QA and for
the browser smoke test in ``scripts/ui_smoke.py``.
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db  # noqa: E402
from app.config import LeagueConfig  # noqa: E402
from app.scoring import ScoringConfig  # noqa: E402
from app.services import (  # noqa: E402
    bots, draft, leagues, minigame, replay, timeline,
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--teams", type=int, default=10)
    ap.add_argument("--humans", type=int, default=3)
    ap.add_argument("--weeks", default="6", help="number of weeks to play, or 'all'")
    ap.add_argument("--name", default="Lockout League")
    ap.add_argument("--seed", type=int, default=11)
    args = ap.parse_args(argv)

    db.init_db()
    rng = random.Random(args.seed)
    conn = db.connect()
    cfg = LeagueConfig.load().merged({"team_count": args.teams, "min_teams": 8})

    created = leagues.create_league(conn, args.name, cfg, ScoringConfig.load())
    league = leagues.require_league(conn, created["id"])
    managers = []
    for i in range(args.humans):
        managers.append(leagues.join(conn, league, f"Manager {i + 1}"))
    for t in leagues.teams(conn, league["id"]):
        leagues.set_locked_in(conn, league["id"], t["id"], True)

    leagues.start_from_lobby(conn, league, rng=rng)
    league = leagues.require_league(conn, created["id"])

    minigame.randomized_order(conn, league["id"], rng=rng)
    draft.initialize(conn, league)
    leagues.set_phase(conn, league["id"], "draft")
    league = leagues.require_league(conn, created["id"])
    while (pick := draft.current_pick(conn, league["id"])) is not None:
        bots.autopick(conn, league, cfg, pick["team_id"])
    replay.start_season(conn, league, cfg)
    league = leagues.require_league(conn, created["id"])

    target = cfg.total_weeks if args.weeks == "all" else int(args.weeks)
    while True:
        league = leagues.require_league(conn, created["id"])
        if (league["current_week"] or 1) > target and args.weeks != "all":
            break
        step = replay.advance_day(conn, league, cfg)
        if step.get("status") == "complete":
            break

    league = leagues.require_league(conn, created["id"])
    line = timeline.describe(conn, league, cfg)
    print(f"code                {league['code']}")
    print(f"commissioner_token  {created['commissioner_token']}")
    for m in managers:
        print(f"manager             {m['name']}  team={m['team_id']}  token={m['manager_token']}")
    print(f"season              {league['season_year']}  phase={league['phase']}")
    print(f"progress            {line['label']}, replayed through {line['as_of']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
