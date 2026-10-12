"""Root-scoped task evidence and idempotent human-input receipts.

This journal is evidence, never a job queue. A new process terminalizes unread
inputs; only its exact retained runtime can accept a new activation. Receipt
tombstones remain after root deletion so an old identifier cannot run twice.
Admission prunes terminal receipt bodies to the latest 1024 per root; older receipt
identities and SHA-256 text fingerprints preserve deduplication. The actual
conversation follows normal session retention. Permanent deletion scrubs bodies.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from datetime import UTC
from datetime import datetime
from typing import TYPE_CHECKING

from fastapi import HTTPException

from nagents.channels.store import InboxStore

from .routing import RoutingStore

if TYPE_CHECKING:
    from pathlib import Path


def now() -> str:
    return datetime.now(UTC).isoformat()


class TaskStore:
    def __init__(self, path: Path, epoch: str) -> None:
        self.db = InboxStore(path, "task-evidence", 1000)
        self.epoch = epoch
        self.ready = False
        self.lock = asyncio.Lock()

    async def initialize(self) -> None:
        if self.ready:
            return
        async with self.lock:
            if self.ready:
                return

            def create(db: sqlite3.Connection) -> None:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS ngn_web_tasks ("
                    "root TEXT NOT NULL, task_id TEXT NOT NULL, child TEXT NOT NULL, "
                    "epoch TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(root, task_id))"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS ngn_web_task_inputs ("
                    "message_id TEXT PRIMARY KEY, root TEXT NOT NULL, target TEXT NOT NULL, "
                    "text TEXT NOT NULL, text_hash TEXT NOT NULL DEFAULT '', task_id TEXT NOT NULL, child TEXT NOT NULL DEFAULT '', "
                    "epoch TEXT NOT NULL, activation INTEGER NOT NULL DEFAULT -1, status TEXT NOT NULL DEFAULT 'queued', revision INTEGER NOT NULL DEFAULT 1, "
                    "queued_at TEXT NOT NULL, picked_up_at TEXT NOT NULL DEFAULT '', "
                    "history_id INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '')"
                )
                if "text_hash" not in {str(row[1]) for row in db.execute("PRAGMA table_info(ngn_web_task_inputs)")}:
                    db.execute("ALTER TABLE ngn_web_task_inputs ADD COLUMN text_hash TEXT NOT NULL DEFAULT ''")
                if "activation" not in {str(row[1]) for row in db.execute("PRAGMA table_info(ngn_web_task_inputs)")}:
                    db.execute("ALTER TABLE ngn_web_task_inputs ADD COLUMN activation INTEGER NOT NULL DEFAULT -1")
                for identifier, text in db.execute(
                    "SELECT message_id, text FROM ngn_web_task_inputs WHERE text_hash = ''"
                ):
                    db.execute(
                        "UPDATE ngn_web_task_inputs SET text_hash = ? WHERE message_id = ?",
                        (hashlib.sha256(text.encode()).hexdigest(), identifier),
                    )
                db.execute(
                    "CREATE INDEX IF NOT EXISTS ngn_web_task_inputs_root "
                    "ON ngn_web_task_inputs(root, task_id, queued_at)"
                )
                db.execute(
                    "UPDATE ngn_web_task_inputs SET status = 'error', revision = revision + 1, "
                    "reason = 'Target unavailable after server restart; this message was not replayed.' "
                    "WHERE status = 'queued' AND epoch != ?",
                    (self.epoch,),
                )
                db.execute("DROP TRIGGER IF EXISTS ngn_web_tasks_root_deleted")
                db.execute(
                    "CREATE TRIGGER ngn_web_tasks_root_deleted BEFORE DELETE ON harness_sessions BEGIN "
                    "UPDATE ngn_web_task_inputs SET status = 'error', revision = revision + 1, "
                    "reason = 'Root session removed.' WHERE root = OLD.id AND status = 'queued'; END"
                )
                db.execute(
                    "CREATE TRIGGER IF NOT EXISTS ngn_web_tasks_root_purged BEFORE DELETE ON v2_sessions BEGIN "
                    "DELETE FROM v2_messages WHERE session_id IN (SELECT child FROM ngn_web_tasks WHERE root = OLD.id); "
                    "DELETE FROM v2_sessions WHERE id IN (SELECT child FROM ngn_web_tasks WHERE root = OLD.id AND child != OLD.id); "
                    "DELETE FROM ngn_web_tasks WHERE root = OLD.id; "
                    "UPDATE ngn_web_task_inputs SET text = '', status = CASE WHEN status = 'queued' THEN 'error' ELSE status END, "
                    "revision = revision + 1, reason = 'Root session permanently deleted.' WHERE root = OLD.id; END"
                )

            await self.db._transaction(create)
            self.ready = True

    @staticmethod
    def root(db: sqlite3.Connection, root: str) -> None:
        try:
            RoutingStore.execution_root(db, root)
        except HTTPException:
            raise HTTPException(404, "Session not found in this workspace.") from None

    async def validate(self, root: str) -> None:
        await self.initialize()
        await self.db._transaction(lambda db: self.root(db, root))

    @staticmethod
    def receipt(row: sqlite3.Row) -> dict[str, object]:
        return {
            "message_id": row["message_id"],
            "session_id": row["root"],
            "task_id": row["task_id"],
            "child_session_id": row["child"],
            "status": row["status"],
            "revision": row["revision"],
            "activation": row["activation"],
            "queued_at": row["queued_at"],
            "picked_up_at": row["picked_up_at"],
            "history_id": row["history_id"],
            "reason": row["reason"],
            "text": row["text"],
            "text_pruned": not bool(row["text"]),
        }

    @staticmethod
    def row(db: sqlite3.Connection, identifier: str) -> sqlite3.Row:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM ngn_web_task_inputs WHERE message_id = ?", (identifier,)).fetchone()
        if row is None:
            raise HTTPException(404, "Task message not found.")
        assert isinstance(row, sqlite3.Row)
        return row

    async def duplicate(self, root: str, target: str, identifier: str, text: str) -> dict[str, object]:
        await self.initialize()

        def read(db: sqlite3.Connection) -> dict[str, object]:
            self.root(db, root)
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM ngn_web_task_inputs WHERE message_id = ?", (identifier,)).fetchone()
            if row is None:
                return {}
            if (row["root"], row["target"], row["text_hash"]) != (
                root,
                target,
                hashlib.sha256(text.encode()).hexdigest(),
            ):
                raise HTTPException(409, "Message ID already belongs to a different target or text.")
            return self.receipt(row)

        return await self.db._transaction(read)

    async def insert(self, root: str, target: str, identifier: str, text: str, task_id: str = "") -> dict[str, object]:
        def save(db: sqlite3.Connection) -> dict[str, object]:
            self.root(db, root)
            count, size = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(length(text)), 0) FROM ngn_web_task_inputs WHERE status = 'queued'"
            ).fetchone()
            if count >= 128 or size + len(text) > 512000:
                raise HTTPException(429, "Task message queue is full. Retry with the same message ID.")
            db.execute(
                "UPDATE ngn_web_task_inputs SET text = '' WHERE root = ? AND status != 'queued' AND rowid NOT IN "
                "(SELECT rowid FROM ngn_web_task_inputs WHERE root = ? AND status != 'queued' ORDER BY rowid DESC LIMIT 1023)",
                (root, root),
            )
            db.execute(
                "INSERT INTO ngn_web_task_inputs(message_id, root, target, text, text_hash, task_id, epoch, queued_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (identifier, root, target, text, hashlib.sha256(text.encode()).hexdigest(), task_id, self.epoch, now()),
            )
            return self.receipt(self.row(db, identifier))

        return await self.db._transaction(save)

    async def bind(self, identifier: str, task: dict[str, object]) -> dict[str, object]:
        def save(db: sqlite3.Connection) -> dict[str, object]:
            row = self.row(db, identifier)
            self.root(db, str(row["root"]))
            if row["task_id"] and row["task_id"] != task["id"]:
                raise RuntimeError("Task input cannot change recipient")
            activation = task.get("activation", 0)
            if row["activation"] >= 0 and row["activation"] != activation:
                raise RuntimeError("Task input cannot change activation")
            db.execute(
                "UPDATE ngn_web_task_inputs SET task_id = ?, child = ?, activation = ? WHERE message_id = ? AND epoch = ?",
                (task["id"], task["child_session_id"], activation, identifier, self.epoch),
            )
            return self.receipt(self.row(db, identifier))

        return await self.db._transaction(save)

    async def fail_task(self, root: str, task_id: str, activation: int) -> list[dict[str, object]]:
        """Terminalize only this exact activation; no UI page or admission lock."""

        def save(db: sqlite3.Connection) -> list[dict[str, object]]:
            db.row_factory = sqlite3.Row
            identifiers = [
                str(row[0])
                for row in db.execute(
                    "SELECT message_id FROM ngn_web_task_inputs WHERE root = ? AND task_id = ? "
                    "AND activation = ? AND epoch = ? AND status = 'queued'",
                    (root, task_id, activation, self.epoch),
                )
            ]
            db.execute(
                "UPDATE ngn_web_task_inputs SET status = 'error', revision = revision + 1, "
                "reason = 'The agent ended before reading this message.' "
                "WHERE root = ? AND task_id = ? AND activation = ? AND epoch = ? AND status = 'queued'",
                (root, task_id, activation, self.epoch),
            )
            return [self.receipt(self.row(db, identifier)) for identifier in identifiers]

        return await self.db._transaction(save)

    async def fail(self, identifier: str, reason: str) -> dict[str, object]:
        def save(db: sqlite3.Connection) -> dict[str, object]:
            db.execute(
                "UPDATE ngn_web_task_inputs SET status = 'error', revision = revision + 1, reason = ? "
                "WHERE message_id = ? AND status = 'queued' AND epoch = ?",
                (reason[:500], identifier, self.epoch),
            )
            return self.receipt(self.row(db, identifier))

        return await self.db._transaction(save)

    async def receipts(self, root: str, task_id: str) -> list[dict[str, object]]:
        def read(db: sqlite3.Connection) -> list[dict[str, object]]:
            self.root(db, root)
            db.row_factory = sqlite3.Row
            return [
                self.receipt(row)
                for row in db.execute(
                    "SELECT * FROM ngn_web_task_inputs WHERE root = ? AND task_id = ? ORDER BY queued_at DESC LIMIT 64",
                    (root, task_id),
                )
            ]

        return list(reversed(await self.db._transaction(read)))

    async def save(self, root: str, data: dict[str, object]) -> None:
        await self.initialize()

        def save(db: sqlite3.Connection) -> None:
            self.root(db, root)
            db.execute(
                "INSERT INTO ngn_web_tasks VALUES (?, ?, ?, ?, ?) ON CONFLICT(root, task_id) DO UPDATE SET "
                "child = excluded.child, epoch = excluded.epoch, data = excluded.data",
                (root, data["id"], data["child_session_id"], self.epoch, json.dumps(data)),
            )

        await self.db._transaction(save)

    async def tasks(self, root: str) -> list[dict[str, object]]:
        await self.initialize()

        def read(db: sqlite3.Connection) -> list[dict[str, object]]:
            self.root(db, root)
            result: list[dict[str, object]] = []
            for data, epoch in db.execute("SELECT data, epoch FROM ngn_web_tasks WHERE root = ?", (root,)):
                task: dict[str, object] = json.loads(data)
                if epoch != self.epoch:
                    task.update(can_message=False, unavailable_reason="The server restarted; this task is read-only.")
                    if task.get("status") == "running":
                        task.update(status="cancelled", phase="cancelled", error="Interrupted by server restart.")
                result.append(task)
            return result

        return await self.db._transaction(read)
