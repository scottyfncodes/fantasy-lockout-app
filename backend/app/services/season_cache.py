"""Caching a season the moment a league draws it.

Pre-caching every year meant a deployment could not serve anything for
twenty-five minutes, and most of that work was for seasons nobody drew. A
season is instead fetched when a league lands on it: the first league to draw
2011 waits about ninety seconds, and every league after that gets it at once,
because the cache is shared by everyone replaying that year.

The lock matters. Ingesting rewrites a season's rows wholesale, so two leagues
drawing the same uncached year at the same moment would race — one would see
the other's half-written season. Only one ingest per year ever runs; the second
caller simply waits for the first.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from typing import Any

from .. import db
from ..config import LeagueConfig

log = logging.getLogger(__name__)

_lock = threading.Lock()
_per_year: dict[int, threading.Lock] = {}
_state: dict[int, dict[str, Any]] = {}


def _year_lock(year: int) -> threading.Lock:
    with _lock:
        return _per_year.setdefault(year, threading.Lock())


def is_cached(conn: sqlite3.Connection, year: int) -> bool:
    row = conn.execute("SELECT eligible FROM seasons WHERE year = ?", (year,)).fetchone()
    return row is not None


def status(conn: sqlite3.Connection, year: int | None) -> dict[str, Any]:
    """What a league waiting on its season should be told."""
    if year is None:
        return {"year": None, "ready": False, "state": "undrawn"}
    if is_cached(conn, year):
        return {"year": year, "ready": True, "state": "ready"}
    live = _state.get(year, {})
    return {
        "year": year,
        "ready": False,
        "state": live.get("state", "pending"),
        "error": live.get("error"),
    }


def ensure(year: int, *, blocking: bool = False) -> None:
    """Cache ``year`` if it is not already, in the background by default."""
    with db.closing_conn() as conn:
        if is_cached(conn, year):
            return
    if _state.get(year, {}).get("state") == "loading":
        return
    if blocking:
        _ingest(year)
    else:
        threading.Thread(target=_ingest, args=(year,), daemon=True,
                         name=f"season-{year}").start()


def _ingest(year: int) -> None:
    with _year_lock(year):
        with db.closing_conn() as conn:
            if is_cached(conn, year):  # another caller got there first
                return
        _state[year] = {"state": "loading"}
        source = os.environ.get("RETRO_SOURCE", "synthetic")
        il_file = os.environ.get("RETRO_IL_FILE") or None
        lenient = os.environ.get("RETRO_ALLOW_MISSING_IL", "1") != "0"
        try:
            # Imported here: the pipeline pulls in the whole ingest stack, and
            # nothing else in a running server needs it.
            from ..pipeline import build as build_mod

            cfg = LeagueConfig.load()
            data, eligibility, cov = build_mod.build_season(
                year, source, cfg, allow_missing_il=lenient,
                il_file=il_file if il_file and os.path.isfile(il_file) else None,
            )
            with db.closing_conn() as conn:
                build_mod.store(conn, data, eligibility, cov)
            _state[year] = {
                "state": "ready" if eligibility["eligible"] else "unusable",
                "error": eligibility.get("reason"),
            }
            log.info("cached season %s (%s)", year, _state[year]["state"])
        except Exception as exc:  # noqa: BLE001 - a league is waiting on this
            log.exception("caching season %s failed", year)
            _state[year] = {"state": "failed", "error": f"{type(exc).__name__}: {exc}"}
