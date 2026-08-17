"""Check that an ingested season is real, complete, and scores.

The Retrosheet pipeline cannot be exercised where most of this app was written
— retrosheet.org is unreachable from there — so its parsing has only ever been
read, not run. This script is the assertion that it worked: point it at a
database that has just ingested a year and it fails loudly on the ways a
box-score parser goes wrong quietly.

    python -m scripts.verify_real_season --year 2019

The failure it exists to catch is not a crash. It is a season that parses into
a plausible-looking shell — half the games dropped, or a column the app scores
that came through as all zeros — which nobody notices until a league is three
weeks into replaying it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db  # noqa: E402
from app.scoring import ScoringConfig, score_batting, score_pitching  # noqa: E402

# A modern season is ~2430 games and comfortably over a thousand players who
# appeared. Well under that means the files parsed but most were discarded.
MIN_GAMES = 2000
MIN_PLAYERS = 1000

# Every column the scoring config can pay for. If real data leaves one of these
# empty for a whole season, that category silently scores zero all year.
BATTING_COLUMNS = ("r", "h", "b1", "b2", "b3", "hr", "rbi", "bb", "ibb", "hbp", "so", "sb")
PITCHING_COLUMNS = ("outs", "bf", "h", "er", "so", "w", "l", "sv", "cg", "gs")

# Grand slams are the one category a source may legitimately not supply, so it
# is reported rather than required.
OPTIONAL_BATTING = ("slam",)


def fail(message: str) -> None:
    print(f"FAIL: {message}")
    sys.exit(1)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--source", default="retrosheet")
    ap.add_argument("--db", default=None)
    # Overridable so the rest of the checks can be exercised against a
    # synthetic season, whose 32x30 rosters are smaller than a real league's.
    ap.add_argument("--min-games", type=int, default=MIN_GAMES)
    ap.add_argument("--min-players", type=int, default=MIN_PLAYERS)
    # Real data comes off one play-by-play feed and should reconcile to near
    # zero. The synthetic generator apportions pitching lines approximately and
    # drifts a few percent on home runs, so checking it needs a looser bar.
    ap.add_argument("--tolerance", type=float, default=0.02)
    # Eligibility folds in the IL feed, which is a different site with its own
    # availability. When it is down, the box scores are still worth checking —
    # this separates "the parsing is correct" from "the season is playable".
    ap.add_argument("--allow-ineligible", action="store_true")
    args = ap.parse_args(argv)
    year = args.year

    with db.closing_conn(args.db) as conn:
        season = conn.execute("SELECT * FROM seasons WHERE year=?", (year,)).fetchone()
        if season is None:
            fail(f"no {year} season row — the ingest wrote nothing")
        if season["source"] != args.source:
            fail(f"{year} came from {season['source']}, expected {args.source}")

        print(f"{year}: {season['player_count']} players, {season['game_count']} games, "
              f"{season['opening_day']} to {season['final_game_day']}")

        if not season["eligible"]:
            if not args.allow_ineligible:
                fail(f"{year} is marked ineligible: {season['ineligible_reason']}")
            print(f"WARNING: not playable as it stands — {season['ineligible_reason']}")
        if season["game_count"] < args.min_games:
            fail(f"only {season['game_count']} games — expected at least {args.min_games}")
        if season["player_count"] < args.min_players:
            fail(f"only {season['player_count']} players — expected at least {args.min_players}")

        _check_columns(conn, year, "batting_lines", BATTING_COLUMNS, OPTIONAL_BATTING)
        _check_columns(conn, year, "pitching_lines", PITCHING_COLUMNS, ())
        _check_hits_add_up(conn, year)
        _check_the_two_sides_agree(conn, year, args.tolerance)
        _check_it_scores(conn, year)
        _show_who_this_actually_is(conn, year)

    print(f"\n{year} ingested from {args.source}: real, complete, and scoring.")
    return 0


def _check_columns(
    conn, year: int, table: str, required: tuple[str, ...], optional: tuple[str, ...]
) -> None:
    columns = required + optional
    sums = conn.execute(
        f"SELECT {', '.join(f'SUM({c}) AS {c}' for c in columns)} "  # noqa: S608 - fixed names
        f"FROM {table} WHERE season=?",
        (year,),
    ).fetchone()
    print(f"\n{table}: " + "  ".join(f"{c}={sums[c] or 0}" for c in columns))
    empty = [c for c in required if not sums[c]]
    if empty:
        fail(f"{table}: nothing at all in {', '.join(empty)} for the whole season")
    for column in optional:
        if not sums[column]:
            print(f"  note: {column} is empty — this source cannot supply it")


def _check_hits_add_up(conn, year: int) -> None:
    """1B+2B+3B+HR must equal H, or the singles were derived wrong."""
    bad = conn.execute(
        "SELECT COUNT(*) n FROM batting_lines "
        "WHERE season=? AND b1 + b2 + b3 + hr != h",
        (year,),
    ).fetchone()["n"]
    if bad:
        fail(f"{bad} batting lines where 1B+2B+3B+HR does not equal H")
    print("\nhits reconcile: 1B+2B+3B+HR = H on every line")


def _check_the_two_sides_agree(conn, year: int, tolerance: float = 0.02) -> None:
    """Batter hits and pitcher hits-allowed describe the same events.

    Derived from the same play-by-play they should match almost exactly, so a
    real gap here means one side of the box score is being dropped — the
    failure that would otherwise show up as pitchers who never allow anything.
    A tolerance is allowed because the synthetic generator apportions pitching
    lines approximately, and it is useful to be able to run this on both.
    """
    for column, label in (("h", "hits"), ("hr", "home runs")):
        batting = conn.execute(
            f"SELECT SUM({column}) n FROM batting_lines WHERE season=?", (year,)  # noqa: S608
        ).fetchone()["n"] or 0
        pitching = conn.execute(
            f"SELECT SUM({column}) n FROM pitching_lines WHERE season=?", (year,)  # noqa: S608
        ).fetchone()["n"] or 0
        if not batting:
            fail(f"no batting {label} at all")
        drift = abs(batting - pitching) / batting
        print(f"{label}: {batting} batted, {pitching} allowed ({drift:.2%} apart)")
        if drift > tolerance:
            fail(f"{label} disagree by {drift:.1%} — one side of the box score is incomplete")


def _check_it_scores(conn, year: int) -> None:
    """The league's own scoring config has to produce points from these rows."""
    cfg = ScoringConfig.load()
    bat = [dict(r) for r in conn.execute(
        "SELECT * FROM batting_lines WHERE season=? ORDER BY h DESC, hr DESC LIMIT 200", (year,))]
    pit = [dict(r) for r in conn.execute(
        "SELECT * FROM pitching_lines WHERE season=? ORDER BY outs DESC LIMIT 200", (year,))]
    if not bat or not pit:
        fail("no lines to score")

    best_bat = max(score_batting(r, cfg).points for r in bat)
    best_pit = max(score_pitching(r, cfg).points for r in pit)
    print(f"best single game scored: {best_bat:.1f} batting, {best_pit:.1f} pitching")
    if best_bat <= 0 or best_pit <= 0:
        fail("real lines produced no points — the scorer is not reading these columns")


