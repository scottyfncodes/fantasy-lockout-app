"""Retrosheet -> daily box scores.

Retrosheet publishes event-level (play-by-play) files, not box scores.  The
standard way to turn those into per-player, per-game stat lines is the
Chadwick tool suite: ``cwdaily`` reads a season's event files and emits one CSV
row per player per game with every counting stat we need — including B_HR4
(grand slams) and B_IBB, which ordinary box scores do not carry.

Requirements
------------
* outbound HTTPS to ``retrosheet.org``
* the Chadwick binaries (``cwdaily``) on PATH — ``brew install chadwick`` or
  build from https://github.com/chadwickbureau/chadwick

Both requirements are checked up front and reported clearly, because in a
sandboxed or offline environment neither is guaranteed.  See ``synthetic.py``
for the offline fallback and ``coverage.py`` for the full stat-by-stat matrix.

Retrosheet's terms require the standard attribution notice, which
``ATTRIBUTION`` carries and the app's rules page displays.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Iterable

from .synthetic import SeasonData

EVENT_URL = "https://www.retrosheet.org/events/{year}eve.zip"

ATTRIBUTION = (
    "The information used here was obtained free of charge from and is "
    "copyrighted by Retrosheet. Interested parties may contact Retrosheet at "
    "https://www.retrosheet.org."
)


class SourceUnavailable(RuntimeError):
    """Raised when the network or the Chadwick tools aren't available."""


def preflight() -> dict[str, Any]:
    """Check everything this pipeline needs before doing any work."""
    have_cwdaily = shutil.which("cwdaily") is not None
    net_ok, net_detail = _check_network()
    return {
        "cwdaily": have_cwdaily,
        "network": net_ok,
        "network_detail": net_detail,
        "ready": have_cwdaily and net_ok,
        "hint": (
            "install Chadwick (cwdaily) and allow outbound HTTPS to "
            "retrosheet.org, or run the pipeline with --source synthetic"
        ),
    }


