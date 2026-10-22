"""SessionManager writes release their SQLite transaction under cancellation."""

from __future__ import annotations

import asyncio
import gc
import sqlite3
import traceback
from contextlib import closing
from typing import TYPE_CHECKING
from typing import cast

import aiosqlite
import pytest

from nagents.session.manager import SessionManager
from nagents.types import Message
from tests.support.hang_guard import HANG_GUARD

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable
    from pathlib import Path


def _competing_write(database: Path) -> None:
    with closing(sqlite3.connect(database, timeout=0)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("UPDATE v2_sessions SET user_id = user_id WHERE id = 'target'")
        connection.commit()


@pytest.mark.parametrize("repeat_cancel", [False, True])
def test_cancelled_add_message_leaves_database_consistent_and_writable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repeat_cancel: bool
) -> None:
    async def scenario() -> None:
        database = tmp_path / "sessions.db"
        manager = SessionManager(database)
        await manager.initialize()
        await manager.get_or_create_session("target", "user")
        initial_count = await manager.get_message_count("target")
        initial_session = await manager.get_session("target")
        assert initial_session is not None
        marker = "cancelled-session-write"
        reached, release, interrupted = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original_execute = cast("Callable[..., Awaitable[object]]", aiosqlite.Connection._execute)

        async def gated_execute(
            connection: aiosqlite.Connection,
            operation: Callable[..., object],
            *args: object,
            **kwargs: object,
        ) -> object:
            result = await original_execute(connection, operation, *args, **kwargs)
            sql = args[0] if args else None
            if isinstance(sql, str) and sql.lstrip().startswith("INSERT INTO v2_messages"):
                # The INSERT has executed and its cursor exists, but the caller
                # has not resumed to perform the session update or commit.
                reached.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    interrupted.set()
                    raise
            return result

        monkeypatch.setattr(aiosqlite.Connection, "_execute", gated_execute)

        task = asyncio.create_task(manager.add_message("target", Message(role="user", content=marker)))
        cancellations: list[asyncio.CancelledError] = []
        positive_control_error: sqlite3.OperationalError | None = None
        try:
            await asyncio.wait_for(reached.wait(), HANG_GUARD)
            try:
                await asyncio.to_thread(_competing_write, database)
            except sqlite3.OperationalError as error:
                positive_control_error = error
            assert positive_control_error is not None
            assert positive_control_error.sqlite_errorcode is not None
            assert positive_control_error.sqlite_errorcode & 0xFF == sqlite3.SQLITE_BUSY
            traceback.clear_frames(positive_control_error.__traceback__)

            task.cancel()
            await asyncio.sleep(0)
            if repeat_cancel:
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
            release.set()
            done, pending = await asyncio.wait({task}, timeout=HANG_GUARD)
            assert not pending and done == {task}
            with pytest.raises(asyncio.CancelledError) as cancelled:
                task.result()
            cancellations.append(cancelled.value)
            assert task.cancelled()
            assert cancellations[0].__traceback__ is not None

            # The caller and cancellation traceback remain alive, with GC off.
            # Cancellation may roll back the message or finish it atomically,
            # but the shared database must be unlocked and have no partial row.
            await asyncio.to_thread(_competing_write, database)
            history = await manager.get_history("target")
            messages = [message for message in history if message.content == marker]
            assert len(messages) <= 1
            final_count = await manager.get_message_count("target")
            assert final_count == initial_count + len(messages)
            final_session = await manager.get_session("target")
            assert final_session is not None
            assert final_session["user_id"] == initial_session["user_id"]
            if messages:
                assert final_session["updated_at"] >= initial_session["updated_at"]
            else:
                assert final_session["updated_at"] == initial_session["updated_at"]
            if interrupted.is_set():
                # The current direct method is interrupted at the gate, so an
                # interrupted write must roll back its row and session metadata.
                assert not messages
                assert final_count == initial_count
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.wait({task}, timeout=HANG_GUARD)
            for cancelled_error in cancellations:
                traceback.clear_frames(cancelled_error.__traceback__)
            cancellations.clear()
            if positive_control_error is not None:
                traceback.clear_frames(positive_control_error.__traceback__)

    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        asyncio.run(scenario())
    finally:
        if gc_was_enabled:
            gc.enable()
