"""A2A connection setup and teardown keep ownership through cancellation."""

from __future__ import annotations

import asyncio
import os
import sqlite3
from threading import Event
from threading import Thread
from threading import current_thread
from typing import TYPE_CHECKING

import aiosqlite
import pytest

from nagents.a2a.store import SQLiteTaskStore
from tests.a2a.test_a2a_store import settings
from tests.support.hang_guard import HANG_GUARD

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.skipif(os.name != "posix", reason="A2A SQLite permissions require POSIX")


@pytest.mark.parametrize("phase", ["connect", "close"])
@pytest.mark.parametrize("repeated", [False, True])
def test_cancelled_connection_finishes_setup_and_close_before_returning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str, repeated: bool
) -> None:
    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        reached = asyncio.Event()
        release, closed = Event(), Event()
        workers: list[Thread] = []
        connections: list[sqlite3.Connection] = []

        def pause() -> None:
            loop.call_soon_threadsafe(reached.set)
            if not release.wait(HANG_GUARD):
                raise TimeoutError("Connection lifecycle gate was not released")

        class TrackedConnection(sqlite3.Connection):
            def close(self) -> None:
                if phase == "close":
                    pause()
                super().close()
                closed.set()

        def gated_connect(database: Path, *, timeout: int) -> aiosqlite.Connection:
            def connector() -> sqlite3.Connection:
                workers.append(current_thread())
                # Cross-thread closure is only for negative-control cleanup if
                # an implementation abandons the worker's newly created handle.
                connection = sqlite3.connect(
                    database, timeout=timeout, factory=TrackedConnection, check_same_thread=False
                )
                connections.append(connection)
                if phase == "connect":
                    pause()
                return connection

            return aiosqlite.Connection(connector, iter_chunk_size=64)

        monkeypatch.setattr(aiosqlite, "connect", gated_connect)
        store = SQLiteTaskStore(settings(tmp_path))

        async def read() -> None:
            async with store.connection() as db, db.execute("SELECT 1") as cursor:
                assert await cursor.fetchone() is not None

        task = asyncio.create_task(read())
        pending_after_cancel = False
        closed_by_owner = False
        try:
            await asyncio.wait_for(reached.wait(), HANG_GUARD)
            task.cancel()
            await asyncio.sleep(0)
            if repeated:
                task.cancel()
                await asyncio.sleep(0)
            pending_after_cancel = not task.done()
        finally:
            release.set()
            await asyncio.wait_for(asyncio.wait((task,)), HANG_GUARD)
            for worker in workers:
                await asyncio.to_thread(worker.join, HANG_GUARD)
                assert not worker.is_alive()
            closed_by_owner = closed.is_set()
            for connection in connections:
                connection.close()

        with pytest.raises(asyncio.CancelledError):
            task.result()
        assert pending_after_cancel
        assert closed_by_owner

    asyncio.run(scenario())
