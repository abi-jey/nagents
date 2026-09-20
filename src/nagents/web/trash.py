"""Durable root membership trash, frozen expiry, and owned idle cleanup."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from contextlib import closing
from contextlib import contextmanager
from contextlib import suppress
from dataclasses import asdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

import anyio
from fastapi import HTTPException

from ._async import finish_on_cancel
from ._async import join_owned as _join
from .deletion import _guard_process
from .deletion import _guard_rows
from .deletion import _quarantine_pending
from .deletion import _release_bindings
from .deletion import _remove_content
from .deletion import _repoint_mains
from .deletion import _selection
from .routing import RoutingStore

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable
    from collections.abc import Iterator

    from .service import WebState

logger = logging.getLogger(__name__)
RETENTION_DAYS = 30
PURGE_INTERVAL = 30.0
PURGE_BATCH = 20
_PURGE_START = (-float("inf"), "")


@dataclass(frozen=True)
class TrashItem:
    id: str
    title: str
    deleted_at: float
    purge_at: float
    deletion_id: str

    def wire(self) -> dict[str, object]:
        return asdict(self)


class SessionTrash:
    """Policy and tombstones share the workspace DB, including SQLite backups.

    The revision covers retention policy only. Deletion generations separately
    protect restore/purge requests against a later deletion of the same root.
    """

    def __init__(
        self, state: WebState, *, clock: Callable[[], float] = time.time, interval: float = PURGE_INTERVAL
    ) -> None:
        self.state = state
        self.store = state.channels.store
        self.clock = clock
        self.interval = interval
        self.closed = False
        self.task: asyncio.Task[None] | None = None
        self._purge_after = _PURGE_START

    @contextmanager
    def _idle(self, operation: str) -> Iterator[None]:
        with self.state.idle():
            if self.state.harness._busy:
                raise HTTPException(409, "Harness busy. Finish the active operation before changing Trash.")
            with self.state.harness.operation(operation):
                yield

    async def initialize(self) -> None:
        def initialize(db: sqlite3.Connection) -> None:
            db.execute(
                "CREATE TABLE IF NOT EXISTS ngn_web_trash_policy ("
                "id INTEGER PRIMARY KEY CHECK(id = 1), retention_days INTEGER NOT NULL CHECK(retention_days BETWEEN 1 AND 365), "
                "revision TEXT NOT NULL)"
            )
            db.execute(
                "INSERT OR IGNORE INTO ngn_web_trash_policy VALUES (1, ?, ?)",
                (RETENTION_DAYS, secrets.token_hex(16)),
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS ngn_web_session_trash ("
                "id TEXT PRIMARY KEY, title TEXT NOT NULL, deleted_at REAL NOT NULL, purge_at REAL NOT NULL, "
                "deletion_id TEXT NOT NULL UNIQUE)"
            )
            db.execute("CREATE INDEX IF NOT EXISTS ngn_web_session_trash_expiry ON ngn_web_session_trash(purge_at, id)")

        await self.store._transaction(initialize)

    @staticmethod
    def _items(db: sqlite3.Connection) -> list[TrashItem]:
        with closing(
            db.execute(
                "SELECT id, title, deleted_at, purge_at, deletion_id FROM ngn_web_session_trash ORDER BY deleted_at DESC, id"
            )
        ) as cursor:
            return [TrashItem(*row) for row in cursor.fetchall()]

    @classmethod
    def _snapshot(cls, db: sqlite3.Connection) -> dict[str, object]:
        with closing(db.execute("SELECT revision, retention_days FROM ngn_web_trash_policy WHERE id = 1")) as cursor:
            revision, days = cursor.fetchone()
        return {
            "revision": str(revision),
            "retention_days": int(days),
            "items": [item.wire() for item in cls._items(db)],
        }

    async def snapshot(self) -> dict[str, object]:
        return await self.store._transaction(self._snapshot)

    async def change(self, revision: str, retention_days: int) -> dict[str, object]:
        if type(retention_days) is not int or not 1 <= retention_days <= 365:
            raise HTTPException(422, "Retention must be an integer from 1 through 365 days.")

        def change(db: sqlite3.Connection) -> dict[str, object]:
            with closing(
                db.execute(
                    "UPDATE ngn_web_trash_policy SET retention_days = ?, revision = ? WHERE id = 1 AND revision = ?",
                    (retention_days, secrets.token_hex(16), revision),
                )
            ) as cursor:
                if cursor.rowcount != 1:
                    raise HTTPException(409, "Trash settings changed. Refresh before saving again.")
            return self._snapshot(db)

        with self._idle("change trash settings"), anyio.CancelScope(shield=True):
            result = await finish_on_cancel(self.store._transaction(change))
        return result

    @staticmethod
    def _find(db: sqlite3.Connection, session_id: str) -> TrashItem | None:
        with closing(
            db.execute(
                "SELECT id, title, deleted_at, purge_at, deletion_id FROM ngn_web_session_trash WHERE id = ?",
                (session_id,),
            )
        ) as cursor:
            row = cursor.fetchone()
        return TrashItem(*row) if row is not None else None

    @classmethod
    def _generation(cls, db: sqlite3.Connection, session_id: str, deletion_id: str) -> TrashItem:
        item = cls._find(db, session_id)
        if item is None:
            with closing(db.execute("SELECT 1 FROM harness_sessions WHERE id = ?", (session_id,))) as cursor:
                if cursor.fetchone() is not None:
                    raise HTTPException(409, "This trash generation is no longer current. Refresh Trash.")
            raise HTTPException(404, "Session not found in Trash.")
        if item.deletion_id != deletion_id:
            raise HTTPException(409, "This trash generation is no longer current. Refresh Trash.")
        # Fail closed for inconsistent/partially restored databases. Purging a
        # tombstone must never prune an active root, and restore must not replace it.
        with closing(db.execute("SELECT 1 FROM harness_sessions WHERE id = ?", (session_id,))) as cursor:
            if cursor.fetchone() is not None:
                raise HTTPException(409, "Session has active membership; trash operation was not performed.")
        return item

    def remove_rows(self, db: sqlite3.Connection, session_id: str, selected: str) -> tuple[str, TrashItem]:
        existing = self._find(db, session_id)
        if existing is not None:
            self._generation(db, session_id, existing.deletion_id)
            return _selection(db, selected), existing
        RoutingStore.root(db, session_id)
        _guard_rows(db, session_id)
        _release_bindings(db, session_id)
        _quarantine_pending(db, session_id)
        with closing(db.execute("SELECT title FROM harness_sessions WHERE id = ?", (session_id,))) as cursor:
            title = str(cursor.fetchone()[0])
        with closing(db.execute("SELECT retention_days FROM ngn_web_trash_policy WHERE id = 1")) as cursor:
            days = int(cursor.fetchone()[0])
        now = self.clock()
        item = TrashItem(session_id, title, now, now + days * 86400, secrets.token_hex(16))
        db.execute(
            "INSERT INTO ngn_web_session_trash VALUES (?, ?, ?, ?, ?)",
            (item.id, item.title, item.deleted_at, item.purge_at, item.deletion_id),
        )
        db.execute("DELETE FROM harness_sessions WHERE id = ?", (session_id,))
        return _selection(db, selected), item

    def _restore_rows(self, db: sqlite3.Connection, session_id: str, deletion_id: str) -> None:
        item = self._generation(db, session_id, deletion_id)
        if item.purge_at <= self.clock():
            raise HTTPException(410, "The recovery period has expired. Refresh Trash.")
        with closing(db.execute("SELECT 1 FROM v2_sessions WHERE id = ?", (session_id,))) as cursor:
            if cursor.fetchone() is None:
                raise HTTPException(409, "Trashed session history is missing; restoration was not performed.")
        _guard_rows(db, session_id)
        db.execute("INSERT INTO harness_sessions (id, title) VALUES (?, ?)", (item.id, item.title))
        db.execute("DELETE FROM ngn_web_session_trash WHERE id = ? AND deletion_id = ?", (item.id, item.deletion_id))

    async def restore(self, session_id: str, deletion_id: str) -> dict[str, object]:
        state = self.state

        async def finish() -> dict[str, object]:
            _guard_process(state, session_id)
            await self.store._transaction(lambda db: self._restore_rows(db, session_id, deletion_id))
            # Invalidate joined membership readers before the first post-commit await.
            state.session_revision += 1
            sessions = [asdict(session) for session in await state.list_sessions()]
            state.bus.publish({"type": "sessions", "sessions": sessions})
            return {"restored_session_id": session_id, "sessions": sessions}

        with self._idle("restore session"), anyio.CancelScope(shield=True):
            result = await finish_on_cancel(finish())
        return result

    def _purge_rows(self, db: sqlite3.Connection, session_id: str, deletion_id: str, *, expired: bool = False) -> None:
        item = self._generation(db, session_id, deletion_id)
        if expired and item.purge_at > self.clock():
            raise HTTPException(409, "The recovery period has not expired.")
        tables = _guard_rows(db, session_id)
        _release_bindings(db, session_id)
        _remove_content(db, session_id, tables)
        db.execute("DELETE FROM ngn_web_session_trash WHERE id = ? AND deletion_id = ?", (session_id, deletion_id))

    async def _purge(self, session_id: str, deletion_id: str, *, expired: bool = False) -> None:
        _guard_process(self.state, session_id)
        await self.store._transaction(lambda db: self._purge_rows(db, session_id, deletion_id, expired=expired))
        _repoint_mains(self.state, session_id, self.state.selected_session_id)
        self.state.bus.delete_session(session_id)
        self.state.wakeups.forget_session(session_id)

    async def purge(self, session_id: str, deletion_id: str) -> dict[str, object]:
        with self._idle("purge session"), anyio.CancelScope(shield=True):
            await finish_on_cancel(self._purge(session_id, deletion_id))
        return {"purged_session_id": session_id}

    async def purge_expired(self) -> list[str]:
        state = self.state
        if self.closed or state.active is not None or state.mutating or state.harness._busy:
            return []

        def candidates(db: sqlite3.Connection) -> list[TrashItem]:
            with closing(
                db.execute(
                    "SELECT id, title, deleted_at, purge_at, deletion_id FROM ngn_web_session_trash "
                    "WHERE purge_at <= ? AND (purge_at, id) > (?, ?) ORDER BY purge_at, id LIMIT ?",
                    (self.clock(), *self._purge_after, PURGE_BATCH),
                )
            ) as cursor:
                return [TrashItem(*row) for row in cursor.fetchall()]

        async def finish() -> list[str]:
            purged: list[str] = []
            batch = await self.store._transaction(candidates)
            if not batch and self._purge_after != _PURGE_START:
                # Wrap only after reaching the end: a full blocked prefix must
                # not hide later expired roots. Earlier repaired rows get retried.
                self._purge_after = _PURGE_START
                batch = await self.store._transaction(candidates)
            for item in batch:
                if self.closed:
                    break
                self._purge_after = (item.purge_at, item.id)
                try:
                    await self._purge(item.id, item.deletion_id, expired=True)
                except HTTPException as error:
                    if error.status_code not in {404, 409}:
                        logger.warning("Trash cleanup deferred a root with HTTP status %s", error.status_code)
                except Exception:
                    # Each root has its own transaction. A failed DELETE rolls
                    # back its journal/history changes, without blocking peers.
                    # Cancellation (BaseException) still joins owned cleanup.
                    logger.exception("Trash cleanup deferred one failed root; continuing other expired roots")
                else:
                    purged.append(item.id)
            return purged

        with self._idle("purge expired trash"), anyio.CancelScope(shield=True):
            result = await finish_on_cancel(finish())
        return result

    async def start(self) -> None:
        await self.initialize()
        await self.purge_expired()
        self.task = asyncio.create_task(self._poll(), name="ngn-web-trash")

    async def _poll(self) -> None:
        while not self.closed:
            await asyncio.sleep(self.interval)
            try:
                await self.purge_expired()
            except Exception:
                logger.exception("Trash cleanup failed; it will retry at a later idle interval")

    def shutdown(self) -> None:
        self.closed = True
        if self.task is not None:
            self.task.cancel()

    async def close(self) -> None:
        self.shutdown()
        if self.task is not None:
            with suppress(asyncio.CancelledError):
                await _join(self.task)
