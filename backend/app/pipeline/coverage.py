"""Which scoring stats each data source can actually supply.

The league's scoring config asks for several stats that are *not* in a
traditional box score.  This module is the single place that records what each
source natively provides, what must be derived, and what simply is not
available — so the schema and the sourcing decision are made explicitly rather
than discovered halfway through a season.

Levels
------
``native``   the source has a column for it; we read it straight through.
``derived``  not a column, but computable without guessing from data the
             source does provide (e.g. a cycle from 1B/2B/3B/HR counts).
``partial``  obtainable only from event-level data with extra work, or only
             for some seasons.  Stored, but may be 0 for some games.
``missing``  the source cannot supply it at all.  The column stays 0 and the
             corresponding scoring category will never fire.
"""

from __future__ import annotations

from typing import Any

BATTING_STATS = ["R", "1B", "2B", "3B", "HR", "RBI", "SB", "BB", "IBB", "HBP", "K", "CYC", "SLAM"]
PITCHING_STATS = ["IP", "W", "CG", "SV", "ER", "K", "QS"]

_ALL_NATIVE_B = {s: ("native", "") for s in BATTING_STATS}
_ALL_NATIVE_P = {s: ("native", "") for s in PITCHING_STATS}

# ---------------------------------------------------------------------------
# source -> {batting|pitching} -> stat -> (level, note)
# ---------------------------------------------------------------------------

SOURCES: dict[str, dict[str, Any]] = {
    "retrosheet": {
        "label": "Retrosheet event files via Chadwick (cwdaily)",
        "years": "1901-present (event files complete from ~1915; field caveats pre-1950)",
        "notes": (
            "Best source for this project: event-level data means daily player box "
            "scores are exact, and the odd stats fall out of the play-by-play."
        ),
        "batting": {
            "R": ("native", "B_R"),
            "1B": ("derived", "B_H - B_2B - B_3B - B_HR"),
            "2B": ("native", "B_2B"),
            "3B": ("native", "B_3B"),
            "HR": ("native", "B_HR"),
            "RBI": ("native", "B_RBI"),
            "SB": ("native", "B_SB"),
            "BB": ("native", "B_BB (includes IBB)"),
            "IBB": ("native", "B_IBB"),
            "HBP": ("native", "B_HP"),
            "K": ("native", "B_SO"),
            "CYC": ("derived", "1B & 2B & 3B & HR all >= 1 in one game"),
            "SLAM": ("native", "B_HR4 — cwdaily breaks home runs out by men on base"),
        },
        "pitching": {
            "IP": ("derived", "P_OUT / 3"),
            "W": ("native", "P_W"),
            "CG": ("native", "P_CG"),
            "SV": ("native", "P_SV"),
            "ER": ("native", "P_ER"),
            "K": ("native", "P_SO"),
            "QS": ("derived", ">= 18 outs and <= 3 ER in a start"),
        },
    },
    "pybaseball": {
        "label": "pybaseball (Baseball Reference / FanGraphs / Statcast wrappers)",
        "years": "1871-present for BR game logs; Statcast 2008+",
        "notes": (
            "Convenient, but daily per-player box scores need one request per "
            "player-season or a game-by-game scrape of BR box scores; rate limits "
            "make a full season slow. Use as a cross-check, not the primary feed."
        ),
        "batting": {
            **_ALL_NATIVE_B,
            "1B": ("derived", "H - 2B - 3B - HR"),
            "IBB": ("native", "BR game logs carry IBB"),
            "CYC": ("derived", ""),
            "SLAM": ("partial", "Not in game logs. Derivable from Statcast (2008+) by filtering HR events with the bases loaded."),
        },
        "pitching": {
            **_ALL_NATIVE_P,
            "IP": ("native", "convert 5.2-style notation to outs"),
            "QS": ("derived", ""),
        },
    },
    "prosportstransactions": {
        "label": "ProSportsTransactions.com (injured list / transactions only)",
        "years": "~2000-present with reliable structure",
        "notes": (
            "Not a stat source. Supplies the IL stint dates the replay locks "
            "lineups against. Pre-2000 coverage is spotty, which is why the random "
            "season draw is restricted to 2000+."
        ),
        "batting": {},
        "pitching": {},
    },
    "synthetic": {
        "label": "Deterministic synthetic season generator (offline default)",
        "years": "any",
        "notes": (
            "Generates a full, self-consistent season from a seed so the app runs "
            "end to end without network access. Every scoring category fires, "
            "including the rare ones, which makes it the right fixture for tests."
        ),
        "batting": dict(_ALL_NATIVE_B),
        "pitching": dict(_ALL_NATIVE_P),
    },
}

# Stats no traditional box score carries. The spec asked for these to be flagged
# before the schema was locked; SLAM is why the schema stores an explicit `slam`
# column rather than inferring grand slams from HR + RBI.
NON_STANDARD_STATS = ["IBB", "SLAM", "CYC", "QS"]


def _iter_stats(spec: dict[str, Any]):
    for half in ("batting", "pitching"):
        for stat, (level, note) in spec.get(half, {}).items():
            yield half, stat, level, note


def report(source: str) -> dict[str, Any]:
    """Coverage report for one source, ready to store in ``seasons.coverage_json``."""
    if source not in SOURCES:
        raise KeyError(f"unknown source {source!r}")
    spec = SOURCES[source]
    buckets: dict[str, list[str]] = {"native": [], "derived": [], "partial": [], "missing": []}
    levels: dict[str, str] = {}
    notes: dict[str, str] = {}
    for half, stat, level, note in _iter_stats(spec):
        key = f"{half[:3]}.{stat}"
        levels[key] = level
        if note:
            notes[key] = note
        buckets[level].append(key)
    return {
        "source": source,
        "label": spec["label"],
        "years": spec["years"],
        "notes": spec["notes"],
        "levels": levels,
        "notes_by_stat": notes,
        "summary": {k: sorted(v) for k, v in buckets.items()},
        "unsupported": sorted(buckets["missing"]),
        "needs_attention": sorted(buckets["missing"] + buckets["partial"]),
    }


def format_table(source: str) -> str:
    """Human-readable coverage table for the CLI and the in-app rules page."""
    spec = SOURCES[source]
    lines = [spec["label"], f"  years: {spec['years']}", ""]
    if not spec["batting"] and not spec["pitching"]:
        lines.append("  (transaction source only — supplies no scoring stats)")
        return "\n".join(lines)
    lines.append(f"  {'SIDE':<5} {'STAT':<6} {'LEVEL':<9} NOTE")
    for half, order in (("batting", BATTING_STATS), ("pitching", PITCHING_STATS)):
        for stat in order:
            if stat not in spec[half]:
                continue
            level, note = spec[half][stat]
            lines.append(f"  {half[:3]:<5} {stat:<6} {level:<9} {note}")
    return "\n".join(lines)
