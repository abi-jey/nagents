"""A pending SQLite writer must not block event-loop-owned reader cleanup."""

import asyncio
import sqlite3
from collections.abc import Awaitable
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from threading import Event
from threading import get_ident

import aiosqlite
import pytest

from nagents.harness.config import HarnessConfig
from nagents.web.service import WebState
from tests.test_web import ControlledHarness
from tests.test_web_deletion import rows


async def _exercise_pending_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    read_rows: Callable[[WebState, str], Awaitable[list[tuple[object, ...]]]],
) -> None:
    harness = ControlledHarness(HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "data", demo=True))
    state = WebState(harness)
    path = state.history.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    writer_ready, commit_requested = Event(), Event()
    query_ready = Event()
    read_entered = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop_thread = get_ident()
    connect: Callable[..., sqlite3.Connection] = sqlite3.connect
    query_threads: list[int] = []

    def write() -> None:
        with closing(connect(path, timeout=5)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE lock_fixture SET value = 2")
            writer_ready.set()
            assert commit_requested.wait(5)
            db.commit()

    def writer_is_pending() -> bool:
        # RESERVED allows new readers; PENDING does not. Observe the actual
        # SQLite lock rather than treating a trace callback or sleep as proof.
        try:
            with (
                closing(connect(path, timeout=0)) as db,
                closing(db.execute("SELECT value FROM lock_fixture")) as cursor,
            ):
                cursor.fetchall()
        except sqlite3.OperationalError as error:
            if error.sqlite_errorcode == sqlite3.SQLITE_BUSY:
                return True
            raise
        return False

    async def pending() -> None:
        while not await asyncio.to_thread(writer_is_pending):
            await asyncio.sleep(0)

    def observe_query(sql: str) -> None:
        if sql == "SELECT value FROM lock_fixture":
            query_threads.append(get_ident())

    def gated_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        assert get_ident() != loop_thread, "SQLite reads must run off the event-loop thread"
        loop.call_soon_threadsafe(read_entered.set)
        # Park the actual helper's background read until the loop releases the
        # SHARED reader and observes native writer COMMIT/close. The helper's
        # unchanged 0.2s busy timeout is not a scheduler/fsync speed requirement.
        assert query_ready.wait(5), "The event loop did not complete writer cleanup"
        db = connect(*args, **kwargs)
        db.set_trace_callback(observe_query)
        return db

    try:
        async with aiosqlite.connect(path) as reader:
            await reader.execute("CREATE TABLE lock_fixture(value INTEGER)")
            await reader.execute("INSERT INTO lock_fixture VALUES (1)")
            await reader.commit()
            writer = asyncio.create_task(asyncio.to_thread(write))
            try:
                assert await asyncio.to_thread(writer_ready.wait, 5)
                await reader.execute("BEGIN")
                async with reader.execute("SELECT value FROM lock_fixture") as cursor:
                    assert [row[0] for row in await cursor.fetchall()] == [1]
                commit_requested.set()
                await asyncio.wait_for(pending(), 3)
                assert not writer.done()
                # Existing reader/writer connections are real SQLite handles.
                # Only the helper's next connection is instrumented; it still
                # executes the real query and must return the committed value.
                with monkeypatch.context() as patch:
                    patch.setattr(sqlite3, "connect", gated_connect)
                    reading = asyncio.ensure_future(read_rows(state, "SELECT value FROM lock_fixture"))
                    entered = asyncio.create_task(read_entered.wait())
                    try:
                        done, _ = await asyncio.wait({reading, entered}, timeout=5, return_when=asyncio.FIRST_COMPLETED)
                        assert done, "The read helper did not reach its SQLite boundary"
                        if reading.done():
                            await reading  # Propagate the synchronous negative control's assertion.
                            pytest.fail("Read completed without waiting at the background-read barrier")
                        assert entered.done() and not writer.done()
                        await reader.rollback()  # Event-loop-driven cleanup while rows() is still pending.
                        await writer
                        assert not reading.done()
                        query_ready.set()
                        assert await reading == [(2,)]
                        assert query_threads and all(thread != loop_thread for thread in query_threads)
                    finally:
                        await reader.rollback()
                        try:
                            await writer
                        finally:
                            query_ready.set()
                            entered.cancel()
                            await asyncio.gather(entered, reading, return_exceptions=True)
            finally:
                commit_requested.set()
                await reader.rollback()
                await writer
    finally:
        await harness.close()


def test_rows_allows_async_shared_reader_to_close_before_pending_writer_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(_exercise_pending_writer(tmp_path, monkeypatch, rows))


def test_pending_writer_oracle_rejects_synchronous_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def synchronous_rows(state: WebState, sql: str) -> list[tuple[object, ...]]:
        # Negative control: the original failure mode, even inside an async API.
        with (
            closing(sqlite3.connect(state.history.db_path, timeout=0.2)) as db,
            closing(db.execute(sql)) as cursor,
        ):
            return cursor.fetchall()

    with pytest.raises(AssertionError, match="SQLite reads must run off the event-loop thread"):
        asyncio.run(_exercise_pending_writer(tmp_path, monkeypatch, synchronous_rows))
