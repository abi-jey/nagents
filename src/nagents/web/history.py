"""SQLite-row identities and atomic, task-scoped ingress provenance for web history.

The public Message type remains unchanged. Only the web root's normal Harness
session adapter associates the first input write with its admitted inbox item;
standalone, child, legacy, and out-of-band writes remain unannotated.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from contextlib import closing
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

from nagents.channels.runtime import _INBOUND_PREFIX
from nagents.channels.store import InboxStore
from nagents.extensions import AgentPlugin
from nagents.harness.execution import _register_owned_adapter_type
from nagents.harness.runtime import _HarnessSession
from nagents.session.deliveries import DeliveryJournal
from nagents.types import AudioContent
from nagents.types import DocumentContent
from nagents.types import ImageContent
from nagents.types import TextContent

from . import live_captions
from ._async import finish_on_cancel
from .local_delivery import presentation
from .routing import RoutingStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
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


@dataclass
class VoiceInput:
    session_id: str
    run_id: str
    text: str
    voice_session_id: str = ""
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
        voice = self.session.voice.get()
        if voice is not None and not voice.consumed and not voice.expected and context.session_id == voice.session_id:
            voice.owner = asyncio.current_task()
            voice.expected = self.session._message_values(context.session_id, message)
        return message


class WebHistory(_HarnessSession):
    def __init__(self, path: Path, received: Callable[[str, dict[str, object]], None]) -> None:
        super().__init__(path)
        self.store = InboxStore(path, "web-history", 1000)
        self.received = received
        self.ingress: ContextVar[Ingress | None] = ContextVar("ngn_web_ingress", default=None)
        self.voice: ContextVar[VoiceInput | None] = ContextVar("ngn_web_voice_input", default=None)
        self.identity = _InputIdentity(self)
        self._annotations_ready = False
        self._annotations_lock = asyncio.Lock()
        self.deliveries = DeliveryJournal(path)

    async def initialize(self) -> None:
        await super().initialize()
        await self.deliveries.initialize()
        if not self._annotations_ready:
            async with self._annotations_lock:
                if not self._annotations_ready:

                    def create(db: sqlite3.Connection) -> None:
                        live_captions.initialize(db)
                        db.execute(
                            "CREATE TABLE IF NOT EXISTS ngn_web_message_origins ("
                            "history_id INTEGER PRIMARY KEY REFERENCES v2_messages(id), "
                            "inbox_id INTEGER NOT NULL UNIQUE REFERENCES ngn_web_inbox(id))"
                        )
                        db.execute(
                            "CREATE TABLE IF NOT EXISTS ngn_web_voice_messages ("
                            "history_id INTEGER PRIMARY KEY REFERENCES v2_messages(id), "
                            "transcript TEXT NOT NULL)"
                        )
                        # Core clear/delete operations use independent SQLite
                        # connections. A trigger keeps projection removal atomic
                        # even when that connection has foreign_keys disabled.
                        db.execute(
                            "CREATE TRIGGER IF NOT EXISTS ngn_web_voice_message_delete "
                            "BEFORE DELETE ON v2_messages BEGIN "
                            "DELETE FROM ngn_web_voice_messages WHERE history_id = OLD.id; END"
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

    @asynccontextmanager
    async def voice_request(
        self, session_id: str, run_id: str, text: str, *, voice_session_id: str = ""
    ) -> AsyncIterator[None]:
        """Project one admitted voice input without rewriting its model context."""
        if not text.strip() or len(text) > 12000:
            raise ValueError("A voice request needs a bounded caller transcript")
        token = self.voice.set(VoiceInput(session_id, run_id, text, voice_session_id=voice_session_id))
        try:
            # Harness sets a blank title from its prompt before writing history.
            # Claim that same blank slot with the caller's words before running it.
            def title(db: sqlite3.Connection) -> None:
                if voice_session_id and not live_captions.register(db, session_id, voice_session_id):
                    raise ValueError("This voice call was invalidated when chat history was cleared. Reconnect voice.")
                db.execute(
                    "UPDATE harness_sessions SET title = ? WHERE id = ? AND title = ''",
                    (" ".join(text.split())[:80], session_id),
                )

            await self.store._transaction(title)
            yield
        finally:
            self.voice.reset(token)

    async def add_message(self, session_id: str, message: Message) -> int:
        ingress = self.ingress.get()
        values = self._message_values(session_id, message)
        voice = self.voice.get()
        if (
            voice is not None
            and not voice.consumed
            and voice.owner is asyncio.current_task()
            and voice.session_id == session_id
            and voice.expected
        ):
            voice.consumed = True
            if message.role == "user" and values == voice.expected:
                origin = (
                    ingress
                    if (
                        ingress is not None
                        and not ingress.consumed
                        and ingress.owner is asyncio.current_task()
                        and ingress.work.session_id == session_id
                        and ingress.expected == values
                    )
                    else None
                )
                if origin is not None:
                    origin.consumed = True
                return await self._add_voice(voice, values, origin)
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

    async def _add_voice(
        self, voice: VoiceInput, values: tuple[str | None, ...], ingress: Ingress | None = None
    ) -> int:
        async def persist() -> int:
            def insert(db: sqlite3.Connection) -> tuple[int, dict[str, object]]:
                if ingress is not None:
                    work = ingress.work
                    if (
                        db.execute(
                            "SELECT 1 FROM ngn_web_inbox WHERE id = ? AND session_id = ? AND channel = ? "
                            "AND message_id = ? AND status = 'running'",
                            (work.id, voice.session_id, work.channel, work.message_id),
                        ).fetchone()
                        is None
                    ):
                        raise RuntimeError("Voice ingress is no longer owned by this run")
                cursor = db.execute(
                    "INSERT INTO v2_messages(session_id, role, content, tool_calls, tool_call_id, name) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    values,
                )
                history_id = cursor.lastrowid
                assert history_id is not None
                db.execute("UPDATE v2_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (voice.session_id,))
                db.execute("INSERT INTO ngn_web_voice_messages VALUES (?, ?)", (history_id, voice.text))
                if voice.voice_session_id:
                    db.execute("INSERT INTO ngn_web_voice_origins VALUES (?, ?)", (history_id, voice.voice_session_id))
                if ingress is not None:
                    db.execute("INSERT INTO ngn_web_message_origins VALUES (?, ?)", (history_id, ingress.work.id))
                db.row_factory = sqlite3.Row
                row = db.execute(self._select() + " WHERE m.id = ?", (history_id,)).fetchone()
                assert row is not None
                return history_id, self._project(row)

            history_id, record = await self.store._transaction(insert)
            self.received(voice.run_id, record)
            return history_id

        return await finish_on_cancel(persist())

    @staticmethod
    def _select() -> str:
        return (
            "SELECT m.id, m.session_id, m.role, m.tool_calls, m.tool_call_id, m.name, m.created_at, "
            "CASE WHEN i.channel = '' AND i.attachments != '[]' THEN NULL ELSE m.content END AS content, "
            "i.id AS origin_id, i.channel AS origin_channel, i.message_id AS origin_message_id, "
            "i.prompt AS origin_prompt, (SELECT json_group_array(json_object('upload_id', u.upload_id, "
            "'filename', u.filename, 'media_type', u.media_type, 'byte_length', u.byte_length)) "
            "FROM (SELECT upload_id, filename, media_type, byte_length, inbox_id, position "
            "FROM ngn_web_uploads ORDER BY position) u WHERE u.inbox_id = i.id) AS upload_metadata, "
            "v.transcript AS voice_transcript, vo.voice_session_id AS voice_session_id, "
            "f.content AS fork_content, (SELECT json_group_array(json_object('upload_id', u.upload_id, "
            "'filename', u.filename, 'media_type', u.media_type, 'byte_length', u.byte_length)) "
            "FROM (SELECT * FROM ngn_fork_message_uploads ORDER BY position) u "
            "WHERE u.history_id = m.id) AS fork_upload_metadata "
            "FROM v2_messages m "
            "LEFT JOIN ngn_web_voice_messages v ON v.history_id = m.id "
            "LEFT JOIN ngn_web_voice_origins vo ON vo.history_id = m.id "
            "LEFT JOIN ngn_fork_message_display f ON f.history_id = m.id "
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
            "parts": self._project_parts(message.content),
            "message_id": "",
            "ingress_id": "",
            "source_verified": False,
        }
        if row["voice_transcript"] is not None and message.role == "user":
            record.update(content=str(row["voice_transcript"]), parts=[], voice_verified=True)
            if row["voice_session_id"]:
                record["voice_session_id"] = row["voice_session_id"]
            if row["origin_id"] is not None and not row["origin_channel"]:
                record.update(
                    message_id=str(row["origin_message_id"]),
                    ingress_id=f"inbox-{row['origin_id']}",
                    source_verified=True,
                )
            return record
        if row["origin_id"] is None or message.role != "user":
            if row["fork_content"] is not None and message.role == "user":
                record["content"] = str(row["fork_content"])
                uploads = json.loads(row["fork_upload_metadata"])
                if uploads:
                    record.update(uploads=uploads, parts=[])
            return record
        message_id = str(row["origin_message_id"])
        if not row["origin_channel"]:
            uploads = json.loads(row["upload_metadata"])
            if uploads:
                record["uploads"] = uploads
                record["parts"] = []
                record["content"] = str(row["origin_prompt"])
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

    @staticmethod
    def _project_parts(content: object) -> list[dict[str, object]]:
        """JSON-safe multimodal parts for the browser; text parts keep the raw text."""
        if not isinstance(content, list):
            return []
        parts: list[dict[str, object]] = []
        for part in content:
            if isinstance(part, TextContent):
                parts.append({"type": "text", "text": part.text})
            elif isinstance(part, ImageContent):
                parts.append({"type": "image", "media_type": part.media_type, "data_base64": part.base64_data})
            elif isinstance(part, DocumentContent):
                parts.append(
                    {
                        "type": "document",
                        "media_type": part.media_type,
                        "title": part.title or "",
                        "data_base64": part.base64_data,
                    }
                )
            elif isinstance(part, AudioContent):
                parts.append({"type": "audio", "format": part.format, "data_base64": part.base64_data})
        return parts

    async def snapshot(self, session_id: str) -> list[dict[str, object]]:
        def read() -> list[dict[str, object]]:
            # One read transaction covers root membership, context boundary,
            # message rows and origin associations, without changing selection.
            with closing(sqlite3.connect(self.db_path, timeout=5)) as db, db:
                db.execute("BEGIN")
                RoutingStore.root(db, session_id)
                deliveries = self.deliveries._history_in(db, session_id)
                db.row_factory = sqlite3.Row
                rows = db.execute(
                    self._select() + " WHERE m.session_id = ? AND m.id >= COALESCE("
                    "(SELECT compacted_at_message_id FROM v2_sessions WHERE id = ?), 0) ORDER BY m.id",
                    (session_id, session_id),
                ).fetchall()
                records = [self._project(row) for row in rows if row["role"] not in {"system", "developer"}]
                for record in records:
                    attached = [
                        presentation(receipt)
                        for receipt in deliveries.current
                        if str(receipt.origin.anchor_message_id) == record["history_id"]
                    ]
                    if attached:
                        record["deliveries"] = attached
                earlier: list[dict[str, object]] = [
                    {
                        "role": "local_delivery",
                        "content": "",
                        "name": "",
                        "tool_call_id": "",
                        "tool_calls": [],
                        "deliveries": [presentation(receipt, earlier=True)],
                    }
                    for receipt in deliveries.earlier
                ]
                return [*earlier, *live_captions.merge(db, session_id, records)]

        return await finish_on_cancel(asyncio.to_thread(read))

    async def add_live_caption(
        self, session_id: str, voice_session_id: str, event: dict[str, object]
    ) -> dict[str, object]:
        return await self.store._transaction(lambda db: live_captions.append(db, session_id, voice_session_id, event))

    async def register_live_call(self, session_id: str, voice_session_id: str) -> None:
        accepted = await self.store._transaction(lambda db: live_captions.register(db, session_id, voice_session_id))
        if not accepted:
            raise ValueError("This voice call was invalidated when chat history was cleared. Reconnect voice.")


# Only this exact, host-owned adapter has the audited assistant-write path.
# Its execution scope remains task-local when designed roots share an instance.
_register_owned_adapter_type(WebHistory)
