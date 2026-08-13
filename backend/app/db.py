"""SQLite access.

Historical season data is cached locally (the pipeline writes it once) so the
app never hits an external API during a request.  Live league state lives in
the same database.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
DEFAULT_DB_PATH = Path(
    os.environ.get("RETRO_REPLAY_DB", Path(__file__).resolve().parents[1] / "data" / "replay.sqlite3")
)


def db_path() -> Path:
    return Path(os.environ.get("RETRO_REPLAY_DB", DEFAULT_DB_PATH))


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    target = Path(path) if path else db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False because FastAPI resolves a request's sync
    # dependencies on the shared anyio threadpool, which does not guarantee the
    # same worker thread for each one — the connection opened by `get_conn` is
    # routinely handed to a dependency running on a different thread. It stays
    # safe because a connection is never shared between requests, and the
    # dependencies of a single request never run concurrently.
    conn = sqlite3.connect(target, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(path: str | Path | None = None) -> None:
    with closing_conn(path) as conn:
        conn.executescript(SCHEMA_PATH.read_text())


@contextmanager
def closing_conn(path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def query(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


def query_one(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
    return conn.execute(sql, params).fetchone()


def execute(conn: sqlite3.Connection, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
    return conn.execute(sql, params)


def executemany(conn: sqlite3.Connection, sql: str, rows: Iterable[Sequence[Any]]) -> None:
    conn.executemany(sql, rows)


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]