def _show_who_this_actually_is(conn, year: int, limit: int = 10) -> None:
    """Print the best batters by season points, named.

    Every check above could pass on well-formed nonsense. Names are the part a
    human can check at a glance: the leaderboard of a real season should be
    recognisable, and if it is not, something upstream is wrong in a way no
    column sum would reveal.
    """
    cfg = ScoringConfig.load()
    totals: dict[str, float] = {}
    names: dict[str, str] = {}
    for row in conn.execute(
        """SELECT b.player_id, p.name, b.* FROM batting_lines b
             JOIN players p ON p.player_id = b.player_id AND p.season = b.season
            WHERE b.season = ?""",
        (year,),
    ):
        line = dict(row)
        pid = line["player_id"]
        names[pid] = line["name"]
        totals[pid] = totals.get(pid, 0.0) + score_batting(line, cfg, include_derived=False).points

    best = sorted(totals.items(), key=lambda kv: -kv[1])[:limit]
    print(f"\ntop {limit} batters of {year} by this league's scoring:")
    for rank, (pid, points) in enumerate(best, 1):
        print(f"  {rank:2}. {names[pid]:<24} {points:7.1f}")
    if not best:
        fail("no batters to rank")

    # A Retrosheet ID standing in for a name means the roster files were not
    # read, and the league would draft troum001 instead of Mike Trout.
    unnamed = [pid for pid in totals if names[pid] == pid]
    if unnamed:
        share = len(unnamed) / len(totals)
        print(f"\n{len(unnamed)} of {len(totals)} batters have no name ({share:.1%})")
        if share > 0.01:
            fail(
                f"{share:.0%} of batters are still Retrosheet IDs (e.g. {unnamed[0]}) — "
                "the .ROS roster files were not read"
            )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
