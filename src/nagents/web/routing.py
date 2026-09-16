"""Atomic web admission and root-session routing, independent of Harness selection.

Bindings and the admitted target are committed in the same transaction as dedup.
Started work is interrupted on restart, never replayed (tools may have acted).
Unclaimed work survives connector configuration changes and process shutdown.
"""

from __future__ import annotations

import json
import uuid
from contextlib import closing
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import HTTPException

from nagents.channels.runtime import _INBOUND_PREFIX
from nagents.channels.store import InboxStore

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path

    from nagents.channels.types import ChannelCommand


@dataclass(frozen=True)
class Work:
    id: int
    session_id: str
    channel: str
    message_id: str
    prompt: str
    conversation_id: str
    thread_id: str
    reply_to: str
    acknowledgement: str
    command: str = ""


@dataclass(frozen=True)
class ChatOwner:
    channel: str
    conversation_id: str
    conflicted: bool = False


_UNAVAILABLE = "Session not found. Use /sessions to list sessions available to this chat."
_RECOVERED = (
    "This chat's previous session is unavailable. A new isolated session was created; please send your request again."
)
_MIGRATED = "Session routing permissions changed. Use /sessions to view sessions available to this chat."
_UNKNOWN = "Unknown session command. Use /sessions, /session [ID|main|default|new], /new [title], or /compact."


