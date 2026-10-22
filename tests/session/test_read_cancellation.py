"""Direct SessionManager reads must release SQLite cursors under cancellation."""

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


_READS = {
    "get_or_create_session": "SELECT id FROM v2_sessions WHERE id = ?",
    "get_history": "SELECT * FROM v2_messages WHERE session_id = ? ORDER BY id",
    "list_sessions": "SELECT * FROM v2_sessions ORDER BY updated_at DESC",
    "context_identity": "SELECT MIN(m.id) FROM v2_messages",
    "session_exists": "SELECT 1 FROM v2_sessions WHERE id = ?",
    "get_session": "SELECT * FROM v2_sessions WHERE id = ?",
    "get_message_count": "SELECT COUNT(*) FROM v2_messages WHERE session_id = ?",
}


def _commit_update(database: Path) -> None:
    with closing(sqlite3.connect(database, timeout=0)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("UPDATE v2_messages SET content = 'writer update' WHERE session_id = 'target'")
        connection.commit()


@pytest.mark.parametrize("reader", tuple(_READS))
@pytest.mark.parametrize("repeat_cancel", [False, True])
def test_cancelled_direct_read_releases_cursor_before_writer_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reader: str, repeat_cancel: bool
) -> None:
    async def scenario() -> None:
        database = tmp_path / "sessions.db"
        manager = SessionManager(database)
        await manager.initialize()
        await manager.get_or_create_session("target", "user")
        await manager.get_or_create_session("other", "user")
        await manager.add_message("target", Message(role="user", content="first"))
        await manager.add_message("target", Message(role="assistant", content="second"))
        await manager.add_message("other", Message(role="user", content="another session"))

        reached, release, interrupted = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original_execute = cast("Callable[..., Awaitable[object]]", aiosqlite.Connection._execute)
        query_prefix = _READS[reader]

        async def gated_execute(
            connection: aiosqlite.Connection,
            operation: Callable[..., object],
            *args: object,
            **kwargs: object,
        ) -> object:
            result = await original_execute(connection, operation, *args, **kwargs)
            sql = args[0] if args else None
            if isinstance(sql, str) and sql.startswith(query_prefix):
                # The SELECT has run and returned its cursor, but the caller has
                # not yet fetched from or closed that cursor.
                reached.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    interrupted.set()
                    raise
            return result

        monkeypatch.setattr(aiosqlite.Connection, "_execute", gated_execute)

        async def read() -> object:
            if reader == "get_or_create_session":
                return await manager.get_or_create_session("target", "user")
            if reader == "get_history":
                return await manager.get_history("target")
            if reader == "list_sessions":
                return await manager.list_sessions()
            if reader == "context_identity":
                return await manager.context_identity("target")
            if reader == "session_exists":
                return await manager.session_exists("target")
            if reader == "get_session":
                return await manager.get_session("target")
            return await manager.get_message_count("target")

        task = asyncio.create_task(read())
        cancellations: list[asyncio.CancelledError] = []
        positive_control_error: sqlite3.OperationalError | None = None
        try:
            await asyncio.wait_for(reached.wait(), HANG_GUARD)
            # In DELETE-journal mode, this writer can acquire its RESERVED lock
            # but cannot commit while the unread SELECT cursor holds a reader.
            try:
                await asyncio.to_thread(_commit_update, database)
            except sqlite3.OperationalError as error:
                positive_control_error = error
            assert positive_control_error is not None
            assert positive_control_error.sqlite_errorcode is not None
            assert positive_control_error.sqlite_errorcode & 0xFF == sqlite3.SQLITE_BUSY
            traceback.clear_frames(positive_control_error.__traceback__)

            task.cancel()
            await asyncio.sleep(0)
            if repeat_cancel:
                # Deliver a second cancellation while the read context is
                # unwinding, the case that can interrupt cursor cleanup.
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
            release.set()

            done, pending = await asyncio.wait({task}, timeout=HANG_GUARD)
            assert not pending
            assert done == {task}
            with pytest.raises(asyncio.CancelledError) as cancelled:
                task.result()
            cancellations.append(cancelled.value)

            # Keep the cancelled task and traceback alive with cyclic GC off;
            # a committed write proves cleanup released any reader ownership.
            assert task.cancelled()
            assert cancellations[0].__traceback__ is not None
            await asyncio.to_thread(_commit_update, database)
        finally:
            release.set()
            if not task.done():
                task.cancel()
                done, _ = await asyncio.wait({task}, timeout=HANG_GUARD)
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
