"""An idle routing poll must not reserve SQLite's writer lock."""

import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from nagents.session import SessionManager
from nagents.web.http.routing import RoutingStore


def test_empty_inbox_poll_succeeds_while_another_writer_holds_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def check() -> None:
        path = tmp_path / "routing.sqlite"
        sessions = SessionManager(path)
        await sessions.get_or_create_session("root", "user")
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE harness_sessions(id TEXT PRIMARY KEY, title TEXT NOT NULL)")
            db.execute("INSERT INTO harness_sessions VALUES ('root', 'Main')")
        store = RoutingStore(path)
        await store.initialize()

        connect = sqlite3.connect

        def no_wait(database: str | Path, timeout: float = 5) -> sqlite3.Connection:
            # Make a regression to BEGIN IMMEDIATE fail deterministically,
            # without reducing SQLite's production timeout.
            return connect(database, timeout=0)

        with closing(connect(path, timeout=0)) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            monkeypatch.setattr(sqlite3, "connect", no_wait)
            assert await store.has_pending() is False
            assert blocker.execute("SELECT COUNT(*) FROM ngn_web_inbox").fetchone() == (0,)
            blocker.rollback()

    asyncio.run(check())
