"""Harness-owned SQLite reads release unread cursors before cancellation escapes."""

from __future__ import annotations

import asyncio
import gc
import sqlite3
import traceback
from contextlib import asynccontextmanager
from contextlib import closing
from typing import TYPE_CHECKING
from typing import TypeVar
from typing import cast

import aiosqlite
import pytest

from nagents.harness.agents.subagents import TaskInfo
from nagents.harness.config import HarnessConfig
from nagents.harness.runtime import Harness
from nagents.harness.task_activity import ChildActivity
from nagents.harness.task_activity import ChildHistoryCommitted
from nagents.types import Message
from tests.support.hang_guard import HANG_GUARD
from tests.support.web import no_guarded_workspace_io as no_guarded_workspace_io

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from collections.abc import Awaitable
    from collections.abc import Callable
    from pathlib import Path

T = TypeVar("T")
READS = {
    "watermark": "SELECT COALESCE(MAX(id), 0)",
    "context_identity": "SELECT MIN(m.id)",
    "session_exists": "SELECT 1 FROM v2_sessions",
}


@asynccontextmanager
async def child_activity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[ChildActivity]:
    parent = Harness(HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "state", demo=True))
    child = parent.tasks._create_child("assistant")
    # Keep real demo initialization and SQLite ownership. Only workspace file
    # discovery is irrelevant to these portable database lifecycle tests.
    monkeypatch.setattr(child, "load_project_instructions", lambda: None)
    monkeypatch.setattr(child.tools, "discover_skills", lambda: None)
    try:
        await child.initialize()
        child._queue = asyncio.Queue()
        info = TaskInfo(
            session_id=child._agent_session_id,
            title="reader",
            root_session_id=parent.session_id,
            _storage_id=child.session_id,
        )
        yield ChildActivity(parent.tasks, info, child)
    finally:
        await child.close()
        await parent.close()


def commit_history(database: Path, session_id: str) -> None:
    with closing(sqlite3.connect(database, timeout=0)) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("UPDATE v2_messages SET content = 'writer committed' WHERE session_id = ?", (session_id,))
        db.commit()


@pytest.mark.parametrize("reader", READS)
@pytest.mark.parametrize("repeated", [False, True])
def test_cancelled_child_read_releases_unread_cursor_before_writer_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reader: str, repeated: bool
) -> None:
    async def scenario() -> None:
        async with child_activity(tmp_path, monkeypatch) as activity:
            child = activity.child
            session = child.agent.session
            await session.add_message(child.session_id, Message(role="user", content="first"))
            await session.add_message(child.session_id, Message(role="assistant", content="second"))
            reached, release, interrupted = asyncio.Event(), asyncio.Event(), asyncio.Event()
            execute = cast("Callable[..., Awaitable[object]]", aiosqlite.Connection._execute)

            async def gated_execute(
                connection: aiosqlite.Connection, operation: Callable[..., T], *args: object, **kwargs: object
            ) -> T:
                result = cast("T", await execute(connection, operation, *args, **kwargs))
                if args and isinstance(args[0], str) and args[0].startswith(READS[reader]):
                    # This is a real unread SQLite cursor, before aiosqlite hands
                    # it to the caller. Retaining cancellation must not pin it.
                    reached.set()
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        interrupted.set()
                        raise
                return result

            monkeypatch.setattr(aiosqlite.Connection, "_execute", gated_execute)

            async def read() -> None:
                if reader == "watermark":
                    await activity._history_committed()
                elif reader == "context_identity":
                    await session.context_identity(child.session_id)
                else:
                    await session.session_exists(child.session_id)

            task = asyncio.create_task(read())
            cancellations: list[asyncio.CancelledError] = []
            try:
                await asyncio.wait_for(reached.wait(), HANG_GUARD)
                # Establish that the gate really holds a reader lock: SQLite
                # accepts the write but cannot commit until the cursor closes.
                with pytest.raises(sqlite3.OperationalError) as busy:
                    await asyncio.to_thread(commit_history, session.db_path, child.session_id)
                assert busy.value.sqlite_errorcode & 0xFF == sqlite3.SQLITE_BUSY
                task.cancel()
                await asyncio.sleep(0)
                if repeated:
                    task.cancel()
                    await asyncio.sleep(0)
            finally:
                release.set()
                await asyncio.wait_for(asyncio.wait((task,)), HANG_GUARD)
                try:
                    task.result()
                except asyncio.CancelledError as error:
                    cancellations.append(error)

            try:
                # Keep both the task and its actual cancellation traceback alive
                # with cyclic GC disabled until after the writer commits.
                assert task.cancelled() and len(cancellations) == 1
                assert cancellations[0].__traceback__ is not None
                await asyncio.to_thread(commit_history, session.db_path, child.session_id)
                assert not interrupted.is_set()
                assert child._queue is not None and child._queue.empty()
            finally:
                # A negative control must not leave the leaked reader alive in
                # pytest's retained failure frames and obscure later teardown.
                for cancellation in cancellations:
                    traceback.clear_frames(cancellation.__traceback__)
                cancellations.clear()

    enabled = gc.isenabled()
    gc.disable()
    try:
        asyncio.run(scenario())
    finally:
        if enabled:
            gc.enable()


def test_child_reads_keep_results_and_emit_only_the_child_watermark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async with child_activity(tmp_path, monkeypatch) as activity:
            child = activity.child
            session = child.agent.session
            first = await session.add_message(child.session_id, Message(role="user", content="first"))
            last = await session.add_message(child.session_id, Message(role="assistant", content="second"))
            await session.get_or_create_session("unrelated", "harness")
            await session.add_message("unrelated", Message(role="user", content="later unrelated history"))
            assert await session.context_identity(child.session_id) == first
            assert await session.session_exists(child.session_id)
            assert not await session.session_exists("missing")
            assert await session.context_identity("missing") == 0
            await activity._history_committed()
            assert child._queue is not None
            event = child._queue.get_nowait()
            assert isinstance(event, ChildHistoryCommitted) and event.history_id == last
            assert child._queue.empty()

    asyncio.run(scenario())
