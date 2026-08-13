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
from .services import leagues as leagues_svc, replay as replay_svc

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


def start() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    scheduler = AsyncIOScheduler(timezone=SIM_TZ)
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
