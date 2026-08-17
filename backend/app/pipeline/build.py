"""Season ingest CLI.

    python -m app.pipeline.build --year 2019 --source synthetic
    python -m app.pipeline.build --years 2000-2019 --source synthetic
    python -m app.pipeline.build --coverage            # print the source matrix
    python -m app.pipeline.build --preflight           # can we reach the real sources?

Writes a season's players, games, daily box-score lines and IL stints into
SQLite, then records an eligibility verdict: a season that cannot support the
22-week fantasy calendar, or whose IL data has gaps, is marked ineligible and
drops out of the random season draw rather than being replayed with holes in
it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from .. import db
from ..config import LeagueConfig
from ..season_calendar import season_fits
from . import coverage, prosportstransactions, retrosheet, synthetic
from .synthetic import SeasonData

BATTING_COLS = ["game_id", "player_id", "season", "date", "team", "pa", "ab", "r", "h",
                "b1", "b2", "b3", "hr", "rbi", "bb", "ibb", "hbp", "so", "sb", "cs",
                "slam", "pos"]
PITCHING_COLS = ["game_id", "player_id", "season", "date", "team", "gs", "outs", "bf",
                 "h", "r", "er", "bb", "ibb", "hbp", "so", "hr", "w", "l", "sv", "cg"]


def store(conn: sqlite3.Connection, data: SeasonData, eligibility: dict[str, Any],
          cov: dict[str, Any]) -> None:
    with db.transaction(conn):
        conn.execute("DELETE FROM seasons WHERE year = ?", (data.year,))
        for table in ("batting_lines", "pitching_lines", "il_stints"):
            conn.execute(f"DELETE FROM {table} WHERE season = ?", (data.year,))
        conn.execute("DELETE FROM games WHERE season = ?", (data.year,))
        conn.execute("DELETE FROM players WHERE season = ?", (data.year,))

        conn.execute(
            """INSERT INTO seasons (year, source, opening_day, final_game_day,
                   all_star_monday, player_count, game_count, coverage_json,
                   eligible, ineligible_reason, ingested_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (data.year, data.source, data.opening_day.isoformat(),
             data.final_game_day.isoformat(), data.all_star_monday.isoformat(),
             len(data.players), len(data.games), json.dumps(cov),
             1 if eligibility["eligible"] else 0, eligibility.get("reason"),
             dt.datetime.utcnow().isoformat(timespec="seconds")),
        )
        conn.executemany(
            """INSERT INTO players (player_id, season, name, mlb_team, positions,
                                    is_pitcher, bats, throws)
               VALUES (:player_id,:season,:name,:mlb_team,:positions,:is_pitcher,:bats,:throws)""",
            data.players,
        )
        conn.executemany(
            """INSERT INTO games (game_id, season, date, home, away, home_runs, away_runs)
               VALUES (:game_id,:season,:date,:home,:away,:home_runs,:away_runs)""",
            [{**g, "home_runs": g.get("home_runs", 0), "away_runs": g.get("away_runs", 0)}
             for g in data.games],
        )
        conn.executemany(
            f"INSERT OR REPLACE INTO batting_lines ({','.join(BATTING_COLS)}) "
            f"VALUES ({','.join(':' + c for c in BATTING_COLS)})",
            [{c: row.get(c, 0 if c not in ("pos",) else None) for c in BATTING_COLS}
             for row in data.batting],
        )
        conn.executemany(
            f"INSERT OR REPLACE INTO pitching_lines ({','.join(PITCHING_COLS)}) "
            f"VALUES ({','.join(':' + c for c in PITCHING_COLS)})",
            [{c: row.get(c, 0) for c in PITCHING_COLS} for row in data.pitching],
        )
        conn.executemany(
            """INSERT INTO il_stints (season, player_id, start_date, end_date, kind, note)
               VALUES (:season,:player_id,:start_date,:end_date,:kind,:note)""",
            data.il_stints,
        )


