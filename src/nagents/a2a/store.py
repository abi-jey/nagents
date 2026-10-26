"""Owned, versioned A2A tasks and retry reservations in a private SQLite store."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import stat
import time
import uuid
from base64 import urlsafe_b64decode
from base64 import urlsafe_b64encode
from contextlib import asynccontextmanager
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

import aiosqlite
from a2a.helpers import new_task
from a2a.helpers import new_text_message
from a2a.server.cluster.event_stream import TaskEventStream
from a2a.server.cluster.event_stream import VersionedEvent
from a2a.server.cluster.task_store import ConcurrentTaskModificationError
from a2a.server.cluster.task_store import StoredTask
from a2a.server.cluster.task_store import VersionedTaskStore
from a2a.server.cluster.version import TaskVersion
from a2a.types import ListTasksRequest
from a2a.types import ListTasksResponse
from a2a.types import Message
from a2a.types import Role
from a2a.types import SendMessageRequest
from a2a.types import StreamResponse
from a2a.types import Task
from a2a.types import TaskArtifactUpdateEvent
from a2a.types import TaskState
from a2a.types import TaskStatusUpdateEvent
from a2a.utils.errors import InvalidParamsError
from a2a.utils.errors import TaskNotFoundError
from a2a.utils.errors import UnsupportedOperationError
from a2a.utils.proto_utils import to_stream_response

from .server.auth import Principal

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from collections.abc import AsyncIterator
    from pathlib import Path

    from a2a.server.context import ServerCallContext
    from a2a.server.events import Event

    from .server.config import A2AConfig

TERMINAL = frozenset(
    {
        TaskState.TASK_STATE_COMPLETED,
        TaskState.TASK_STATE_FAILED,
        TaskState.TASK_STATE_CANCELED,
        TaskState.TASK_STATE_REJECTED,
    }
)


def principal(context: ServerCallContext) -> Principal:
    user = context.user
    if not isinstance(user, Principal):
        raise PermissionError("An authenticated A2A principal is required")
    return user


def private_file(path: Path) -> None:
    """Create a private regular file through owned, non-writable directories.

    Resolve the operator's path first (including platform /tmp aliases), then
    open every component without following links. This persistence boundary,
    like ngn's existing credential stores, requires POSIX ownership controls.
    """
    if os.name != "posix":
        raise ValueError("Private A2A persistence currently requires POSIX ownership controls")
    target = path.absolute()
    parent = target.parent.resolve()
    directory = os.open(parent.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parent.parts[1:]:
            with suppress(FileExistsError):
                os.mkdir(part, mode=0o700, dir_fd=directory)
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = child
            info = os.fstat(directory)
            sticky = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
            if info.st_uid not in {0, os.getuid()} or (stat.S_IMODE(info.st_mode) & 0o022 and not sticky):
                raise ValueError("A2A data must be in an owned private directory")
        info = os.fstat(directory)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError("A2A data directory must be owned with mode 700")
        descriptor = os.open(
            target.name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory
        )
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise ValueError("A2A data must be an owned regular file with mode 600")
        finally:
            os.close(descriptor)
    finally:
        os.close(directory)


@dataclass(frozen=True)
class MessageClaim:
    task_id: str
    context_id: str
    message_id: str
    duplicate: bool


def stream_event(response: StreamResponse) -> Event:
    """Unwrap SDK protocol messages without importing its optional SQL stack."""
    match response.WhichOneof("payload"):
        case "task":
            return response.task
        case "message":
            return response.message
        case "status_update":
            return response.status_update
        case "artifact_update":
            return response.artifact_update
        case _:
            raise ValueError("Unknown stored A2A event")


class SQLiteTaskStore(VersionedTaskStore):
    def __init__(self, config: A2AConfig) -> None:
        self.config = config
        self.path = config.data_dir.resolve() / "tasks.db"
        self._key = b""

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[aiosqlite.Connection]:
        async with aiosqlite.connect(self.path, timeout=5) as db:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA foreign_keys=ON")
            yield db

    async def initialize(self) -> None:
        private_file(self.path)
        async with self.connection() as db:
            await db.executescript(
                "CREATE TABLE IF NOT EXISTS contexts (id TEXT PRIMARY KEY, owner TEXT NOT NULL, session_id TEXT NOT NULL UNIQUE);"
                "CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, context_id TEXT NOT NULL REFERENCES contexts(id), "
                "owner TEXT NOT NULL, version INTEGER NOT NULL, state INTEGER NOT NULL, updated REAL NOT NULL, body BLOB NOT NULL);"
                "CREATE INDEX IF NOT EXISTS tasks_owner_updated ON tasks(owner, updated DESC, id);"
                "CREATE TABLE IF NOT EXISTS messages (owner TEXT NOT NULL, id TEXT NOT NULL, digest TEXT NOT NULL, "
                "task_id TEXT NOT NULL REFERENCES tasks(id), started INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(owner,id));"
                "CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE, "
                "version INTEGER NOT NULL, body BLOB NOT NULL);"
                "CREATE INDEX IF NOT EXISTS events_task_version ON events(task_id, version);"
                "CREATE TABLE IF NOT EXISTS input_claims (task_id TEXT PRIMARY KEY REFERENCES tasks(id), message_id TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS private_metadata (key TEXT PRIMARY KEY, value BLOB NOT NULL);"
            )
            await db.execute(
                "INSERT OR IGNORE INTO private_metadata VALUES ('pagination_key', ?)", (secrets.token_bytes(32),)
            )
            row = await (await db.execute("SELECT value FROM private_metadata WHERE key='pagination_key'")).fetchone()
            if row is None:
                raise RuntimeError("A2A store initialization failed")
            self._key = bytes(row[0])
            await db.commit()

    @staticmethod
    def _task(body: bytes) -> Task:
        task = Task()
        task.ParseFromString(body)
        return task

    def _bounded(self, task: Task) -> Task:
        result = Task()
        result.CopyFrom(task)
        while len(result.history) > 32:
            del result.history[0]
        maximum = self.config.max_output_bytes + 2 * self.config.max_request_bytes
        while result.ByteSize() > maximum and len(result.history) > 1:
            del result.history[0]
        if result.ByteSize() > maximum:
            raise UnsupportedOperationError("Task output exceeded the configured limit")
        return result

    async def reserve(self, params: SendMessageRequest, user: Principal) -> MessageClaim:
        """Reserve a message, server IDs, and initial task atomically.

        Retained message IDs never execute twice. Storage exhaustion rejects new
        work rather than silently expiring deduplication records. Context IDs
        are server-issued; a caller may reuse only one it already owns.
        """
        message = params.message
        if message.role != Role.ROLE_USER or not message.message_id or len(message.message_id) > 128:
            raise InvalidParamsError("A bounded user message ID is required")
        if not message.parts or any(
            part.WhichOneof("content") != "text" or part.media_type not in {"", "text/plain"} for part in message.parts
        ):
            raise InvalidParamsError("This agent accepts text/plain messages only")
        if not any(part.text.strip() for part in message.parts):
            raise InvalidParamsError("Message text must not be empty")
        if message.ByteSize() > self.config.max_request_bytes:
            raise InvalidParamsError("Message exceeds the configured input limit")
        digest = hashlib.sha256(message.SerializeToString(deterministic=True)).hexdigest()
        owner = user.user_name
        async with self.connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            old = await (
                await db.execute(
                    "SELECT digest, task_id FROM messages WHERE owner=? AND id=?", (owner, message.message_id)
                )
            ).fetchone()
            if old is not None:
                if not hmac.compare_digest(str(old[0]), digest):
                    raise InvalidParamsError("A retained message ID cannot be reused for different content")
                row = await (
                    await db.execute("SELECT context_id FROM tasks WHERE id=? AND owner=?", (old[1], owner))
                ).fetchone()
                if row is None:
                    raise TaskNotFoundError()
                return MessageClaim(str(old[1]), str(row[0]), message.message_id, True)
            if message.task_id:
                row = await (
                    await db.execute(
                        "SELECT context_id, state FROM tasks WHERE id=? AND owner=?", (message.task_id, owner)
                    )
                ).fetchone()
                if row is None:
                    raise TaskNotFoundError()
                context_id = str(row[0])
                if message.context_id and message.context_id != context_id:
                    raise InvalidParamsError("Task and context do not match")
                if int(row[1]) != TaskState.TASK_STATE_INPUT_REQUIRED:
                    raise UnsupportedOperationError("Use a new task in this context; this task is not awaiting input")
                task_id = message.task_id
                pending = await (
                    await db.execute("SELECT message_id FROM input_claims WHERE task_id=?", (task_id,))
                ).fetchone()
                if pending is not None:
                    raise UnsupportedOperationError("An input response is already being admitted for this task")
                await db.execute("INSERT INTO input_claims VALUES (?,?)", (task_id, message.message_id))
            else:
                if message.context_id:
                    row = await (
                        await db.execute("SELECT id FROM contexts WHERE id=? AND owner=?", (message.context_id, owner))
                    ).fetchone()
                    if row is None:
                        raise InvalidParamsError("Unknown context")
                    context_id = str(row[0])
                else:
                    context_id = uuid.uuid4().hex
                    await db.execute(
                        "INSERT INTO contexts VALUES (?,?,?)", (context_id, owner, f"ngn-{uuid.uuid4().hex}")
                    )
                counts = await (
                    await db.execute(
                        "SELECT COUNT(*), SUM(owner=?), SUM(state NOT IN (3,4,5,7)), "
                        "SUM(owner=? AND state NOT IN (3,4,5,7)) FROM tasks",
                        (owner, owner),
                    )
                ).fetchone()
                if counts is None or any(
                    int(counts[index] or 0) >= limit
                    for index, limit in enumerate(
                        (
                            self.config.max_retained_tasks,
                            self.config.max_tasks_per_principal,
                            self.config.max_active_tasks,
                            self.config.max_active_per_principal,
                        )
                    )
                ):
                    raise UnsupportedOperationError("Task quota reached; no work was started")
                task_id = uuid.uuid4().hex
                incoming = Message()
                incoming.CopyFrom(message)
                incoming.task_id, incoming.context_id = task_id, context_id
                task = new_task(
                    task_id=task_id, context_id=context_id, state=TaskState.TASK_STATE_SUBMITTED, history=[incoming]
                )
                task.status.timestamp.GetCurrentTime()
                await db.execute(
                    "INSERT INTO tasks VALUES (?,?,?,?,?,?,?)",
                    (task_id, context_id, owner, 1, task.status.state, time.time(), task.SerializeToString()),
                )
            await db.execute(
                "INSERT INTO messages(owner,id,digest,task_id) VALUES (?,?,?,?)",
                (owner, message.message_id, digest, task_id),
            )
            await db.commit()
            return MessageClaim(task_id, context_id, message.message_id, False)

    async def started(self, claim: MessageClaim, user: Principal) -> None:
        async with self.connection() as db:
            await db.execute(
                "UPDATE messages SET started=1 WHERE owner=? AND id=? AND task_id=?",
                (user.user_name, claim.message_id, claim.task_id),
            )
            await db.commit()

    async def abandon_unstarted(self, claim: MessageClaim, user: Principal) -> None:
        """A failed admission cannot leave an apparently executable reservation."""
        async with self.connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute(
                    "SELECT t.body,t.version FROM tasks t JOIN messages m ON m.task_id=t.id "
                    "WHERE t.id=? AND t.owner=? AND m.owner=? AND m.id=? AND m.started=0 AND t.state=?",
                    (claim.task_id, user.user_name, user.user_name, claim.message_id, TaskState.TASK_STATE_SUBMITTED),
                )
            ).fetchone()
            if row is not None:
                task = self._task(bytes(row[0]))
                task.status.state = TaskState.TASK_STATE_FAILED
                task.status.timestamp.GetCurrentTime()
                task.status.message.CopyFrom(
                    new_text_message(
                        text="Request interrupted before execution started. No pending work was replayed.",
                        task_id=task.id,
                        context_id=task.context_id,
                    )
                )
                await db.execute(
                    "UPDATE tasks SET state=?,version=?,updated=?,body=? WHERE id=?",
                    (task.status.state, int(row[1]) + 1, time.time(), task.SerializeToString(), task.id),
                )
            await db.execute(
                "DELETE FROM input_claims WHERE task_id=? AND message_id=? AND EXISTS "
                "(SELECT 1 FROM messages WHERE owner=? AND id=? AND task_id=? AND started=0)",
                (claim.task_id, claim.message_id, user.user_name, claim.message_id, claim.task_id),
            )
            await db.commit()

    async def session_id(self, context_id: str, user: Principal) -> str:
        async with self.connection() as db:
            row = await (
                await db.execute("SELECT session_id FROM contexts WHERE id=? AND owner=?", (context_id, user.user_name))
            ).fetchone()
        if row is None:
            raise TaskNotFoundError()
        return str(row[0])

    async def get(self, task_id: str, context: ServerCallContext) -> StoredTask | None:
        async with self.connection() as db:
            row = await (
                await db.execute(
                    "SELECT body, version FROM tasks WHERE id=? AND owner=?", (task_id, principal(context).user_name)
                )
            ).fetchone()
        return StoredTask(self._task(bytes(row[0])), TaskVersion(int(row[1]))) if row is not None else None

    async def save(
        self,
        task: Task,
        *,
        event: Event | None,
        prev: Task | None,
        prev_version: TaskVersion,
        context: ServerCallContext,
    ) -> TaskVersion:
        del prev
        owner = principal(context).user_name
        task = self._bounded(task)
        async with self.connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (
                await db.execute("SELECT owner, version, state, context_id FROM tasks WHERE id=?", (task.id,))
            ).fetchone()
            if row is None or str(row[0]) != owner or str(row[3]) != task.context_id:
                raise TaskNotFoundError()
            version = int(row[1])
            cancel = task.status.state == TaskState.TASK_STATE_CANCELED
            if (cancel and int(row[2]) in TERMINAL) or (not cancel and TaskVersion(version) != prev_version):
                raise ConcurrentTaskModificationError(task.id)
            version += 1
            updated = task.status.timestamp.seconds + task.status.timestamp.nanos / 1_000_000_000
            await db.execute(
                "UPDATE tasks SET version=?,state=?,updated=?,body=? WHERE id=? AND owner=?",
                (version, task.status.state, updated, task.SerializeToString(), task.id, owner),
            )
            if task.status.state != TaskState.TASK_STATE_INPUT_REQUIRED:
                await db.execute("DELETE FROM input_claims WHERE task_id=?", (task.id,))
            if isinstance(event, Task | Message | TaskStatusUpdateEvent | TaskArtifactUpdateEvent):
                wire = to_stream_response(event).SerializeToString()
                await db.execute("INSERT INTO events(task_id,version,body) VALUES (?,?,?)", (task.id, version, wire))
                await db.execute(
                    "DELETE FROM events WHERE task_id=? AND seq NOT IN (SELECT seq FROM events WHERE task_id=? ORDER BY seq DESC LIMIT ?)",
                    (task.id, task.id, self.config.max_events_per_task),
                )
                # Keep both count and byte bounds. The current Task is always a
                # complete snapshot; subscriptions do not need expired deltas.
                rows = await (
                    await db.execute(
                        "SELECT seq,length(body) FROM events WHERE task_id=? ORDER BY seq DESC", (task.id,)
                    )
                ).fetchall()
                size = 0
                for retained in rows:
                    size += int(retained[1])
                    if size > 2 * 1024 * 1024:
                        await db.execute("DELETE FROM events WHERE task_id=? AND seq<=?", (task.id, int(retained[0])))
                        break
            await db.commit()
        return TaskVersion(version)

    def _page_token(self, owner: str, fingerprint: str, offset: int) -> str:
        body = json.dumps([owner, fingerprint, offset], separators=(",", ":")).encode()
        return urlsafe_b64encode(body + hmac.digest(self._key, body, "sha256")).decode()

    async def list(self, params: ListTasksRequest, context: ServerCallContext) -> ListTasksResponse:
        owner = principal(context).user_name
        clean = ListTasksRequest()
        clean.CopyFrom(params)
        clean.page_token = ""
        fingerprint = hashlib.sha256(clean.SerializeToString(deterministic=True)).hexdigest()
        offset = 0
        if params.page_token:
            try:
                if len(params.page_token) > 2048:
                    raise ValueError()
                decoded = urlsafe_b64decode(params.page_token)
                body, signature = decoded[:-32], decoded[-32:]
                if not hmac.compare_digest(hmac.digest(self._key, body, "sha256"), signature):
                    raise ValueError()
                token = json.loads(body)
                if (
                    not isinstance(token, list)
                    or len(token) != 3
                    or token[:2] != [owner, fingerprint]
                    or type(token[2]) is not int
                    or token[2] < 0
                ):
                    raise ValueError()
                offset = token[2]
            except (ValueError, UnicodeError):
                raise InvalidParamsError("Invalid page token") from None
        page_size = params.page_size if params.HasField("page_size") else 50
        if not 1 <= page_size <= 100:
            raise InvalidParamsError("Page size must be between 1 and 100")
        where, values = ["owner=?"], [owner]
        if params.context_id:
            where.append("context_id=?")
            values.append(params.context_id)
        if params.status:
            where.append("state=?")
            values.append(str(params.status))
        if params.HasField("status_timestamp_after"):
            where.append("updated>=?")
            values.append(
                str(params.status_timestamp_after.seconds + params.status_timestamp_after.nanos / 1_000_000_000)
            )
        clause = " AND ".join(where)
        async with self.connection() as db:
            count = await (await db.execute(f"SELECT COUNT(*) FROM tasks WHERE {clause}", values)).fetchone()
            rows = list(
                await (
                    await db.execute(
                        f"SELECT body FROM tasks WHERE {clause} ORDER BY updated DESC,id LIMIT ? OFFSET ?",
                        [*values, page_size, offset],
                    )
                ).fetchall()
            )
        total = int(count[0]) if count is not None else 0
        next_page = self._page_token(owner, fingerprint, offset + len(rows)) if offset + len(rows) < total else ""
        return ListTasksResponse(
            tasks=[self._task(bytes(row[0])) for row in rows],
            next_page_token=next_page,
            page_size=page_size,
            total_size=total,
        )

    async def delete(self, task_id: str, context: ServerCallContext) -> None:
        del task_id, context
        # Deleting the ledger would permit replay of old message IDs. Retention
        # cleanup is deliberately a separate, explicit operator operation.
        raise UnsupportedOperationError("Task deletion is not exposed by this service")

    async def reconcile_restart(self) -> int:
        """Retain evidence of interrupted work; never reconstruct an execution."""
        count = 0
        async with self.connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            rows = await (await db.execute("SELECT id,body,version FROM tasks WHERE state NOT IN (3,4,5,7)")).fetchall()
            for row in rows:
                task = self._task(bytes(row[1]))
                task.status.state = TaskState.TASK_STATE_FAILED
                task.status.timestamp.GetCurrentTime()
                task.status.message.CopyFrom(
                    new_text_message(
                        text="Execution interrupted by server restart. Pending operations were not replayed; inspect prior outcomes before sending new work.",
                        task_id=task.id,
                        context_id=task.context_id,
                    )
                )
                await db.execute(
                    "UPDATE tasks SET state=?,version=?,updated=?,body=? WHERE id=?",
                    (task.status.state, int(row[2]) + 1, time.time(), task.SerializeToString(), task.id),
                )
                count += 1
            await db.commit()
        return count


class SQLiteEventStream(TaskEventStream):
    """A bounded persistent tail. Access is checked by the SDK's task handler."""

    def __init__(self, store: SQLiteTaskStore) -> None:
        self.store = store
        self._changed: dict[str, asyncio.Event] = {}

    async def publish(self, task_id: str, event: VersionedEvent) -> None:
        del event  # The store committed the event with its Task atomically.
        if task_id in self._changed:
            self._changed[task_id].set()

    async def subscribe(self, task_id: str, *, after: TaskVersion) -> AsyncGenerator[VersionedEvent, None]:
        changed = self._changed.setdefault(task_id, asyncio.Event())
        while True:
            changed.clear()
            async with self.store.connection() as db:
                rows = await (
                    await db.execute("SELECT body,version FROM events WHERE task_id=? ORDER BY seq", (task_id,))
                ).fetchall()
            for row in rows:
                version = TaskVersion(int(row[1]))
                if version.is_after(after):
                    response = StreamResponse()
                    response.ParseFromString(bytes(row[0]))
                    yield VersionedEvent(stream_event(response), version)
                    after = version
            with suppress(TimeoutError):
                await asyncio.wait_for(changed.wait(), timeout=0.5)

    async def destroy(self, task_id: str) -> None:
        changed = self._changed.pop(task_id, None)
        if changed is not None:
            changed.set()
