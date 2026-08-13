"""What is each roster slot actually worth under the current scoring config?

Removing a scoring category does not just subtract points — it changes which
positions are worth drafting.  This prints the numbers a commissioner needs to
decide whether the roster shape still makes sense:

* what the *starters* at each slot score (the top `teams x slots` players),
* what *replacement level* is (the best player who would not be started),
* the gap between them, which is what a slot is really worth,
* and how many points the average team gets from each slot.

    python -m scripts.balance_report --year 2019
    python -m scripts.balance_report --year 2019 --compare '{"pitching":{"SV":20}}'

`--compare` re-runs the whole thing with those scoring overrides applied and
shows the delta, which is how to answer "what would doubling saves do to relief
pitchers?" without guessing. It warns when an override names a category the
cached season has no data for, since that produces a zero delta that means
"nothing to score", not "no effect".

Two caveats, stated plainly:

* A player eligible at several slots is counted in each of them, so the columns
  do not sum to a league total. That is the standard convention for positional
  scarcity and the right one for comparing slots against each other. The SOLE
  column shows how much of a slot's strength is borrowed from players who are
  really something else.
* Run this against a **real** cached season before making a rules decision. On
  a synthetic season the numbers describe the generator's assumptions about
  playing time, not baseball.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db  # noqa: E402
from app.config import LeagueConfig  # noqa: E402
from app.scoring import ScoringConfig  # noqa: E402
from app.services import players as players_svc  # noqa: E402


def slot_table(
    conn, year: int, cfg: LeagueConfig, scoring: ScoringConfig
) -> dict[str, dict[str, float]]:
    totals = players_svc.season_totals(conn, year, scoring)
    pool = players_svc.list_players(conn, year)
    points = {p["player_id"]: totals.get(p["player_id"], {}).get("points", 0.0) for p in pool}

    out: dict[str, dict[str, float]] = {}
    for slot, count in cfg.active_slots.items():
        eligible = [
            (points[p["player_id"]], _is_sole(p, slot, cfg))
            for p in pool
            if slot in players_svc.eligible_slots(p, cfg.active_slots.keys())
        ]
        eligible.sort(key=lambda e: -e[0])
        startable = cfg.team_count * count
        if len(eligible) <= startable:
            continue
        starters = [pts for pts, _ in eligible[:startable]]
        replacement = eligible[startable][0]
        out[slot] = {
            "slots_per_team": count,
            "pool": len(eligible),
            "best": starters[0],
            "starter_avg": statistics.mean(starters),
            "replacement": replacement,
            "value_over_replacement": statistics.mean(starters) - replacement,
            "team_points": statistics.mean(starters) * count,
            # How many startable players belong to this slot alone. A low number
            # means the slot is being propped up by players borrowed from
            # elsewhere — an RP slot filled by swingmen looks healthy while
            # genuine relievers are worthless.
            "sole": sum(1 for _, sole in eligible[:startable] if sole),
        }
    return out


def _is_sole(player: dict[str, Any], slot: str, cfg: LeagueConfig) -> bool:
    """Is this player eligible *only* at `slot`, ignoring the catch-all slots?"""
    specific = {
        s for s in players_svc.eligible_slots(player, cfg.active_slots.keys())
        if s not in ("UTIL", "P")
    }
    return specific == {slot}


TEAM_COUNT = [12]  # set once from the CLI so the renderer can show "sole / startable"


def render(title: str, table: dict[str, dict[str, float]], order: list[str]) -> None:
    print(f"\n{title}")
    print(f"  {'SLOT':<6} {'x':>2} {'POOL':>5} {'BEST':>8} {'STARTER':>8} "
          f"{'REPL':>8} {'VOR':>8} {'PER TEAM':>9} {'SOLE':>7}")
    for slot in order:
        r = table.get(slot)
        if not r:
            continue
        startable = int(r["slots_per_team"]) * TEAM_COUNT[0]
        # Sole eligibility is meaningless for the catch-all slots: everyone in
        # them is by definition borrowed from a specific position.
        sole = "  any  " if slot in ("UTIL", "P") else f"{int(r['sole']):>3}/{startable:<3}"
        print(f"  {slot:<6} {int(r['slots_per_team']):>2} {int(r['pool']):>5} "
              f"{r['best']:>8.0f} {r['starter_avg']:>8.0f} {r['replacement']:>8.0f} "
              f"{r['value_over_replacement']:>8.0f} {r['team_points']:>9.0f} {sole}")


def render_delta(
    current: dict[str, dict[str, float]], overridden: dict[str, dict[str, float]],
    order: list[str],
) -> None:
    """What the overrides would change, relative to the config in force now."""
    print("\nCHANGE (overrides - current)")
    print(f"  {'SLOT':<6} {'STARTER':>9} {'REPL':>9} {'VOR':>9} {'PER TEAM':>10}")
    for slot in order:
        b, a = current.get(slot), overridden.get(slot)
        if not b or not a:
            continue
        print(f"  {slot:<6} {a['starter_avg'] - b['starter_avg']:>+9.0f} "
              f"{a['replacement'] - b['replacement']:>+9.0f} "
              f"{a['value_over_replacement'] - b['value_over_replacement']:>+9.0f} "
              f"{a['team_points'] - b['team_points']:>+10.0f}")


# Scoring categories and the pitching_lines column each one reads. A category
# whose column no longer exists cannot change anything, and a silent zero delta
# would read as "this made no difference" rather than "there is no data".
_PITCHING_COLUMNS = {"IP": "outs", "W": "w", "CG": "cg", "SV": "sv", "ER": "er", "K": "so"}
_BATTING_COLUMNS = {"R": "r", "1B": "b1", "2B": "b2", "3B": "b3", "HR": "hr", "RBI": "rbi",
                    "SB": "sb", "BB": "bb", "IBB": "ibb", "HBP": "hbp", "K": "so",
                    "SLAM": "slam"}


def _warn_about_missing_data(conn, year: int, overrides: dict[str, Any]) -> None:
    tables = {
        "batting": ({r[1] for r in conn.execute("PRAGMA table_info(batting_lines)")},
                    _BATTING_COLUMNS),
        "pitching": ({r[1] for r in conn.execute("PRAGMA table_info(pitching_lines)")},
                     _PITCHING_COLUMNS),
    }
    for half, cats in overrides.items():
        if half not in tables:
            continue
        columns, mapping = tables[half]
        for category in cats:
            source = mapping.get(category)
            if source is None or source not in columns:
                print(f"\n  !! {half}.{category} has no data in the cached season — "
                      f"the comparison below will show no change for it, which means "
                      f"'nothing to score', not 'no effect'.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--teams", type=int, default=None)
    ap.add_argument("--compare", type=str, default=None,
                    help='scoring overrides as JSON, e.g. \'{"pitching":{"SV":20}}\'')
    args = ap.parse_args(argv)

    cfg = LeagueConfig.load()
    if args.teams:
        cfg = cfg.merged({"team_count": args.teams})
    scoring = ScoringConfig.load()
    order = list(cfg.active_slots)

    with db.closing_conn() as conn:
        if not conn.execute("SELECT 1 FROM seasons WHERE year = ?", (args.year,)).fetchone():
            print(f"season {args.year} is not cached — run app.pipeline.build first")
            return 1

        TEAM_COUNT[0] = cfg.team_count
        print(f"{args.year} · {cfg.team_count} teams · "
              f"{cfg.active_size} starters ({cfg.roster_size} rostered)")
        print(f"pitching categories: {', '.join(scoring.pitching)}")
        current = slot_table(conn, args.year, cfg, scoring)
        render("CURRENT SCORING", current, order)

        if args.compare:
            overrides = json.loads(args.compare)
            merged = scoring.to_dict()
            for half in ("batting", "pitching", "options"):
                if half in overrides:
                    merged[half] = {**merged.get(half, {}), **overrides[half]}
            other = ScoringConfig.from_dict(merged)
            _warn_about_missing_data(conn, args.year, overrides)
            players_svc.invalidate_cache()
            alt = slot_table(conn, args.year, cfg, other)
            render(f"WITH {args.compare}", alt, order)
            # Delta reads in the direction the reader just saw: what the
            # overrides would do to the league as it stands today.
            render_delta(current, alt, order)

    print("\nVOR is what the slot is worth: how far a startable player at that slot")
    print("sits above the best player you could have had for free.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
