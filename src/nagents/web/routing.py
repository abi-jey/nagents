"""Atomic web admission and root-session routing, independent of Harness selection.

Bindings and the admitted target are committed in the same transaction as dedup.
Started work is interrupted on restart, never replayed (tools may have acted).
Unclaimed work survives connector configuration changes and process shutdown.
"""

from __future__ import annotations

import json
import uuid
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
                "acknowledgement TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'queued', "
                "UNIQUE(channel, message_id))"
            )
            db.execute("CREATE INDEX IF NOT EXISTS ngn_web_inbox_pending ON ngn_web_inbox(status, id)")
            db.execute("UPDATE ngn_web_inbox SET status = 'interrupted' WHERE status = 'running'")

        await self._transaction(initialize)

    @staticmethod
    def root(db: sqlite3.Connection, session_id: str) -> str:
        if (
            db.execute(
                "SELECT 1 FROM harness_sessions h JOIN v2_sessions s ON s.id = h.id WHERE h.id = ?", (session_id,)
            ).fetchone()
            is None
        ):
            raise HTTPException(404, "Session not found in this workspace.")
        return session_id

    @staticmethod
    def new_root(db: sqlite3.Connection, title: str) -> str:
        session_id = f"ngn-{uuid.uuid4().hex[:16]}"
        db.execute("INSERT INTO v2_sessions (id, user_id) VALUES (?, 'harness')", (session_id,))
        db.execute("INSERT INTO harness_sessions (id, title) VALUES (?, ?)", (session_id, title[:80]))
        return session_id

    def capacity(self, db: sqlite3.Connection) -> None:
        if (
            db.execute("SELECT COUNT(*) FROM ngn_web_inbox WHERE status IN ('queued', 'running')").fetchone()[0]
            >= self.inbox_limit
        ):
            raise HTTPException(429, "Message inbox is full. Retry with the same message ID.")

    async def web(self, session_id: str, message_id: str, prompt: str) -> str:
        def admit(db: sqlite3.Connection) -> str:
            self.root(db, session_id)
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
                "SELECT 1 FROM ngn_web_inbox WHERE channel = ? AND message_id = ?", (channel, payload["message_id"])
            ).fetchone():
                return
            self.capacity(db)
            conversation = payload["conversation_id"]
            row = db.execute(
                "SELECT session_id, default_session_id FROM ngn_web_bindings WHERE channel = ? AND conversation_id = ?",
                (channel, conversation),
            ).fetchone()
            if row:
                target, default = row
            else:
                target = default = self.new_root(db, f"{channel}: {conversation}")
            ack = ""
            if command is not None:
                argument = command.arguments.strip()
                if command.name == "sessions":
                    roots = db.execute(
                        "SELECT id, title FROM harness_sessions ORDER BY rowid DESC LIMIT 100"
                    ).fetchall()
                    lines = ["Sessions:"]
                    units = 10
                    for id, title in roots:
                        line = f"{id} — {title or 'New session'}"
                        length = len(line.encode("utf-16-le")) // 2 + 1
                        if units + length > 3500:
                            lines.append("More sessions are available in the web UI.")
                            break
                        lines.append(line)
                        units += length
                    ack = "\n".join(lines)
                elif command.name in {"session", "new"}:
                    if command.name == "new" or argument == "new":
                        target = self.new_root(db, argument if command.name == "new" else f"{channel}: {conversation}")
                    elif argument:
                        candidate = main if argument == "main" else default if argument == "default" else argument
                        try:
                            target = self.root(db, candidate)
                        except HTTPException:
                            ack = "Session not found. Use /sessions to list available session IDs."
                    if not ack:
                        ack = f"Session: {target}"
                else:
                    ack = "Unknown session command. Use /sessions, /session [ID|main|default|new], or /new [title]."
            db.execute(
                "INSERT INTO ngn_web_bindings VALUES (?, ?, ?, ?) ON CONFLICT(channel, conversation_id) "
                "DO UPDATE SET session_id = excluded.session_id",
                (channel, conversation, target, default),
            )
            db.execute(
                "INSERT INTO ngn_web_inbox(session_id, channel, message_id, prompt, conversation_id, thread_id, "
                "reply_to, acknowledgement) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    target,
                    channel,
                    payload["message_id"],
                    _INBOUND_PREFIX + envelope,
                    conversation,
                    payload["thread_id"],
                    payload["reply_to"],
                    ack,
                ),
            )

        await self._transaction(admit)

    @staticmethod
    def eligible(web_only: bool, available_channels: tuple[str, ...] | None) -> tuple[str, tuple[str | bool, ...]]:
        predicate = "status = 'queued' AND (? = 0 OR channel = '')"
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
            row = db.execute(
                "SELECT id, session_id, channel, message_id, prompt, conversation_id, thread_id, reply_to, "
                f"acknowledgement FROM ngn_web_inbox WHERE {predicate} ORDER BY id LIMIT 1",
                parameters,
            ).fetchone()
            if row is None:
                return None
            db.execute("UPDATE ngn_web_inbox SET status = 'running' WHERE id = ?", (row[0],))
            return Work(*row)

        return await self._transaction(claim)

    async def has_pending(self, *, web_only: bool = False, available_channels: tuple[str, ...] | None = None) -> bool:
        predicate, parameters = self.eligible(web_only, available_channels)
        return await self._transaction(
            lambda db: (
                db.execute(
                    f"SELECT 1 FROM ngn_web_inbox WHERE {predicate} LIMIT 1",
                    parameters,
                ).fetchone()
                is not None
            )
        )

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
