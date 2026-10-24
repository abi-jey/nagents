"""Atomic receipt pickup in the exact child's first user-history write."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from nagents.extensions import AgentPlugin
from nagents.harness.execution import _register_owned_adapter_type
from nagents.harness.runtime import _HarnessSession

from ._async import finish_on_cancel
from .task_store import now

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

    from nagents.extensions import RunContext
    from nagents.types import Message

    from .task_store import TaskStore


class TaskInputIdentity(AgentPlugin):
    def __init__(self, adapter: TaskHistory) -> None:
        self.adapter = adapter

    async def before_run(self, context: RunContext, message: Message) -> Message:
        self.adapter.prepare_input(context.session_id, message)
        return message


class TaskHistory(_HarnessSession):
    def __init__(
        self, store: TaskStore, identifier: str, child: str, received: Callable[[dict[str, object]], None]
    ) -> None:
        super().__init__(store.db.db_path)
        self.store, self.identifier, self.child, self.received = store, identifier, child, received
        self.owner: asyncio.Task[object] | None = None
        self.expected: tuple[str | None, ...] = ()
        self.consumed = False
        self.identity = TaskInputIdentity(self)

    def prepare_input(self, session_id: str, message: Message) -> None:
        if session_id == self.child and not self.consumed:
            self.owner = asyncio.current_task()
            self.expected = self._message_values(session_id, message)

    async def add_message(self, session_id: str, message: Message) -> int:
        values = self._message_values(session_id, message)
        if self.consumed or self.owner is not asyncio.current_task() or session_id != self.child or not self.expected:
            return await super().add_message(session_id, message)
        self.consumed = True
        if message.role != "user" or values != self.expected:
            raise RuntimeError("Child input changed after receipt identity was accepted")

        async def persist() -> int:
            def insert(db: sqlite3.Connection) -> tuple[int, dict[str, object]]:
                row = self.store.row(db, self.identifier)
                self.store.root(db, str(row["root"]))
                if row["status"] != "queued" or row["epoch"] != self.store.epoch or row["storage_id"] != session_id:
                    raise RuntimeError("Child input is no longer owned by this runtime")
                cursor = db.execute(
                    "INSERT INTO v2_messages(session_id, role, content, tool_calls, tool_call_id, name) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    values,
                )
                identifier = cursor.lastrowid
                assert identifier is not None
                db.execute("UPDATE v2_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (session_id,))
                db.execute(
                    "UPDATE ngn_web_agent_inputs SET status = 'picked_up', revision = revision + 1, "
                    "history_id = ?, picked_up_at = ? WHERE message_id = ?",
                    (identifier, now(), self.identifier),
                )
                return identifier, self.store.receipt(self.store.row(db, self.identifier))

            identifier, receipt = await self.store.db._transaction(insert)
            self.received(receipt)
            return identifier

        return await finish_on_cancel(persist())


_register_owned_adapter_type(TaskHistory)