def assess(
    data: SeasonData, cfg: LeagueConfig, il_report: dict[str, Any] | None,
    allow_missing_il: bool = False,
) -> dict[str, Any]:
    """Decide whether this season belongs in the random draw pool.

    ``allow_missing_il`` trades a feature for a season. The IL feed is a
    separate site from the box scores and can be unreachable while Retrosheet
    is fine; refusing the season then means refusing real baseball over a
    missing transaction log. With this set the season is playable and the
    absence is recorded as a warning instead — nobody goes on the IL all year,
    which the app has to say out loud rather than leave managers to notice.
    """
    reasons: list[str] = []
    warnings: list[str] = []
    fits, detail = season_fits(
        data.opening_day, data.final_game_day, cfg.total_weeks,
        cfg.regular_season_weeks, data.all_star_monday,
    )
    if not fits:
        reasons.append(f"calendar: {detail}")

    needed = cfg.max_teams * cfg.roster_size + 150
    if len(data.players) < needed:
        reasons.append(
            f"player pool: {len(data.players)} players, need {needed} for a "
            f"{cfg.max_teams}-team league plus a free-agent pool"
        )
    if il_report and not il_report.get("ok", True):
        target = warnings if allow_missing_il else reasons
        target.append(f"IL data: {il_report.get('reason')}")

    return {
        "eligible": not reasons,
        "reason": "; ".join(reasons) or None,
        "warnings": warnings,
        "no_il_data": bool(warnings) and not data.il_stints,
        "calendar": detail,
        "players": len(data.players),
        "games": len(data.games),
        "il_stints": len(data.il_stints),
    }


def build_season(
    year: int, source: str, cfg: LeagueConfig, seed: int | None = None,
    allow_missing_il: bool = False,
) -> tuple[SeasonData, dict, dict]:
    il_report: dict[str, Any] | None = None
    if source == "synthetic":
        data = synthetic.generate_season(year, seed=seed)
    elif source == "retrosheet":
        data = retrosheet.build(year)
        try:
            stints, il_report = prosportstransactions.build(year, data.players)
            data.il_stints = stints
        except prosportstransactions.SourceUnavailable as exc:
            il_report = {"ok": False, "reason": str(exc)}
    else:
        raise SystemExit(f"unknown source {source!r} (use synthetic or retrosheet)")

    if not data.il_stints and source == "synthetic":
        pass  # generator always supplies stints
    eligibility = assess(data, cfg, il_report, allow_missing_il=allow_missing_il)
    cov = coverage.report(data.source)
    if il_report:
        cov["il_report"] = il_report
    # Carried in coverage_json so the app can tell managers what this season
    # does not have, the same way it tells them which stats a source misses.
    cov["no_il_data"] = eligibility["no_il_data"]
    cov["warnings"] = eligibility["warnings"]
    return data, eligibility, cov


def parse_years(spec: str) -> list[int]:
    if "-" in spec:
        lo, hi = spec.split("-", 1)
        return list(range(int(lo), int(hi) + 1))
    return [int(p) for p in spec.split(",")]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build the local season cache")
    ap.add_argument("--year", type=int)
    ap.add_argument("--years", type=str, help="e.g. 2000-2019 or 2015,2017")
    ap.add_argument("--source", default="synthetic", choices=["synthetic", "retrosheet"])
    ap.add_argument("--db", type=str, default=None)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--coverage", action="store_true", help="print the source coverage matrix")
    ap.add_argument("--preflight", action="store_true", help="check the real sources' availability")
    ap.add_argument(
        "--allow-missing-il", action="store_true",
        help="keep a season playable when the IL feed is unreachable; nobody "
             "will go on the injured list and the app will say so",
    )
    args = ap.parse_args(argv)

    if args.coverage:
        for name in coverage.SOURCES:
            print(coverage.format_table(name), "\n")
        print("Stats absent from a standard box score:", ", ".join(coverage.NON_STANDARD_STATS))
        return 0

    if args.preflight:
        print("retrosheet          ", retrosheet.preflight())
        print("prosportstransactions", prosportstransactions.preflight())
        return 0

    years = parse_years(args.years) if args.years else ([args.year] if args.year else [])
    if not years:
        ap.error("pass --year or --years")

    cfg = LeagueConfig.load()
    db.init_db(args.db)
    with db.closing_conn(args.db) as conn:
        for year in years:
            print(f"[{year}] building from {args.source} ...", flush=True)
            data, eligibility, cov = build_season(
                year, args.source, cfg, seed=args.seed,
                allow_missing_il=args.allow_missing_il,
            )
            store(conn, data, eligibility, cov)
            flag = "eligible" if eligibility["eligible"] else f"EXCLUDED ({eligibility['reason']})"
            print(f"[{year}] {eligibility['players']} players, {eligibility['games']} games, "
                  f"{eligibility['il_stints']} IL stints — {flag}")
            for warning in eligibility["warnings"]:
                print(f"[{year}] playable, but: {warning}")
            if cov.get("needs_attention"):
                print(f"[{year}] stats needing attention: {', '.join(cov['needs_attention'])}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
