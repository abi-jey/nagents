"""Durable channel admission, independent of the session history schema.

Processed rows (including their envelopes) are retained indefinitely for durable
deduplication. Applications must explicitly manage retention; deleting a row also
removes its deduplication guarantee. A running row is never replayed automatically.
Listener ownership is enforced by the runtime *in this process*, not by this store
or a cross-process lease. Do not run multiple processes for the same DB/session.
"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING
from typing import Literal
from typing import TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Coroutine
    from pathlib import Path

T = TypeVar("T")
InboxStatus = Literal["completed", "failed", "interrupted"]


async def finish_on_cancel(operation: Coroutine[object, object, T]) -> T:
    """Join an operation even when cancelled, then propagate cancellation.

    In particular, a cancelled admission may have committed. Waiting for the
    transaction/thread to finish prevents orphaned writes; source retries dedup.
    """
    task = asyncio.create_task(operation)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


class Admission(Enum):
    INSERTED = "inserted"
    DUPLICATE = "duplicate"
    FULL = "full"


@dataclass(frozen=True)
class InboxItem:
    id: int
    channel: str
    message_id: str
    envelope: str


class InboxStore:
    """Short, atomic SQLite transactions; no transaction spans model/tool I/O."""

    def __init__(self, db_path: Path, session_id: str, inbox_limit: int) -> None:
        self.db_path = db_path
        self.session_id = session_id
        self.inbox_limit = inbox_limit

    async def _transaction(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        def execute() -> T:
            with closing(sqlite3.connect(self.db_path, timeout=5)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                return operation(db)

        return await finish_on_cancel(asyncio.to_thread(execute))

    async def initialize(self) -> None:
        def initialize(db: sqlite3.Connection) -> None:
            db.execute(
                """CREATE TABLE IF NOT EXISTS nagents_channel_inbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    envelope TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued', 'running', 'completed', 'failed', 'interrupted')),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (session_id, channel, message_id)
                )"""
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS nagents_channel_inbox_pending "
                "ON nagents_channel_inbox (session_id, status, id)"
            )
            self._interrupt(db)

        await self._transaction(initialize)

    def _interrupt(self, db: sqlite3.Connection) -> None:
        db.execute(
            "UPDATE nagents_channel_inbox SET status = 'interrupted', updated_at = CURRENT_TIMESTAMP "
            "WHERE session_id = ? AND status = 'running'",
            (self.session_id,),
        )

    async def interrupt_running(self) -> None:
        await self._transaction(self._interrupt)

    async def queued_channels(self) -> set[str]:
        """Inspect startup binding requirements without claiming queued work."""

        def queued_channels(db: sqlite3.Connection) -> set[str]:
            rows = db.execute(
                "SELECT DISTINCT channel FROM nagents_channel_inbox WHERE session_id = ? AND status = 'queued'",
                (self.session_id,),
            ).fetchall()
            return {row[0] for row in rows}

        return await self._transaction(queued_channels)

    async def admit(self, channel: str, message_id: str, envelope: str) -> Admission:
        def admit(db: sqlite3.Connection) -> Admission:
            duplicate = db.execute(
                "SELECT 1 FROM nagents_channel_inbox WHERE session_id = ? AND channel = ? AND message_id = ?",
                (self.session_id, channel, message_id),
            ).fetchone()
            if duplicate:
                return Admission.DUPLICATE
            pending = db.execute(
                "SELECT COUNT(*) FROM nagents_channel_inbox WHERE session_id = ? AND status IN ('queued', 'running')",
                (self.session_id,),
            ).fetchone()
            if pending[0] >= self.inbox_limit:
                return Admission.FULL
            db.execute(
                "INSERT INTO nagents_channel_inbox (session_id, channel, message_id, envelope) VALUES (?, ?, ?, ?)",
                (self.session_id, channel, message_id, envelope),
            )
            return Admission.INSERTED

        return await self._transaction(admit)

    async def claim(self) -> InboxItem | None:
        def claim(db: sqlite3.Connection) -> InboxItem | None:
            row = db.execute(
                "SELECT id, channel, message_id, envelope FROM nagents_channel_inbox "
                "WHERE session_id = ? AND status = 'queued' ORDER BY id LIMIT 1",
                (self.session_id,),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE nagents_channel_inbox SET status = 'running', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (row[0],),
            )
            return InboxItem(id=row[0], channel=row[1], message_id=row[2], envelope=row[3])

        return await self._transaction(claim)

    async def finish(self, item: InboxItem, status: InboxStatus) -> None:
        def finish(db: sqlite3.Connection) -> None:
            db.execute(
                "UPDATE nagents_channel_inbox SET status = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND session_id = ? AND status = 'running'",
                (status, item.id, self.session_id),
            )

        await self._transaction(finish)