class RoutingStore(InboxStore):
    def __init__(self, path: Path) -> None:
        super().__init__(path, "web-host", 1000)

    async def initialize(self) -> None:
        def initialize(db: sqlite3.Connection) -> None:
            db.execute(
                "CREATE TABLE IF NOT EXISTS ngn_web_bindings ("
                "channel TEXT NOT NULL, conversation_id TEXT NOT NULL, session_id TEXT NOT NULL, "
                "default_session_id TEXT NOT NULL, PRIMARY KEY(channel, conversation_id))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS ngn_web_inbox ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, channel TEXT NOT NULL, "
                "message_id TEXT NOT NULL, prompt TEXT NOT NULL, conversation_id TEXT NOT NULL DEFAULT '', "
                "thread_id TEXT NOT NULL DEFAULT '', reply_to TEXT NOT NULL DEFAULT '', "
                "acknowledgement TEXT NOT NULL DEFAULT '', command TEXT NOT NULL DEFAULT '', "
                "status TEXT NOT NULL DEFAULT 'queued', "
                "UNIQUE(channel, message_id))"
            )
            db.execute("CREATE INDEX IF NOT EXISTS ngn_web_inbox_pending ON ngn_web_inbox(status, id)")
            if "command" not in {str(row[1]) for row in db.execute("PRAGMA table_info(ngn_web_inbox)")}:
                db.execute("ALTER TABLE ngn_web_inbox ADD COLUMN command TEXT NOT NULL DEFAULT ''")
            db.execute(
                "CREATE TABLE IF NOT EXISTS ngn_web_deleted_messages ("
                "channel TEXT NOT NULL, message_id TEXT NOT NULL, PRIMARY KEY(channel, message_id))"
            )
            self._migrate_owners(db)
            db.execute("UPDATE ngn_web_inbox SET status = 'interrupted' WHERE status = 'running'")
            self._quarantine_work(db)

        await self._transaction(initialize)

    @staticmethod
    def _migrate_owners(db: sqlite3.Connection) -> None:
        existed = (
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ngn_web_session_owners'"
            ).fetchone()
            is not None
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS ngn_web_session_owners ("
            "session_id TEXT PRIMARY KEY, channel TEXT NOT NULL, conversation_id TEXT NOT NULL, "
            "conflicted INTEGER NOT NULL DEFAULT 0 CHECK(conflicted IN (0, 1)))"
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS ngn_web_session_owners_chat "
            "ON ngn_web_session_owners(channel, conversation_id, conflicted, session_id)"
        )
        # Accepted provenance, never command text or model-supplied destinations.
        # Keep the first owner forever; contradictory/incomplete evidence marks
        # the root conflicted forever, even if later history/bindings are removed.
        db.execute(
            "INSERT INTO ngn_web_session_owners(session_id, channel, conversation_id, conflicted) "
            "SELECT session_id, channel, conversation_id, channel = '' OR conversation_id = '' FROM ("
            "SELECT session_id, channel, conversation_id FROM ngn_web_bindings UNION "
            "SELECT default_session_id, channel, conversation_id FROM ngn_web_bindings UNION "
            "SELECT session_id, channel, conversation_id FROM ngn_web_inbox WHERE channel != '') "
            "WHERE session_id != '' "
            "ON CONFLICT(session_id) DO UPDATE SET conflicted = "
            "ngn_web_session_owners.conflicted OR excluded.conflicted OR "
            "ngn_web_session_owners.channel != excluded.channel OR "
            "ngn_web_session_owners.conversation_id != excluded.conversation_id"
        )
        if not existed:
            # Old /sessions acknowledgements may contain other chats' titles.
            # Do not replay the command, or emit its old cross-chat catalog.
            db.execute(
                "UPDATE ngn_web_inbox SET acknowledgement = ? "
                "WHERE channel != '' AND status = 'queued' AND acknowledgement != ''",
                (_MIGRATED,),
            )

    @staticmethod
    def _owner(db: sqlite3.Connection, session_id: str) -> ChatOwner | None:
        with closing(
            db.execute(
                "SELECT channel, conversation_id, conflicted FROM ngn_web_session_owners WHERE session_id = ?",
                (session_id,),
            )
        ) as cursor:
            row = cursor.fetchone()
        return ChatOwner(str(row[0]), str(row[1]), bool(row[2])) if row is not None else None

    async def owner(self, session_id: str) -> ChatOwner | None:
        return await self._transaction(lambda db: self._owner(db, session_id))

    @staticmethod
    def _own(db: sqlite3.Connection, session_id: str, channel: str, conversation_id: str) -> None:
        if not channel.strip() or not conversation_id.strip():
            raise ValueError("Chat ownership requires nonblank connection and conversation IDs")
        owner = RoutingStore._owner(db, session_id)
        if owner is not None and owner != ChatOwner(channel, conversation_id):
            raise HTTPException(409, _UNAVAILABLE)
        db.execute(
            "INSERT OR IGNORE INTO ngn_web_session_owners VALUES (?, ?, ?, 0)", (session_id, channel, conversation_id)
        )

    async def assign_owner(self, session_id: str, channel: str, conversation_id: str) -> None:
        """Trusted management assignment; chat commands never adopt unowned roots.

        Call after initialize(), within the host's mutation/execution ownership
        boundary. This operation cannot transfer ownership or clear conflicts.
        """

        def assign(db: sqlite3.Connection) -> None:
            self.root(db, session_id)
            self._own(db, session_id, channel, conversation_id)

        await self._transaction(assign)

    @staticmethod
    def chat_root(db: sqlite3.Connection, session_id: str, channel: str, conversation_id: str) -> str:
        with closing(
            db.execute(
                "SELECT 1 FROM ngn_web_session_owners o JOIN harness_sessions h ON h.id = o.session_id "
                "JOIN v2_sessions s ON s.id = h.id WHERE h.id = ? AND o.channel = ? "
                "AND o.conversation_id = ? AND o.conflicted = 0",
                (session_id, channel, conversation_id),
            )
        ) as cursor:
            if cursor.fetchone() is None:
                raise HTTPException(404, _UNAVAILABLE)
        return session_id

    @classmethod
    def execution_root(cls, db: sqlite3.Connection, session_id: str) -> str:
        cls.root(db, session_id)
        owner = cls._owner(db, session_id)
        if owner is not None and owner.conflicted:
            raise HTTPException(409, "Session ownership is conflicted. Use a different session.")
        return session_id

    @classmethod
    def new_chat_root(cls, db: sqlite3.Connection, channel: str, conversation_id: str, title: str) -> str:
        session_id = cls.new_root(db, title)
        cls._own(db, session_id, channel, conversation_id)
        return session_id

    @classmethod
    def adoptable_root(cls, db: sqlite3.Connection, session_id: str, channel: str, conversation_id: str) -> str:
        """Attach this chat to an existing unowned root, adopting it permanently.

        A root already owned by another chat (or conflicted) is never taken over;
        ownership is recorded in the caller's transaction, before any dispatch.
        """
        cls.root(db, session_id)
        if cls._owner(db, session_id) is not None:
            raise HTTPException(404, _UNAVAILABLE)
        cls._own(db, session_id, channel, conversation_id)
        return session_id

    @staticmethod
    def _quarantine_work(db: sqlite3.Connection) -> None:
        # Retain rejected work and provenance, but don't let it consume queue
        # capacity forever or execute/acknowledge another chat's old context.
        db.execute(
            "UPDATE ngn_web_inbox SET status = 'failed' WHERE status = 'queued' AND ("
            "EXISTS (SELECT 1 FROM ngn_web_session_owners o WHERE o.session_id = ngn_web_inbox.session_id "
            "AND o.conflicted = 1) OR (channel != '' AND NOT EXISTS ("
            "SELECT 1 FROM ngn_web_session_owners o WHERE o.session_id = ngn_web_inbox.session_id "
            "AND o.channel = ngn_web_inbox.channel AND o.conversation_id = ngn_web_inbox.conversation_id "
            "AND o.conflicted = 0)))"
        )

    async def validate_work(self, work: Work) -> None:
        def validate(db: sqlite3.Connection) -> None:
            self.execution_root(db, work.session_id)
            if work.channel:
                self.chat_root(db, work.session_id, work.channel, work.conversation_id)

        await self._transaction(validate)

    @staticmethod
    def _chat_sessions(db: sqlite3.Connection, channel: str, conversation: str) -> str:
        def rows(statement: str, parameters: tuple[str, ...]) -> list[tuple[str, str]]:
            with closing(db.execute(statement, parameters)) as cursor:
                return [(str(row[0]), str(row[1] or "")) for row in cursor.fetchall()]

        owned = rows(
            "SELECT h.id, h.title FROM harness_sessions h JOIN v2_sessions s ON s.id = h.id "
            "JOIN ngn_web_session_owners o ON o.session_id = h.id "
            "WHERE o.channel = ? AND o.conversation_id = ? AND o.conflicted = 0 ORDER BY h.rowid DESC LIMIT 100",
            (channel, conversation),
        )
        # Roots with no owner yet are chat-creatable web sessions; a trusted chat
        # may attach one and adopt it. Foreign-owned roots are never listed.
        available = rows(
            "SELECT h.id, h.title FROM harness_sessions h JOIN v2_sessions s ON s.id = h.id "
            "WHERE NOT EXISTS (SELECT 1 FROM ngn_web_session_owners o WHERE o.session_id = h.id) "
            "ORDER BY h.rowid DESC LIMIT 100",
            (),
        )
        lines = ["Sessions:"]
        units = 10
        for id, title in owned:
            line = f"{id} — {title or 'New session'}"
            length = len(line.encode("utf-16-le")) // 2 + 1
            if units + length > 3500:
                lines.append("More sessions are available in the web UI.")
                return "\n".join(lines)
            lines.append(line)
            units += length
        if available:
            lines.append("Available to attach (send /session ID to adopt):")
            for id, title in available:
                line = f"{id} — {title or 'New session'}"
                length = len(line.encode("utf-16-le")) // 2 + 1
                if units + length > 3500:
                    break
                lines.append(line)
                units += length
        return "\n".join(lines)

    async def acknowledgement(self, work: Work) -> str:
        """Authorize cached control replies without replaying routing commands.

        Rebuild catalogs on delivery: a legacy/imported acknowledgement may embed
        unrelated titles even if the journal row itself belongs to this chat.
        """

        def render(db: sqlite3.Connection) -> str:
            self.chat_root(db, work.session_id, work.channel, work.conversation_id)
            text = work.acknowledgement
            if text == "Sessions:" or text.startswith("Sessions:\n"):
                return self._chat_sessions(db, work.channel, work.conversation_id)
            for prefix in ("Session: ", "Default session: "):
                if text.startswith(prefix):
                    owner = self._owner(db, text.removeprefix(prefix))
                    return text if owner == ChatOwner(work.channel, work.conversation_id) else _MIGRATED
            return text if text in {_UNAVAILABLE, _RECOVERED, _MIGRATED, _UNKNOWN} else _MIGRATED

        return await self._transaction(render)

    @staticmethod
    def root(db: sqlite3.Connection, session_id: str) -> str:
        with closing(
            db.execute(
                "SELECT 1 FROM harness_sessions h JOIN v2_sessions s ON s.id = h.id WHERE h.id = ?", (session_id,)
            )
        ) as cursor:
            if cursor.fetchone() is None:
                raise HTTPException(404, "Session not found in this workspace.")
        return session_id

    @staticmethod
    def new_root(db: sqlite3.Connection, title: str) -> str:
        session_id = f"ngn-{uuid.uuid4().hex[:16]}"
        with closing(db.execute("INSERT INTO v2_sessions (id, user_id) VALUES (?, 'harness')", (session_id,))):
            pass
        with closing(db.execute("INSERT INTO harness_sessions (id, title) VALUES (?, ?)", (session_id, title[:80]))):
            pass
        return session_id

    def capacity(self, db: sqlite3.Connection) -> None:
        self._quarantine_work(db)
        if (
            db.execute("SELECT COUNT(*) FROM ngn_web_inbox WHERE status IN ('queued', 'running')").fetchone()[0]
            >= self.inbox_limit
        ):
            raise HTTPException(429, "Message inbox is full. Retry with the same message ID.")

    async def web(self, session_id: str, message_id: str, prompt: str) -> str:
        def admit(db: sqlite3.Connection) -> str:
            self.execution_root(db, session_id)
            row = db.execute(
                "SELECT session_id, prompt FROM ngn_web_inbox WHERE channel = '' AND message_id = ?", (message_id,)
            ).fetchone()
            if row:
                if row != (session_id, prompt):
                    raise HTTPException(409, "Message ID already belongs to a different submission.")
                return session_id
            self.capacity(db)
            db.execute(
                "INSERT INTO ngn_web_inbox(session_id, channel, message_id, prompt) VALUES (?, '', ?, ?)",
                (session_id, message_id, prompt),
            )
            return session_id

        return await self._transaction(admit)

    async def receive(self, channel: str, envelope: str, main: str, command: ChannelCommand | None) -> None:
        payload = json.loads(envelope)

        def admit(db: sqlite3.Connection) -> None:
            if db.execute(
                "SELECT 1 FROM ngn_web_deleted_messages WHERE channel = ? AND message_id = ?",
                (channel, payload["message_id"]),
            ).fetchone():
                return
            if db.execute(
                "SELECT 1 FROM ngn_web_inbox WHERE channel = ? AND message_id = ?", (channel, payload["message_id"])
            ).fetchone():
                return
            self.capacity(db)
            conversation = payload["conversation_id"]
            fresh_title = (
                command.arguments.strip()
                if command is not None and command.name == "new"
                else f"{channel}: {conversation}"
            )
            row = db.execute(
                "SELECT session_id, default_session_id FROM ngn_web_bindings WHERE channel = ? AND conversation_id = ?",
                (channel, conversation),
            ).fetchone()
            if row:
                target, default = row
            else:
                target = default = self.new_chat_root(db, channel, conversation, fresh_title)
            ack = ""
            kind = ""
            recovered = False
            try:
                self.chat_root(db, target, channel, conversation)
            except HTTPException:
                # An unsafe legacy binding is local to this chat. Keep its old
                # ownership/history quarantined and consume this request only as
                # a fixed recovery notice, never as model input on mixed history.
                target = self.new_chat_root(db, channel, conversation, fresh_title)
                recovered = True
                ack = _RECOVERED
                try:
                    self.chat_root(db, default, channel, conversation)
                except HTTPException:
                    default = target
            if command is not None:
                argument = command.arguments.strip()
                if command.name == "sessions":
                    ack = self._chat_sessions(db, channel, conversation)
                elif command.name == "compact":
                    # Compaction is model work, not a cached reply: admit it as the
                    # chat's current session and let the host run it when idle.
                    if argument:
                        ack = _UNKNOWN
                    else:
                        kind = "compact"
                elif command.name in {"session", "new"}:
                    if command.name == "session" and argument.startswith("default "):
                        candidate = argument.removeprefix("default ").strip()
                        try:
                            selected = self.chat_root(db, candidate, channel, conversation)
                        except HTTPException:
                            try:
                                selected = self.adoptable_root(db, candidate, channel, conversation)
                            except HTTPException:
                                selected = ""
                        if selected:
                            default = selected
                            ack = f"Default session: {default}"
                        else:
                            ack = _UNAVAILABLE
                    elif command.name == "new" or argument == "new":
                        if row and not recovered:
                            target = self.new_chat_root(
                                db,
                                channel,
                                conversation,
                                argument if command.name == "new" else f"{channel}: {conversation}",
                            )
                        ack = f"Session: {target}"
                    elif argument:
                        candidate = main if argument == "main" else default if argument == "default" else argument
                        selected = ""
                        try:
                            selected = self.chat_root(db, candidate, channel, conversation)
                        except HTTPException:
                            try:
                                selected = self.adoptable_root(db, candidate, channel, conversation)
                            except HTTPException:
                                selected = ""
                        if selected:
                            target = selected
                            ack = f"Session: {target}"
                        else:
                            ack = _UNAVAILABLE
                    if not ack:
                        ack = f"Session: {target}"
                else:
                    ack = _UNKNOWN
            self.chat_root(db, target, channel, conversation)
            db.execute(
                "INSERT INTO ngn_web_bindings VALUES (?, ?, ?, ?) ON CONFLICT(channel, conversation_id) "
                "DO UPDATE SET session_id = excluded.session_id, default_session_id = excluded.default_session_id",
                (channel, conversation, target, default),
            )
            db.execute(
                "INSERT INTO ngn_web_inbox(session_id, channel, message_id, prompt, conversation_id, thread_id, "
                "reply_to, acknowledgement, command) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    target,
                    channel,
                    payload["message_id"],
                    _INBOUND_PREFIX + envelope,
                    conversation,
                    payload["thread_id"],
                    payload["reply_to"],
                    ack,
                    kind,
                ),
            )

        await self._transaction(admit)

    @staticmethod
    def eligible(web_only: bool, available_channels: tuple[str, ...] | None) -> tuple[str, tuple[str | bool, ...]]:
        predicate = (
            "status = 'queued' AND (? = 0 OR channel = '') AND EXISTS ("
            "SELECT 1 FROM harness_sessions h JOIN v2_sessions s ON s.id = h.id WHERE h.id = ngn_web_inbox.session_id)"
        )
        parameters: tuple[str | bool, ...] = (web_only,)
        if available_channels is not None:
            if available_channels:
                slots = ",".join("?" for _ in available_channels)
                predicate += f" AND (acknowledgement = '' OR channel IN ({slots}))"
                parameters += available_channels
            else:
                predicate += " AND acknowledgement = ''"
        return predicate, parameters

    async def claim_work(
        self, *, web_only: bool = False, available_channels: tuple[str, ...] | None = None
    ) -> Work | None:
        predicate, parameters = self.eligible(web_only, available_channels)

        def claim(db: sqlite3.Connection) -> Work | None:
            self._quarantine_work(db)
            row = db.execute(
                "SELECT id, session_id, channel, message_id, prompt, conversation_id, thread_id, reply_to, "
                f"acknowledgement, command FROM ngn_web_inbox WHERE {predicate} ORDER BY id LIMIT 1",
                parameters,
            ).fetchone()
            if row is None:
                return None
            db.execute("UPDATE ngn_web_inbox SET status = 'running' WHERE id = ?", (row[0],))
            return Work(*row)

        return await self._transaction(claim)

    async def has_pending(self, *, web_only: bool = False, available_channels: tuple[str, ...] | None = None) -> bool:
        predicate, parameters = self.eligible(web_only, available_channels)

        def pending(db: sqlite3.Connection) -> bool:
            self._quarantine_work(db)
            return (
                db.execute(
                    f"SELECT 1 FROM ngn_web_inbox WHERE {predicate} LIMIT 1",
                    parameters,
                ).fetchone()
                is not None
            )

        return await self._transaction(pending)

    async def release_work(self, work: Work) -> None:
        """Return a claim that was never executed, before any external operation."""
        await self.finish_work(work, "queued")

    async def interrupt_work(self) -> None:
        def interrupt(db: sqlite3.Connection) -> None:
            db.execute("UPDATE ngn_web_inbox SET status = 'interrupted' WHERE status = 'running'")

        await self._transaction(interrupt)

    async def finish_work(self, work: Work, status: str) -> None:
        await self._transaction(
            lambda db: (
                db.execute("UPDATE ngn_web_inbox SET status = ? WHERE id = ? AND status = 'running'", (status, work.id))
                and None
            )
        )

    async def bindings(self) -> list[dict[str, str]]:
        def read(db: sqlite3.Connection) -> list[dict[str, str]]:
            return [
                dict(zip(("channel", "conversation_id", "session_id"), row, strict=True))
                for row in db.execute(
                    "SELECT channel, conversation_id, session_id FROM ngn_web_bindings ORDER BY channel, conversation_id"
                )
            ]

        return await self._transaction(read)

    async def activity_bindings(self, session_id: str) -> list[dict[str, str]]:
        """Return the permanent owner of a live root, independent of chat selection."""

        def read(db: sqlite3.Connection) -> list[dict[str, str]]:
            return [
                dict(zip(("channel", "conversation_id", "session_id"), row, strict=True))
                for row in db.execute(
                    "SELECT o.channel, o.conversation_id, o.session_id FROM ngn_web_session_owners o "
                    "JOIN harness_sessions h ON h.id = o.session_id JOIN v2_sessions s ON s.id = h.id "
                    "WHERE o.session_id = ? AND o.conflicted = 0",
                    (session_id,),
                )
            ]

        return await self._transaction(read)
