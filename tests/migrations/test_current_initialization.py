"""Opening an already migrated database must not compete for its writer slot."""

from __future__ import annotations

import asyncio
import gc
import sqlite3
import traceback
from contextlib import closing
from typing import TYPE_CHECKING
from typing import TypeVar
from typing import cast

import aiosqlite
import pytest
from tests.support.hang_guard import HANG_GUARD

from nagents.migrations.base import Migration
from nagents.migrations.manager import MigrationManager
from nagents.session.manager import SessionManager

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable
    from pathlib import Path

T = TypeVar("T")


def immediate_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    """Expose actual contention immediately rather than waiting five seconds."""
    connect = aiosqlite.connect

    def immediate(database: Path) -> aiosqlite.Connection:
        return connect(database, timeout=0)

    monkeypatch.setattr(aiosqlite, "connect", immediate)


@pytest.mark.asyncio
async def test_current_session_initialization_does_not_require_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "current.db"
    await SessionManager(path).initialize()
    immediate_connections(monkeypatch)
    with closing(sqlite3.connect(path, timeout=0)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO v2_sessions(id, user_id) VALUES ('pending', 'harness')")
        try:
            # A normal uncommitted write permits committed reads, but no other
            # BEGIN IMMEDIATE. Child startup only needs the committed schema.
            session = SessionManager(path)
            await session.initialize()
            assert session._initialized
            assert not await session.session_exists("pending")
            writer.commit()
            assert await session.session_exists("pending")
        finally:
            writer.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize("pending_upgrade", [False, True])
async def test_current_schema_check_does_not_hide_required_lock_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pending_upgrade: bool
) -> None:
    path = tmp_path / "locked.db"
    initial = Migration(1, "initial", "CREATE TABLE original(value TEXT);")
    upgrade = Migration(2, "upgrade", "CREATE TABLE upgraded(value TEXT);")
    await MigrationManager(path, migrations=[initial], atomic=True).initialize()
    manager = MigrationManager(path, migrations=[initial, upgrade] if pending_upgrade else [initial], atomic=True)
    immediate_connections(monkeypatch)
    with closing(sqlite3.connect(path, timeout=0)) as writer:
        # A pending upgrade still needs a writer. An exclusive lock blocks even
        # a current-schema read. Neither condition may be mistaken for success.
        writer.execute("BEGIN IMMEDIATE" if pending_upgrade else "BEGIN EXCLUSIVE")
        try:
            with pytest.raises(sqlite3.OperationalError) as busy:
                await manager.initialize()
            assert busy.value.sqlite_errorcode & 0xFF == sqlite3.SQLITE_BUSY
            assert not manager._initialized
        finally:
            writer.rollback()
    await manager.initialize()
    assert manager._initialized
    assert await manager.get_version() == (2 if pending_upgrade else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("repeated", [False, True])
async def test_cancelled_current_schema_check_releases_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repeated: bool
) -> None:
    path = tmp_path / "cancelled-read.db"
    initial = Migration(1, "initial", "CREATE TABLE original(value TEXT);")
    await MigrationManager(path, migrations=[initial], atomic=True).initialize()
    manager = MigrationManager(path, migrations=[initial], atomic=True)
    reached, release, interrupted = asyncio.Event(), asyncio.Event(), asyncio.Event()
    execute = cast("Callable[..., Awaitable[object]]", aiosqlite.Connection._execute)

    async def gated_execute(
        connection: aiosqlite.Connection, operation: Callable[..., T], *args: object, **kwargs: object
    ) -> T:
        result = cast("T", await execute(connection, operation, *args, **kwargs))
        if args and args[0] == "SELECT MAX(version) FROM schema_migrations":
            # Hold the real unread SQLite cursor before its caller receives it.
            reached.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                interrupted.set()
                raise
        return result

    def commit_probe() -> None:
        with closing(sqlite3.connect(path, timeout=0)) as writer:
            writer.execute("INSERT INTO original VALUES ('committed')")
            writer.commit()

    monkeypatch.setattr(aiosqlite.Connection, "_execute", gated_execute)
    task = asyncio.create_task(manager.initialize())
    cancellations: list[asyncio.CancelledError] = []
    enabled = gc.isenabled()
    gc.disable()
    try:
        try:
            await asyncio.wait_for(reached.wait(), HANG_GUARD)
            with pytest.raises(sqlite3.OperationalError) as busy:
                commit_probe()
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
        # Keep the cancelled task and its real traceback alive: cleanup cannot
        # rely on garbage collection to release an unread cursor's lock.
        assert task.cancelled() and len(cancellations) == 1
        assert cancellations[0].__traceback__ is not None
        assert not interrupted.is_set() and not manager._initialized
        commit_probe()
    finally:
        for cancellation in cancellations:
            traceback.clear_frames(cancellation.__traceback__)
        cancellations.clear()
        if enabled:
            gc.enable()
