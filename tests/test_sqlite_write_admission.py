"""Real SQLite writers queue before SQL, without changing lock timeouts."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING
from typing import cast

import aiosqlite
import pytest

from nagents import _sqlite
from nagents._sqlite import database_write
from nagents._sqlite import database_writes
from nagents._sqlite import write_database
from nagents.channels.store import InboxStore
from nagents.harness.runtime import _HarnessSession
from nagents.harness.session_identity import SessionIdentityStore
from nagents.session.manager import SessionManager
from nagents.types import Message
from tests.support.hang_guard import HANG_GUARD

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable
    from pathlib import Path


def _insert(path: Path, value: int) -> None:
    with closing(sqlite3.connect(path, timeout=0)) as db, db:
        db.execute("INSERT INTO writes VALUES (?)", (value,))


def _prepare(path: Path) -> None:
    with closing(sqlite3.connect(path, timeout=0)) as db, db:
        db.execute("CREATE TABLE writes (value INTEGER)")


@pytest.mark.asyncio
async def test_fifo_aliases_cancelled_waiters_and_independent_databases(tmp_path: Path) -> None:
    path, other = tmp_path / "main.db", tmp_path / "other.db"
    _prepare(path)
    _prepare(other)
    (tmp_path / "alias").mkdir()
    alias = tmp_path / "alias" / ".." / path.name
    order: list[int] = []

    async def write(value: int, target: Path = path) -> None:
        async def insert() -> None:
            _insert(target, value)
            order.append(value)

        await write_database(target, insert)

    requests: list[asyncio.Task[None]] = []
    try:
        async with database_write(path):
            with closing(sqlite3.connect(path, timeout=0)) as held:
                held.execute("BEGIN IMMEDIATE")
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    _insert(path, 0)
                requests = [
                    asyncio.create_task(write(1)),
                    asyncio.create_task(write(2, alias)),
                    asyncio.create_task(write(3)),
                ]
                await asyncio.sleep(0)
                assert all(not request.done() for request in requests)
                requests[1].cancel()
                with pytest.raises(asyncio.CancelledError):
                    await requests[1]
                await write(9, other)
                assert order == [9]
                held.rollback()
        await asyncio.gather(requests[0], requests[2])
        assert order == [9, 1, 3]
        assert not _sqlite._gates
    finally:
        await asyncio.gather(*requests, return_exceptions=True)


@pytest.mark.asyncio
async def test_accepted_cancellation_keeps_admission_until_commit_and_close(tmp_path: Path) -> None:
    path = tmp_path / "owned.db"
    _prepare(path)
    entered, release = asyncio.Event(), asyncio.Event()

    async def accepted() -> None:
        with closing(sqlite3.connect(path, timeout=0)) as db, db:
            db.execute("INSERT INTO writes VALUES (1)")
            entered.set()
            await release.wait()

    first = asyncio.create_task(write_database(path, accepted))
    await asyncio.wait_for(entered.wait(), HANG_GUARD)

    async def following() -> None:
        _insert(path, 2)

    second = asyncio.create_task(write_database(path, following))
    try:
        first.cancel()
        await asyncio.sleep(0)
        first.cancel()
        await asyncio.sleep(0)
        assert not first.done() and not second.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await first
        await second
    with closing(sqlite3.connect(path, timeout=0)) as db:
        assert db.execute("SELECT value FROM writes ORDER BY rowid").fetchall() == [(1,), (2,)]
    assert not _sqlite._gates


@pytest.mark.asyncio
async def test_session_and_inbox_writers_join_the_same_gate_before_sql(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "shared.db"
    manager = SessionManager(path)
    await manager.get_or_create_session("root", "test")
    await manager.get_or_create_session("child", "test")
    inbox = InboxStore(path, "root", 10)
    await inbox.initialize()
    attempted = asyncio.Event()
    original = cast("Callable[..., Awaitable[object]]", aiosqlite.Connection._execute)

    async def observe(
        connection: aiosqlite.Connection, operation: Callable[..., object], *args: object, **kwargs: object
    ) -> object:
        if args and isinstance(args[0], str) and args[0].lstrip().startswith("INSERT INTO v2_messages"):
            attempted.set()
        return await original(connection, operation, *args, **kwargs)

    monkeypatch.setattr(aiosqlite.Connection, "_execute", observe)
    requests: list[asyncio.Task[object]] = []
    callbacks: list[str] = []

    def inbox_write(db: sqlite3.Connection) -> None:
        callbacks.append("once")
        db.execute("UPDATE v2_sessions SET user_id = 'inbox' WHERE id = 'root'")

    try:
        async with database_write(path):
            with closing(sqlite3.connect(path, timeout=0)) as held:
                held.execute("BEGIN IMMEDIATE")
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    held_probe = sqlite3.connect(path, timeout=0)
                    try:
                        held_probe.execute("BEGIN IMMEDIATE")
                    finally:
                        held_probe.close()
                requests = [
                    asyncio.create_task(manager.add_message("child", Message(role="user", content="queued"))),
                    asyncio.create_task(inbox._transaction(inbox_write)),
                ]
                try:
                    async with asyncio.timeout(HANG_GUARD):
                        while (
                            not attempted.is_set()
                            and sum(gate.users for gates in _sqlite._gates.values() for gate in gates.values()) < 3
                        ):
                            await asyncio.sleep(0)
                    assert not attempted.is_set(), "SessionManager must wait for admission before issuing its INSERT"
                    assert callbacks == []
                    assert await manager.get_message_count("root") == 0, (
                        "ordinary reads stay independent of writer admission"
                    )
                finally:
                    held.rollback()
    finally:
        await asyncio.gather(*requests, return_exceptions=True)
    await asyncio.gather(*requests)
    assert callbacks == ["once"]
    assert [message.content for message in await manager.get_history("child")] == ["queued"]
    assert not _sqlite._gates


@pytest.mark.asyncio
async def test_same_task_nested_admission_fails_clearly_and_cleans_registry(tmp_path: Path) -> None:
    async with database_write(tmp_path / "nested.db"):
        with pytest.raises(RuntimeError, match="reentered"):
            async with database_write(tmp_path / "nested.db"):
                pytest.fail("nested ownership cannot acquire the same SQLite writer")
    assert not _sqlite._gates


@pytest.mark.asyncio
async def test_attached_databases_acquire_in_canonical_order_without_blocking_an_independent_writer(
    tmp_path: Path,
) -> None:
    first, second = tmp_path / "a.db", tmp_path / "b.db"
    _prepare(first)
    _prepare(second)

    async def attached() -> None:
        async with database_writes((second, first, first)):
            with closing(sqlite3.connect(first, timeout=0)) as db, db:
                db.execute("ATTACH DATABASE ? AS other", (str(second),))
                db.execute("BEGIN IMMEDIATE")
                db.execute("INSERT INTO writes VALUES (2)")
                db.execute("INSERT INTO other.writes VALUES (2)")

    async def independent() -> None:
        async with database_write(second):
            _insert(second, 9)

    async with database_write(first):
        pending = asyncio.create_task(attached())
        await asyncio.sleep(0)
        async with asyncio.timeout(HANG_GUARD):
            await independent()
        assert not pending.done()
    await pending
    with closing(sqlite3.connect(second, timeout=0)) as db:
        assert db.execute("SELECT value FROM writes ORDER BY rowid").fetchall() == [(9,), (2,)]
    assert not _sqlite._gates


@pytest.mark.asyncio
async def test_cancelled_attached_admission_releases_its_earlier_gate(tmp_path: Path) -> None:
    first, second = tmp_path / "a.db", tmp_path / "b.db"

    async def waiting() -> None:
        async with database_writes((second, first)):
            pytest.fail("a request cancelled before all database admissions cannot execute")

    async with database_write(second):
        pending = asyncio.create_task(waiting())
        async with asyncio.timeout(HANG_GUARD):
            while sum(gate.users for gates in _sqlite._gates.values() for gate in gates.values()) != 3:
                await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        async with asyncio.timeout(HANG_GUARD), database_write(first):
            pass
    assert not _sqlite._gates


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["session", "harness", "create", "inbox", "identity"])
async def test_cancelled_common_writer_never_executes_after_waiting_for_admission(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "cancelled.db"
    manager = SessionManager(path)
    await manager.get_or_create_session("root", "test")
    adapter = _HarnessSession(path)
    await adapter.initialize()
    inbox = InboxStore(path, "root", 10)
    await inbox.initialize()
    callbacks: list[str] = []

    def callback(db: sqlite3.Connection) -> None:
        callbacks.append("unexpected")
        db.execute("UPDATE v2_sessions SET user_id = 'unexpected' WHERE id = 'root'")

    async def request() -> None:
        if kind == "session":
            await manager.add_message("root", Message(role="user", content="cancelled"))
        elif kind == "harness":
            await adapter.add_message("root", Message(role="user", content="cancelled"))
        elif kind == "create":
            await adapter.get_or_create_session("cancelled", "test")
        elif kind == "inbox":
            await inbox._transaction(callback)
        else:
            await SessionIdentityStore(path).reserve("root", "", "Cancelled reservation")

    async with database_write(path):
        pending = asyncio.create_task(request())
        async with asyncio.timeout(HANG_GUARD):
            while sum(gate.users for gates in _sqlite._gates.values() for gate in gates.values()) != 2:
                await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    assert callbacks == []
    assert await manager.get_message_count("root") == 0
    assert not await manager.session_exists("cancelled")
    with closing(sqlite3.connect(path, timeout=0)) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name = 'nagents_agent_sessions'").fetchall() == []
    assert not _sqlite._gates
