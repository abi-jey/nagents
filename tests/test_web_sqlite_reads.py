"""A pending SQLite writer must not block event-loop-owned reader cleanup."""

import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path
from threading import Event

import aiosqlite

from nagents.harness.config import HarnessConfig
from nagents.web.service import WebState
from tests.test_web import ControlledHarness
from tests.test_web_deletion import rows


def test_rows_allows_async_shared_reader_to_close_before_pending_writer_commits(tmp_path: Path) -> None:
    async def run() -> None:
        harness = ControlledHarness(HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "data", demo=True))
        state = WebState(harness)
        path = state.history.db_path
        path.parent.mkdir(parents=True, exist_ok=True)
        writer_ready, commit_requested = Event(), Event()

        def write() -> None:
            with closing(sqlite3.connect(path, timeout=5)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("UPDATE lock_fixture SET value = 2")
                writer_ready.set()
                assert commit_requested.wait(5)
                db.commit()

        def writer_is_pending() -> bool:
            # RESERVED allows new readers; PENDING does not. Observe the actual
            # SQLite lock rather than assume a COMMIT trace callback or sleep
            # means the writer has acquired its pending exclusive lock.
            try:
                with (
                    closing(sqlite3.connect(path, timeout=0)) as db,
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
                    # Closing the cursor preserves SHARED until this explicit
                    # transaction is rolled back by an event-loop coroutine.
                    commit_requested.set()
                    await asyncio.wait_for(pending(), 3)
                    assert not writer.done()
                    release_reader = asyncio.Event()

                    async def release() -> None:
                        await release_reader.wait()
                        await reader.rollback()

                    releasing = asyncio.create_task(release())
                    asyncio.get_running_loop().call_soon(release_reader.set)
                    try:
                        assert await rows(state, "SELECT value FROM lock_fixture") == [(2,)]
                    finally:
                        release_reader.set()
                        await releasing
                finally:
                    commit_requested.set()
                    await reader.rollback()
                    await writer
        finally:
            await harness.close()

    asyncio.run(run())
