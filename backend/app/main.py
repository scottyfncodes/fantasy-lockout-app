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
    return {
        "ok": True,
        "database": str(db.db_path()),
        "next_replay_run": scheduler.next_run(),
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
