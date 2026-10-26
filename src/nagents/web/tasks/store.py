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
from dataclasses import asdict
from datetime import UTC
from datetime import datetime
from typing import TYPE_CHECKING

from fastapi import HTTPException

from nagents.channels.store import InboxStore
from nagents.harness.agents.subagents import TaskInfo
from nagents.harness.session_identity import initialize_identity_schema
from nagents.harness.session_identity import validate_session_id

from ..routing import RoutingStore

if TYPE_CHECKING:
    from pathlib import Path


MAX_ACTIVATION_BYTES = 2 * 1024 * 1024
MAX_ACTIVATION_RECORDS = 1024


def _preview(value: str, maximum: int) -> str:
    return value.encode("utf-8", errors="replace")[:maximum].decode("utf-8", errors="ignore")


def _compact_activations(records: list[dict[str, object]]) -> tuple[list[dict[str, object]], int]:
    """Bound presentation details before omitting any host identity or state."""
    projected: list[dict[str, object]] = []
    for prompt, result, error in ((512, 2048, 1024), (256, 512, 256), (64, 128, 64), (0, 0, 0)):
        projected = []
        for record in records:
            item = dict(record)
            shortened = False
            for field, limit in (("prompt", prompt), ("result", result), ("error", error)):
                original = str(record.get(field, ""))
                item[field] = _preview(original, limit)
                shortened |= item[field] != original
            if shortened:
                item["details_truncated"] = True
            projected.append(item)
        if len(json.dumps(projected).encode("ascii")) <= MAX_ACTIVATION_BYTES:
            return projected, 0
    # Exceptional histories/metadata can exceed even the detail-free budget.
    # Keep the newest authoritative records and report every omitted record.
    kept: list[dict[str, object]] = []
    size = 2
    for item in reversed(projected):
        encoded = len(json.dumps(item).encode("ascii")) + (2 if kept else 0)
        if size + encoded <= MAX_ACTIVATION_BYTES:
            kept.append(item)
            size += encoded
    return list(reversed(kept)), len(projected) - len(kept)


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
                initialize_identity_schema(db)
                db.execute(
                    "CREATE TABLE IF NOT EXISTS ngn_web_agent_sessions ("
                    "root TEXT NOT NULL, session_id TEXT NOT NULL, storage_id TEXT NOT NULL, "
                    "epoch TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(root, session_id))"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS ngn_web_agent_inputs ("
                    "message_id TEXT PRIMARY KEY, root TEXT NOT NULL, target TEXT NOT NULL, "
                    "text TEXT NOT NULL, text_hash TEXT NOT NULL DEFAULT '', session_id TEXT NOT NULL, storage_id TEXT NOT NULL DEFAULT '', "
                    "epoch TEXT NOT NULL, owner_run_id TEXT NOT NULL DEFAULT '', activation INTEGER NOT NULL DEFAULT -1, status TEXT NOT NULL DEFAULT 'queued', revision INTEGER NOT NULL DEFAULT 1, "
                    "queued_at TEXT NOT NULL, picked_up_at TEXT NOT NULL DEFAULT '', "
                    "history_id INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '')"
                )
                db.execute(
                    "CREATE INDEX IF NOT EXISTS ngn_web_agent_inputs_root "
                    "ON ngn_web_agent_inputs(root, session_id, queued_at)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS ngn_web_agent_activations ("
                    "sequence INTEGER PRIMARY KEY AUTOINCREMENT, root TEXT NOT NULL, session_id TEXT NOT NULL, "
                    "activation INTEGER NOT NULL, epoch TEXT NOT NULL, started TEXT NOT NULL DEFAULT '{}', "
                    "completed TEXT NOT NULL DEFAULT '{}', UNIQUE(root, session_id, activation))"
                )
                db.execute(
                    "UPDATE ngn_web_agent_inputs SET status = 'error', revision = revision + 1, "
                    "reason = 'Target unavailable after server restart; this message was not replayed.' "
                    "WHERE status = 'queued' AND epoch != ?",
                    (self.epoch,),
                )
                db.execute("DROP TRIGGER IF EXISTS ngn_web_agent_sessions_root_deleted")
                db.execute(
                    "CREATE TRIGGER ngn_web_agent_sessions_root_deleted BEFORE DELETE ON harness_sessions BEGIN "
                    "UPDATE ngn_web_agent_inputs SET status = 'error', revision = revision + 1, "
                    "reason = 'Root session removed.' WHERE root = OLD.id AND status = 'queued'; END"
                )
                db.execute("DROP TRIGGER IF EXISTS ngn_web_agent_sessions_root_purged")
                db.execute(
                    "CREATE TRIGGER ngn_web_agent_sessions_root_purged BEFORE DELETE ON v2_sessions BEGIN "
                    "DELETE FROM v2_messages WHERE session_id IN (SELECT storage_id FROM ngn_web_agent_sessions WHERE root = OLD.id); "
                    "DELETE FROM v2_sessions WHERE id IN (SELECT storage_id FROM ngn_web_agent_sessions WHERE root = OLD.id AND storage_id != OLD.id); "
                    "DELETE FROM ngn_web_agent_sessions WHERE root = OLD.id; "
                    "DELETE FROM ngn_web_agent_activations WHERE root = OLD.id; "
                    "UPDATE ngn_web_agent_inputs SET text = '', status = CASE WHEN status = 'queued' THEN 'error' ELSE status END, "
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
            "root_session_id": row["root"],
            "session_id": row["session_id"],
            "status": row["status"],
            "revision": row["revision"],
            "activation": row["activation"],
            "owner_run_id": row["owner_run_id"],
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
        row = db.execute("SELECT * FROM ngn_web_agent_inputs WHERE message_id = ?", (identifier,)).fetchone()
        if row is None:
            raise HTTPException(404, "Task message not found.")
        assert isinstance(row, sqlite3.Row)
        return row

    async def duplicate(self, root: str, target: str, identifier: str, text: str) -> dict[str, object]:
        await self.initialize()

        def read(db: sqlite3.Connection) -> dict[str, object]:
            self.root(db, root)
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM ngn_web_agent_inputs WHERE message_id = ?", (identifier,)).fetchone()
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

    async def insert(
        self, root: str, target: str, identifier: str, text: str, session_id: str = ""
    ) -> dict[str, object]:
        def save(db: sqlite3.Connection) -> dict[str, object]:
            self.root(db, root)
            count, size = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(length(text)), 0) FROM ngn_web_agent_inputs WHERE status = 'queued'"
            ).fetchone()
            if count >= 128 or size + len(text) > 512000:
                raise HTTPException(429, "Task message queue is full. Retry with the same message ID.")
            db.execute(
                "UPDATE ngn_web_agent_inputs SET text = '' WHERE root = ? AND status != 'queued' AND rowid NOT IN "
                "(SELECT rowid FROM ngn_web_agent_inputs WHERE root = ? AND status != 'queued' ORDER BY rowid DESC LIMIT 1023)",
                (root, root),
            )
            db.execute(
                "INSERT INTO ngn_web_agent_inputs(message_id, root, target, text, text_hash, session_id, epoch, queued_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    identifier,
                    root,
                    target,
                    text,
                    hashlib.sha256(text.encode()).hexdigest(),
                    session_id,
                    self.epoch,
                    now(),
                ),
            )
            return self.receipt(self.row(db, identifier))

        return await self.db._transaction(save)

    async def bind(self, identifier: str, task: dict[str, object], storage_id: str) -> dict[str, object]:
        def save(db: sqlite3.Connection) -> dict[str, object]:
            row = self.row(db, identifier)
            self.root(db, str(row["root"]))
            if task.get("root_session_id") != row["root"] or not storage_id:
                raise RuntimeError("Child input belongs to a different root")
            if row["storage_id"] and row["storage_id"] != storage_id:
                raise RuntimeError("Child input cannot change storage conversation")
            if row["session_id"] and row["session_id"] != task["session_id"]:
                raise RuntimeError("Task input cannot change recipient")
            activation = task.get("activation", 0)
            if row["activation"] >= 0 and row["activation"] != activation:
                raise RuntimeError("Task input cannot change activation")
            owner_run_id = task.get("receipt_run_id", "")
            if row["owner_run_id"] and row["owner_run_id"] != owner_run_id:
                raise RuntimeError("Task input cannot change execution owner")
            db.execute(
                "UPDATE ngn_web_agent_inputs SET session_id = ?, storage_id = ?, activation = ?, owner_run_id = ? WHERE message_id = ? AND epoch = ?",
                (task["session_id"], storage_id, activation, owner_run_id, identifier, self.epoch),
            )
            return self.receipt(self.row(db, identifier))

        return await self.db._transaction(save)

    async def fail_task(self, root: str, session_id: str, activation: int) -> list[dict[str, object]]:
        """Terminalize only this exact activation; no UI page or admission lock."""

        def save(db: sqlite3.Connection) -> list[dict[str, object]]:
            db.row_factory = sqlite3.Row
            identifiers = [
                str(row[0])
                for row in db.execute(
                    "SELECT message_id FROM ngn_web_agent_inputs WHERE root = ? AND session_id = ? "
                    "AND activation = ? AND epoch = ? AND status = 'queued'",
                    (root, session_id, activation, self.epoch),
                )
            ]
            db.execute(
                "UPDATE ngn_web_agent_inputs SET status = 'error', revision = revision + 1, "
                "reason = 'The agent ended before reading this message.' "
                "WHERE root = ? AND session_id = ? AND activation = ? AND epoch = ? AND status = 'queued'",
                (root, session_id, activation, self.epoch),
            )
            return [self.receipt(self.row(db, identifier)) for identifier in identifiers]

        return await self.db._transaction(save)

    async def fail(self, identifier: str, reason: str) -> dict[str, object]:
        def save(db: sqlite3.Connection) -> dict[str, object]:
            db.execute(
                "UPDATE ngn_web_agent_inputs SET status = 'error', revision = revision + 1, reason = ? "
                "WHERE message_id = ? AND status = 'queued' AND epoch = ?",
                (reason[:500], identifier, self.epoch),
            )
            return self.receipt(self.row(db, identifier))

        return await self.db._transaction(save)

    async def receipts(self, root: str, session_id: str) -> list[dict[str, object]]:
        def read(db: sqlite3.Connection) -> list[dict[str, object]]:
            self.root(db, root)
            db.row_factory = sqlite3.Row
            return [
                self.receipt(row)
                for row in db.execute(
                    "SELECT * FROM ngn_web_agent_inputs WHERE root = ? AND session_id = ? ORDER BY queued_at DESC LIMIT 64",
                    (root, session_id),
                )
            ]

        return list(reversed(await self.db._transaction(read)))

    async def save(self, root: str, data: dict[str, object], storage_id: str) -> None:
        await self.initialize()
        if data.get("root_session_id") != root or not storage_id:
            raise ValueError("Child evidence must belong to its root and private storage conversation")
        validate_session_id(str(data["session_id"]))
        public = {key: value for key, value in data.items() if not key.startswith("_")}

        def save(db: sqlite3.Connection) -> None:
            self.root(db, root)
            previous = db.execute(
                "SELECT storage_id FROM ngn_web_agent_sessions WHERE root = ? AND session_id = ?",
                (root, data["session_id"]),
            ).fetchone()
            if previous is not None and previous[0] != storage_id:
                raise RuntimeError("Child session cannot change its private storage conversation")
            db.execute(
                "INSERT INTO ngn_web_agent_sessions VALUES (?, ?, ?, ?, ?) ON CONFLICT(root, session_id) DO UPDATE SET "
                "storage_id = excluded.storage_id, epoch = excluded.epoch, data = excluded.data",
                (root, data["session_id"], storage_id, self.epoch, json.dumps(public)),
            )

        await self.db._transaction(save)

    async def tasks(self, root: str) -> list[dict[str, object]]:
        await self.initialize()

        def read(db: sqlite3.Connection) -> list[dict[str, object]]:
            self.root(db, root)
            result: list[dict[str, object]] = []
            for data, epoch in db.execute("SELECT data, epoch FROM ngn_web_agent_sessions WHERE root = ?", (root,)):
                task: dict[str, object] = json.loads(data)
                if epoch != self.epoch:
                    task.update(can_message=False, unavailable_reason="The server restarted; this task is read-only.")
                    if task.get("status") == "running":
                        task.update(status="cancelled", phase="cancelled", error="Interrupted by server restart.")
                result.append(task)
            return result

        return await self.db._transaction(read)

    async def record_activation(self, root: str, record: dict[str, object]) -> None:
        """Freeze host lifecycle evidence independently for every accepted activation."""
        kind = record.get("event")
        if kind not in {"task_started", "task_completed"}:
            return
        if record.get("root_session_id") != root:
            raise ValueError("Activation evidence belongs to another root")
        session_id, activation = record.get("session_id"), record.get("activation")
        if not isinstance(session_id, str) or type(activation) is not int or activation < 0:
            raise ValueError("Activation evidence needs its short session ID and activation")
        validate_session_id(session_id)
        await self.initialize()
        fields = set(TaskInfo.__dataclass_fields__) - {
            name for name in TaskInfo.__dataclass_fields__ if name.startswith("_")
        }
        fields.update({"followup", "timestamp"})
        public = {key: value for key, value in record.items() if key in fields}
        column = "started" if kind == "task_started" else "completed"

        def save(db: sqlite3.Connection) -> None:
            self.root(db, root)
            db.execute(
                "INSERT INTO ngn_web_agent_activations(root, session_id, activation, epoch, " + column + ") "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(root, session_id, activation) DO UPDATE SET "
                + column
                + " = CASE WHEN "
                + column
                + " = '{}' THEN excluded."
                + column
                + " ELSE "
                + column
                + " END",
                (root, session_id, activation, self.epoch, json.dumps(public)),
            )

        await self.db._transaction(save)

    async def activations(self, root: str) -> list[dict[str, object]]:
        """Compact visible host evidence; execution never derives from this view."""
        records, _ = await self.activation_projection(root)
        return records

    async def activation_projection(
        self, root: str, *, history_ids: frozenset[str] | None = None, parent_session_id: str = ""
    ) -> tuple[list[dict[str, object]], int]:
        """Project visible call anchors and latest unanchored activations, root-scoped.

        Explicit history IDs select one conversation's currently loaded page.
        The team index instead follows each actor's persisted compaction boundary.
        Full started/completed records are never changed by this projection.
        """
        await self.initialize()

        def read(db: sqlite3.Connection) -> tuple[list[dict[str, object]], int]:
            self.root(db, root)
            metadata = db.execute(
                "SELECT sequence, session_id, activation, "
                "COALESCE(json_extract(started, '$.parent_session_id'), json_extract(completed, '$.parent_session_id'), ''), "
                "COALESCE(json_extract(started, '$.activation_history_id'), '') "
                "FROM ngn_web_agent_activations WHERE root = ? ORDER BY sequence",
                (root,),
            ).fetchall()
            latest = {str(row[1]): int(row[2]) for row in sorted(metadata, key=lambda row: row[2])}
            visible: set[tuple[str, str]] = set()
            if history_ids is None:
                visible = {
                    (str(actor), str(identifier))
                    for actor, identifier in db.execute(
                        "SELECT CASE WHEN m.session_id = ? THEN '' ELSE a.session_id END, m.id "
                        "FROM v2_messages m JOIN v2_sessions v ON v.id = m.session_id "
                        "LEFT JOIN ngn_web_agent_sessions a ON a.root = ? AND a.storage_id = m.session_id "
                        "WHERE m.id IN (SELECT CAST(value AS INTEGER) FROM json_each(?)) "
                        "AND (m.session_id = ? OR a.session_id IS NOT NULL) "
                        "AND m.id >= COALESCE(v.compacted_at_message_id, 0)",
                        (root, root, json.dumps(list({str(row[4]) for row in metadata if row[4]})), root),
                    )
                }
            selected = [
                int(sequence)
                for sequence, alias, activation, parent, anchor in metadata
                if (history_ids is None or parent == parent_session_id)
                and (
                    (
                        bool(anchor)
                        and ((str(parent), str(anchor)) in visible if history_ids is None else anchor in history_ids)
                    )
                    or (not anchor and activation == latest[str(alias)])
                )
            ]
            omitted = max(0, len(selected) - MAX_ACTIVATION_RECORDS)
            selected = selected[-MAX_ACTIVATION_RECORDS:]
            records: list[dict[str, object]] = []
            for session_id, activation, epoch, started, completed in db.execute(
                "SELECT session_id, activation, epoch, started, completed FROM ngn_web_agent_activations "
                "WHERE root = ? AND sequence IN (SELECT value FROM json_each(?)) ORDER BY sequence",
                (root, json.dumps(selected)),
            ):
                initial: dict[str, object] = json.loads(started)
                terminal: dict[str, object] = json.loads(completed)
                values = {**initial, **terminal}
                info = TaskInfo(session_id, str(values.get("title", "")), root_session_id=root, activation=activation)
                public = {key: value for key, value in asdict(info).items() if not key.startswith("_")}
                public.update({key: value for key, value in values.items() if key in public})
                public.update(
                    followups=values.get("followup", public["followups"]),
                    started_at=initial.get("timestamp", 0),
                    phase=values.get("status", "running"),
                    can_message=False,
                    can_start_children=False,
                    unavailable_reason="Historical activation; open its conversation to send a new message.",
                    child_agents=[],
                    child_start_unavailable_reason="Historical activation.",
                )
                if epoch != self.epoch and public["status"] == "running":
                    public.update(status="cancelled", phase="cancelled", error="Interrupted by server restart.")
                records.append(public)
            return records, omitted

        records, omitted = await self.db._transaction(read)
        compact, oversized = await asyncio.to_thread(_compact_activations, records)
        return compact, omitted + oversized

    async def storage_id(self, root: str, session_id: str) -> str:
        """Resolve a private conversation only within an authenticated root."""
        await self.initialize()
        validate_session_id(session_id)

        def read(db: sqlite3.Connection) -> str:
            self.root(db, root)
            row = db.execute(
                "SELECT storage_id FROM ngn_web_agent_sessions WHERE root = ? AND session_id = ?",
                (root, session_id),
            ).fetchone()
            if row is None:
                raise HTTPException(404, "Agent session not found in this conversation.")
            allocator = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'nagents_agent_sessions'"
            ).fetchone()
            if (
                allocator is None
                or db.execute(
                    "SELECT 1 FROM nagents_agent_sessions WHERE public_id = ? AND root_session_id = ? AND storage_id = ?",
                    (session_id, root, row[0]),
                ).fetchone()
                is None
            ):
                raise HTTPException(409, "Agent history ownership is inconsistent. History was not read.")
            return str(row[0])

        return await self.db._transaction(read)
