"""Read-only imported evidence has no Harness or inbox session identity."""

from __future__ import annotations

from typing import TYPE_CHECKING

from nagents.protocol.models import Session

if TYPE_CHECKING:
    import sqlite3


def initialize_in(db: sqlite3.Connection) -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS ngn_engine_events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
        "session_id TEXT NOT NULL, run_id TEXT NOT NULL, timestamp_ms INTEGER NOT NULL, payload TEXT NOT NULL)"
    )
    db.execute("CREATE INDEX IF NOT EXISTS ngn_engine_events_session ON ngn_engine_events(session_id, sequence)")
    db.execute(
        "CREATE TABLE IF NOT EXISTS ngn_engine_archives ("
        "id TEXT PRIMARY KEY, source_id TEXT NOT NULL UNIQUE, title TEXT NOT NULL, updated_at_ms INTEGER NOT NULL)"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS ngn_engine_host_import ("
        "id INTEGER PRIMARY KEY CHECK(id = 1), snapshot TEXT NOT NULL, sessions INTEGER NOT NULL, "
        "events INTEGER NOT NULL, ignored_children INTEGER NOT NULL)"
    )


def _session(row: tuple[str, str, int]) -> Session:
    return Session(id=row[0], title=row[1], updated_at_ms=row[2], archived=True)


def get_in(db: sqlite3.Connection, session: str) -> Session | None:
    row = db.execute("SELECT id, title, updated_at_ms FROM ngn_engine_archives WHERE id = ?", (session,)).fetchone()
    return _session(row) if row is not None else None


def list_in(db: sqlite3.Connection) -> list[Session]:
    return [_session(row) for row in db.execute("SELECT id, title, updated_at_ms FROM ngn_engine_archives")]
