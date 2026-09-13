"""SQLite-row identities and atomic, task-scoped ingress provenance for web history.

The public Message type remains unchanged. Only the web root's normal Harness
session adapter associates the first input write with its admitted inbox item;
standalone, child, legacy, and out-of-band writes remain unannotated.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

from nagents.channels.runtime import _INBOUND_PREFIX
from nagents.channels.store import InboxStore
from nagents.channels.store import finish_on_cancel
from nagents.extensions import AgentPlugin
from nagents.harness.runtime import _HarnessSession

from .routing import RoutingStore

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Iterator
    from pathlib import Path

    from nagents.extensions import RunContext
    from nagents.types import Message

    from .routing import Work


@dataclass
class Ingress:
    work: Work
    run_id: str
    owner: asyncio.Task[object] | None = None
    expected: tuple[str | None, ...] = ()
    consumed: bool = False


class _InputIdentity(AgentPlugin):
    def __init__(self, session: WebHistory) -> None:
        self.session = session

    async def before_run(self, context: RunContext, message: Message) -> Message:
        ingress = self.session.ingress.get()
        if (
            ingress is not None
            and not ingress.consumed
            and not ingress.expected
            and context.session_id == ingress.work.session_id
        ):
            # Install after configured hooks. Writes made by those hooks before
            # this point, or by other tasks, cannot steal the ingress identity.
            ingress.owner = asyncio.current_task()
            ingress.expected = self.session._message_values(context.session_id, message)
        return message


class WebHistory(_HarnessSession):
    def __init__(self, path: Path, received: Callable[[str, dict[str, object]], None]) -> None:
        super().__init__(path)
        self.store = InboxStore(path, "web-history", 1000)
        self.received = received
        self.ingress: ContextVar[Ingress | None] = ContextVar("ngn_web_ingress", default=None)
        self.identity = _InputIdentity(self)
        self._annotations_ready = False
        self._annotations_lock = asyncio.Lock()

    async def initialize(self) -> None:
        await super().initialize()
        if not self._annotations_ready:
            async with self._annotations_lock:
                if not self._annotations_ready:

                    def create(db: sqlite3.Connection) -> None:
                        db.execute(
                            "CREATE TABLE IF NOT EXISTS ngn_web_message_origins ("
                            "history_id INTEGER PRIMARY KEY REFERENCES v2_messages(id), "
                            "inbox_id INTEGER NOT NULL UNIQUE REFERENCES ngn_web_inbox(id))"
                        )

                    await self.store._transaction(create)
                    self._annotations_ready = True

    @contextmanager
    def admitted(self, work: Work, run_id: str) -> Iterator[None]:
        token = self.ingress.set(Ingress(work, run_id))
        try:
            yield
        finally:
            self.ingress.reset(token)

    async def add_message(self, session_id: str, message: Message) -> int:
        ingress = self.ingress.get()
        values = self._message_values(session_id, message)
        if (
            ingress is None
            or ingress.consumed
            or ingress.owner is not asyncio.current_task()
            or ingress.work.session_id != session_id
            or not ingress.expected
        ):
            return await super().add_message(session_id, message)
        # Reserve before yielding. The task-scoped admission is authoritative;
        # matching serialized input is an extra guard, not a content-based lookup.
        ingress.consumed = True
        if message.role != "user" or values != ingress.expected:
            # Unexpected post-hook transformations are left unverified; never
            # transfer this ingress to a later child/wakeup notification instead.
            return await super().add_message(session_id, message)

        async def persist() -> int:
            def insert(db: sqlite3.Connection) -> tuple[int, dict[str, object]]:
                work = ingress.work
                if (
                    db.execute(
                        "SELECT 1 FROM ngn_web_inbox WHERE id = ? AND session_id = ? AND channel = ? "
                        "AND message_id = ? AND status = 'running'",
                        (work.id, session_id, work.channel, work.message_id),
                    ).fetchone()
                    is None
                ):
                    raise RuntimeError("Ingress is no longer owned by this run")
                cursor = db.execute(
                    "INSERT INTO v2_messages(session_id, role, content, tool_calls, tool_call_id, name) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    values,
                )
                history_id = cursor.lastrowid
                assert history_id is not None
                db.execute("UPDATE v2_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (session_id,))
                db.execute("INSERT INTO ngn_web_message_origins VALUES (?, ?)", (history_id, work.id))
                db.row_factory = sqlite3.Row
                row = db.execute(self._select() + " WHERE m.id = ?", (history_id,)).fetchone()
                assert row is not None
                return history_id, self._project(row)

            history_id, record = await self.store._transaction(insert)
            self.received(ingress.run_id, record)
            return history_id

        # A cancellation racing commit still finishes the association and its
        # notification before propagating; restart snapshots never guess IDs.
        return await finish_on_cancel(persist())

    @staticmethod
    def _select() -> str:
        return (
            "SELECT m.*, i.id AS origin_id, i.channel AS origin_channel, i.message_id AS origin_message_id, "
            "i.prompt AS origin_prompt FROM v2_messages m "
            "LEFT JOIN ngn_web_message_origins o ON o.history_id = m.id "
            "LEFT JOIN ngn_web_inbox i ON i.id = o.inbox_id AND i.session_id = m.session_id"
        )

    def _project(self, row: sqlite3.Row) -> dict[str, object]:
        message = self._row_to_message(row)
        record: dict[str, object] = {
            "history_id": str(row["id"]),
            "role": message.role,
            "content": message.content if isinstance(message.content, str) else "",
            "name": message.name or "",
            "tool_call_id": message.tool_call_id or "",
            "tool_calls": [
                {"id": call.id, "name": call.name, "arguments": call.arguments} for call in message.tool_calls
            ],
            "message_id": "",
            "ingress_id": "",
            "source_verified": False,
        }
        if row["origin_id"] is None or message.role != "user":
            return record
        message_id = str(row["origin_message_id"])
        if row["origin_channel"]:
            # Parse the durable ingress journal, never the user/history content.
            try:
                payload = json.loads(str(row["origin_prompt"]).removeprefix(_INBOUND_PREFIX))
            except ValueError:
                return record
            if (
                not isinstance(payload, dict)
                or type(payload.get("version")) is not int
                or payload.get("version") != 1
                or payload.get("channel") != row["origin_channel"]
                or payload.get("message_id") != message_id
                or not isinstance(payload.get("conversation_id"), str)
                or not payload.get("conversation_id")
                or not all(
                    isinstance(payload.get(key), str)
                    for key in (
                        "sender_id",
                        "text",
                        "thread_id",
                        "reply_to",
                        "event_type",
                    )
                )
                or not isinstance(payload.get("attachments"), list)
                or not isinstance(payload.get("metadata"), dict)
            ):
                return record
            record["source"] = payload
            record["content"] = payload["text"]
            if message.content != row["origin_prompt"]:
                record["stored_content"] = str(row["content"])
        record.update(message_id=message_id, ingress_id=f"inbox-{row['origin_id']}", source_verified=True)
        return record

    async def snapshot(self, session_id: str) -> list[dict[str, object]]:
        def read() -> list[dict[str, object]]:
            # One read transaction covers root membership, context boundary,
            # message rows and origin associations, without changing selection.
            with closing(sqlite3.connect(self.db_path, timeout=5)) as db, db:
                db.execute("BEGIN")
                RoutingStore.root(db, session_id)
                db.row_factory = sqlite3.Row
                rows = db.execute(
                    self._select() + " WHERE m.session_id = ? AND m.id >= COALESCE("
                    "(SELECT compacted_at_message_id FROM v2_sessions WHERE id = ?), 0) ORDER BY m.id",
                    (session_id, session_id),
                ).fetchall()
                return [self._project(row) for row in rows if row["role"] not in {"system", "developer"}]

        return await finish_on_cancel(asyncio.to_thread(read))
