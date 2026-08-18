"""IL stints from a published transaction export.

ProSportsTransactions is the natural source, and it refuses automated traffic
from most hosts — including every one this app has been deployed to. A CSV
export of the same records sidesteps that without trying to defeat it.

The exports in circulation share one shortcoming: they carry only *placements*.
There is no activation row, so a stint has a start and no end, and the nominal
"15 day DL" in the notes is a floor, not a fact — players routinely stay out
far longer. Believing it would activate genuinely injured stars weeks early,
which does not merely add noise, it rewards rostering the injured.

So the end date comes from the box scores instead: a player is back on the day
he next appears in a game. That is exact rather than nominal, and it stays
causal — at any lineup lock it asks only whether he has played yet, never how
long he will end up being out.

    Date,Team,Acquired,Relinquished,Notes,Injury,DL_length,Injury_Type
    2016-04-05, Cardinals, , • Matt Holliday, placed on 15 day DL,1,15,back
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Iterable

from .prosportstransactions import _clean_name, attach_player_ids, coverage_check


class InjuryFileUnusable(RuntimeError):
    """The file is missing, unreadable, or has nothing for this season."""


def read_rows(path: str | Path) -> list[dict[str, str]]:
    target = Path(path)
    if not target.is_file():
        raise InjuryFileUnusable(f"no injury file at {target}")
    with target.open(newline="", encoding="utf-8", errors="replace") as handle:
        rows = list(csv.DictReader(handle))
    if not rows or "Date" not in rows[0]:
        raise InjuryFileUnusable(f"{target} does not look like a transaction export")
    return rows


def covered_years(rows: Iterable[dict[str, str]]) -> set[int]:
    years: set[int] = set()
    for row in rows:
        date = (row.get("Date") or "").strip()
        if len(date) >= 4 and date[:4].isdigit():
            years.add(int(date[:4]))
    return years


def appearance_dates(
    batting: Iterable[dict[str, Any]], pitching: Iterable[dict[str, Any]]
) -> dict[str, list[str]]:
    """Every date each player actually played, oldest first."""
    seen: dict[str, set[str]] = {}
    for line in list(batting) + list(pitching):
        seen.setdefault(line["player_id"], set()).add(line["date"])
    return {pid: sorted(dates) for pid, dates in seen.items()}


def parse_placements(rows: Iterable[dict[str, str]], year: int) -> list[dict[str, Any]]:
    """One open stint per placement row for this season."""
    out: list[dict[str, Any]] = []
    for row in rows:
        date = (row.get("Date") or "").strip()
        if not date.startswith(str(year)):
            continue
        name = _clean_name(row.get("Relinquished") or "")
        if not name:
            continue
        notes = (row.get("Notes") or "").strip()
        # The export carries the nominal length as its own column, which beats
        # re-parsing the prose — these notes write "15 day DL" without the
        # hyphen the site itself uses.
        length = (row.get("DL_length") or "").strip()
        out.append({
            "name": name,
            "season": year,
            "start_date": date,
            "end_date": None,
            "kind": f"{length}-day IL" if length.isdigit() and length != "0" else "IL",
            "note": (row.get("Injury_Type") or notes).strip() or None,
        })
    return out


def close_stints(
    stints: list[dict[str, Any]], appearances: dict[str, list[str]]
) -> list[dict[str, Any]]:
    """End each stint on the day the player next appears.

    No appearance after the start means he never came back that season, which
    ``end_date=None`` already represents.
    """
    for stint in stints:
        played = appearances.get(stint["player_id"], ())
        stint["end_date"] = next((d for d in played if d > stint["start_date"]), None)
    return stints


def build(
    year: int,
    players: list[dict[str, Any]],
    path: str | Path,
    batting: Iterable[dict[str, Any]],
    pitching: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = read_rows(path)
    if year not in covered_years(rows):
        raise InjuryFileUnusable(f"{Path(path).name} has no rows for {year}")

    placements = parse_placements(rows, year)
    matched, unmatched = attach_player_ids(placements, players)
    stints = close_stints(matched, appearance_dates(batting, pitching))

    report = coverage_check(stints, year)
    report["source"] = f"file:{Path(path).name}"
    report["unmatched_names"] = len(unmatched)
    report["unmatched_sample"] = sorted(set(unmatched))[:20]
    report["ended_by_return"] = sum(1 for s in stints if s["end_date"])
    return stints, report
