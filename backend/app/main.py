"""Retro Season Replay — FastAPI application."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import db, scheduler
from .api.live import router as live_router
from .api.routes import router as api_router
from .ws import hub

logging.basicConfig(level=os.environ.get("RETRO_LOG_LEVEL", "INFO"))

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
ENABLE_SCHEDULER = os.environ.get("RETRO_SCHEDULER", "1") != "0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    if ENABLE_SCHEDULER:
        scheduler.start()
    yield
    scheduler.shutdown()
    await hub.shutdown()


app = FastAPI(
    title="Retro Season Replay",
    description="Draft and replay a historical MLB season as a fantasy league.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("RETRO_CORS_ORIGINS", "http://localhost:5173").split(","),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)
app.include_router(live_router)


@app.get("/api/health")
def health() -> dict[str, object]:
    """Liveness, plus enough to diagnose a deploy without reading the logs.

    A season cache that failed to build is invisible until someone tries to
    create a league and is told there are no eligible years. Reporting it here
    turns that into one glance.
    """
    seasons: dict[str, object] = {"cached": 0, "eligible": 0, "years": []}
    try:
        with db.closing_conn() as conn:
            rows = [dict(r) for r in conn.execute(
                "SELECT year, source, eligible FROM seasons ORDER BY year")]
        seasons = {
            "cached": len(rows),
            "eligible": sum(1 for r in rows if r["eligible"]),
            "years": [r["year"] for r in rows],
            "source": rows[0]["source"] if rows else None,
        }
    except Exception as exc:  # noqa: BLE001 - health must answer regardless
        seasons["error"] = f"{type(exc).__name__}: {exc}"

    return {
        "ok": True,
        "database": str(db.db_path()),
        "next_replay_run": scheduler.next_run(),
        "seasons": seasons,
        "playable": bool(seasons.get("eligible")),
    }


# Serve the built React app when it exists, so a single process runs the whole
# thing in production. In development the frontend runs on Vite's dev server.
if FRONTEND_DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str) -> FileResponse:
        candidate = FRONTEND_DIST / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")