def _check_network(timeout: int = 10) -> tuple[bool, str]:
    try:
        req = urllib.request.Request("https://www.retrosheet.org/", method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 400, f"HTTP {resp.status}"
    except Exception as exc:  # noqa: BLE001 - any failure means "unavailable"
        return False, f"{type(exc).__name__}: {exc}"


def download_events(year: int, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    url = EVENT_URL.format(year=year)
    try:
        with urllib.request.urlopen(url, timeout=120) as resp:
            payload = resp.read()
    except Exception as exc:  # noqa: BLE001
        raise SourceUnavailable(f"could not download {url}: {exc}") from exc
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        zf.extractall(dest)
    return dest


def run_cwdaily(year: int, event_dir: Path) -> list[dict[str, str]]:
    """Run cwdaily over a season's event files and return its CSV rows."""
    if shutil.which("cwdaily") is None:
        raise SourceUnavailable("cwdaily not found on PATH (install the Chadwick tool suite)")
    files = sorted(p.name for p in event_dir.glob(f"{year}*.EV*"))
    if not files:
        raise SourceUnavailable(f"no {year} event files found in {event_dir}")
    proc = subprocess.run(
        ["cwdaily", "-q", "-y", str(year), *files],
        cwd=event_dir, capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise SourceUnavailable(f"cwdaily failed: {proc.stderr.strip()[:400]}")
    return list(csv.DictReader(io.StringIO(proc.stdout)))


def _int(row: dict[str, str], key: str) -> int:
    value = row.get(key)
    if value in (None, "", "NA"):
        return 0
    try:
        return int(float(value))
    except ValueError:
        return 0


def map_batting(row: dict[str, str]) -> dict[str, Any]:
    h, b2, b3, hr = (_int(row, k) for k in ("B_H", "B_2B", "B_3B", "B_HR"))
    return {
        "game_id": row["GAME_ID"],
        "player_id": row["PLAYER_ID"],
        "date": _game_date(row),
        "team": row.get("TEAM_ID", ""),
        "pa": _int(row, "B_PA"), "ab": _int(row, "B_AB"), "r": _int(row, "B_R"),
        "h": h, "b1": max(0, h - b2 - b3 - hr), "b2": b2, "b3": b3, "hr": hr,
        "rbi": _int(row, "B_RBI"), "bb": _int(row, "B_BB"), "ibb": _int(row, "B_IBB"),
        "hbp": _int(row, "B_HP"), "so": _int(row, "B_SO"),
        "sb": _int(row, "B_SB"), "cs": _int(row, "B_CS"),
        # cwdaily splits home runs by baserunners; HR4 is a grand slam.
        "slam": _int(row, "B_HR4"),
        "pos": row.get("F_P_G") and "P" or None,
    }


def map_pitching(row: dict[str, str]) -> dict[str, Any]:
    return {
        "game_id": row["GAME_ID"],
        "player_id": row["PLAYER_ID"],
        "date": _game_date(row),
        "team": row.get("TEAM_ID", ""),
        "gs": _int(row, "P_GS"), "outs": _int(row, "P_OUT"), "bf": _int(row, "P_TBF"),
        "h": _int(row, "P_H"), "r": _int(row, "P_R"), "er": _int(row, "P_ER"),
        "bb": _int(row, "P_BB"), "ibb": _int(row, "P_IBB"), "hbp": _int(row, "P_HP"),
        "so": _int(row, "P_SO"), "hr": _int(row, "P_HR"),
        "w": _int(row, "P_W"), "l": _int(row, "P_L"), "sv": _int(row, "P_SV"),
        "cg": _int(row, "P_CG"),
    }


def _game_date(row: dict[str, str]) -> str:
    """Retrosheet GAME_IDs embed the date: TTTYYYYMMDDN."""
    gid = row["GAME_ID"]
    return f"{gid[3:7]}-{gid[7:9]}-{gid[9:11]}"


def build(year: int, cache_dir: Path | None = None) -> SeasonData:
    """Fetch, parse and normalise a full season. Raises SourceUnavailable."""
    checks = preflight()
    if not checks["ready"]:
        raise SourceUnavailable(
            f"retrosheet pipeline not ready: cwdaily={checks['cwdaily']}, "
            f"network={checks['network']} ({checks['network_detail']}). {checks['hint']}"
        )
    work = Path(cache_dir or tempfile.mkdtemp(prefix=f"retro{year}"))
    download_events(year, work)
    rows = run_cwdaily(year, work)
    return assemble(year, rows)


def assemble(year: int, rows: Iterable[dict[str, str]]) -> SeasonData:
    """Turn cwdaily rows into the same shape the synthetic generator emits."""
    players: dict[str, dict[str, Any]] = {}
    games: dict[str, dict[str, Any]] = {}
    batting: list[dict[str, Any]] = []
    pitching: list[dict[str, Any]] = []
    positions: dict[str, set[str]] = {}

    for row in rows:
        pid = row["PLAYER_ID"]
        date = _game_date(row)
        gid = row["GAME_ID"]
        games.setdefault(gid, {
            "game_id": gid, "season": year, "date": date,
            "home": gid[:3], "away": row.get("OPP_ID", ""),
            "home_runs": 0, "away_runs": 0,
        })
        pitched = _int(row, "P_G") > 0
        batted = _int(row, "B_G") > 0
        positions.setdefault(pid, set())
        for field, pos in (("F_C_G", "C"), ("F_1B_G", "1B"), ("F_2B_G", "2B"),
                           ("F_3B_G", "3B"), ("F_SS_G", "SS"), ("F_LF_G", "OF"),
                           ("F_CF_G", "OF"), ("F_RF_G", "OF"), ("F_P_G", "P")):
            if _int(row, field) > 0:
                positions[pid].add(pos)
        if _int(row, "B_G_DH") > 0:
            positions[pid].add("DH")

        players.setdefault(pid, {
            "player_id": pid, "season": year, "name": row.get("PLAYER_NAME", pid),
            "mlb_team": row.get("TEAM_ID", ""), "positions": "",
            "is_pitcher": 0, "bats": None, "throws": None,
        })
        if pitched:
            players[pid]["is_pitcher"] = 1
            line = map_pitching(row)
            line["season"] = year
            pitching.append(line)
        if batted:
            line = map_batting(row)
            line["season"] = year
            batting.append(line)

    for pid, player in players.items():
        pos = positions.get(pid, set())
        if player["is_pitcher"]:
            # Starter vs reliever from how the player was actually used.
            starts = sum(1 for l in pitching if l["player_id"] == pid and l["gs"])
            apps = sum(1 for l in pitching if l["player_id"] == pid)
            player["positions"] = "SP" if starts >= max(1, apps * 0.5) else "RP"
        else:
            player["positions"] = ",".join(sorted(pos - {"P"})) or "DH"

    for game in games.values():
        game["home_runs"] = sum(l["r"] for l in batting
                                if l["game_id"] == game["game_id"] and l["team"] == game["home"])
        game["away_runs"] = sum(l["r"] for l in batting
                                if l["game_id"] == game["game_id"] and l["team"] != game["home"])

    days = sorted({dt.date.fromisoformat(g["date"]) for g in games.values()})
    from ..season_calendar import detect_all_star_week, monday_of  # local: avoids cycle

    return SeasonData(
        year=year,
        source="retrosheet",
        opening_day=days[0],
        final_game_day=days[-1],
        all_star_monday=monday_of(detect_all_star_week(days, year)),
        players=list(players.values()),
        games=list(games.values()),
        batting=batting,
        pitching=pitching,
        il_stints=[],  # filled separately from ProSportsTransactions
    )
