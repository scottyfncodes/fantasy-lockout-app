"""Nightly replay job.

Every day at 8:00 PM Central the replay advances one day for every live league.
The schedule is fixed in v1 (not a commissioner setting); the commissioner's
``/advance`` endpoint exists for catching up a stalled league or demoing.

"8:00 PM CST" is implemented as 20:00 ``America/Chicago``, which follows the
local clock through daylight saving — that is what a league means by it.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from . import db
from .config import LeagueConfig
from .services import (
    bots as bots_svc,
    leagues as leagues_svc,
    lineups as lineups_svc,
    replay as replay_svc,
    waivers as waivers_svc,
)

log = logging.getLogger("retro.scheduler")

SIM_HOUR = int(os.environ.get("RETRO_SIM_HOUR", "20"))
SIM_MINUTE = int(os.environ.get("RETRO_SIM_MINUTE", "0"))
SIM_TZ = os.environ.get("RETRO_SIM_TZ", "America/Chicago")

_scheduler: AsyncIOScheduler | None = None


def run_nightly() -> list[dict[str, Any]]:
    """Advance every live league by one replay day."""
    results: list[dict[str, Any]] = []
    with db.closing_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM leagues WHERE phase IN ('season','playoffs')"
        ).fetchall()
        for row in rows:
            league = leagues_svc.require_league(conn, row["id"])
            cfg = leagues_svc.league_config(league)
            try:
                with db.transaction(conn):
                    outcome = replay_svc.advance_day(conn, league, cfg)
                results.append({"league": league["code"], **outcome})
                log.info("advanced %s -> %s", league["code"], outcome.get("date", outcome.get("status")))
            except Exception:  # noqa: BLE001 - one bad league must not stop the rest
                log.exception("nightly advance failed for league %s", league["code"])
                results.append({"league": league["code"], "status": "error"})
    return results


def _live_leagues(conn) -> list[dict[str, Any]]:
    return [
        leagues_svc.require_league(conn, r["id"])
        for r in conn.execute("SELECT id FROM leagues WHERE phase IN ('season','playoffs')")
    ]


def run_waivers() -> list[dict[str, Any]]:
    """Clear pending FAAB bids. Runs at midnight on the configured weekdays.

    Waivers are a real-world deadline rather than a replay one: managers bid
    across the week and the wire clears three times in it, so a player dropped
    on Tuesday is claimable by Wednesday rather than sitting until the next
    Monday.
    """
    results: list[dict[str, Any]] = []
    with db.closing_conn() as conn:
        for league in _live_leagues(conn):
            cfg = leagues_svc.league_config(league)
            week = league["current_week"] or 1
            if week > cfg.regular_season_weeks:
                continue  # rosters freeze for the playoffs
            try:
                with db.transaction(conn):
                    if cfg.bots_use_waivers:
                        bots_svc.submit_bot_bids(conn, league, cfg, week)
                    done = waivers_svc.process_week(conn, league, cfg, week)
                results.append({"league": league["code"], "claims": len(done)})
                if done:
                    log.info("waivers cleared %s claims in %s", len(done), league["code"])
            except Exception:  # noqa: BLE001 - one league must not stop the rest
                log.exception("waiver run failed for league %s", league["code"])
                results.append({"league": league["code"], "status": "error"})
    return results


def run_lineup_lock() -> list[dict[str, Any]]:
    """Lock the current week's lineups. Monday at noon, by default.

    Hours before that night's first replayed games, so a manager who has not
    set a lineup gets the auto-fill rather than an empty one, and nobody can
    react to a result they have already seen.
    """
    results: list[dict[str, Any]] = []
    with db.closing_conn() as conn:
        for league in _live_leagues(conn):
            cfg = leagues_svc.league_config(league)
            week = league["current_week"] or 1
            try:
                with db.transaction(conn):
                    bots_svc.set_all_bot_lineups(conn, league, cfg, week)
                    locked = lineups_svc.lock_week(conn, league, cfg, week)
                results.append({"league": league["code"], "locked": len(locked)})
            except Exception:  # noqa: BLE001
                log.exception("lineup lock failed for league %s", league["code"])
                results.append({"league": league["code"], "status": "error"})
    return results


def start() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    scheduler = AsyncIOScheduler(timezone=SIM_TZ)
    cfg = LeagueConfig.load()
    scheduler.add_job(
        run_waivers,
        CronTrigger(day_of_week=",".join(str(d) for d in cfg.waiver_run_weekdays),
                    hour=0, minute=0, timezone=SIM_TZ),
        id="waivers", replace_existing=True, misfire_grace_time=3600,
    )
    scheduler.add_job(
        run_lineup_lock,
        CronTrigger(day_of_week=str(cfg.lineup_lock_weekday),
                    hour=cfg.lineup_lock_hour, minute=0, timezone=SIM_TZ),
        id="lineup-lock", replace_existing=True, misfire_grace_time=3600,
    )
    scheduler.add_job(
        run_nightly,
        CronTrigger(hour=SIM_HOUR, minute=SIM_MINUTE, timezone=SIM_TZ),
        id="nightly_replay",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
    )
    scheduler.start()
    _scheduler = scheduler
    log.info("nightly replay scheduled for %02d:%02d %s", SIM_HOUR, SIM_MINUTE, SIM_TZ)
    return scheduler


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def next_run() -> str | None:
    if _scheduler is None:
        return None
    job = _scheduler.get_job("nightly_replay")
    return job.next_run_time.isoformat() if job and job.next_run_time else None
