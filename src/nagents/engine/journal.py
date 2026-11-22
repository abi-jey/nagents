"""Durable protocol projections and bounded live deltas for an existing WebState."""

from __future__ import annotations

import asyncio
import hashlib
import math
import sqlite3
from collections import deque
from contextlib import closing
from contextlib import suppress
from dataclasses import dataclass
from dataclasses import field
from time import time_ns
from typing import TYPE_CHECKING
from typing import cast

from fastapi import HTTPException
from pydantic import JsonValue
from pydantic import TypeAdapter
from pydantic import ValidationError
from starlette.websockets import WebSocketDisconnect

from nagents.protocol import EVENTS_SUBPROTOCOL
from nagents.protocol import PROTOCOL_VERSION
from nagents.protocol.events import ApprovalRequested
from nagents.protocol.events import ApprovalResolved
from nagents.protocol.events import EngineConnected
from nagents.protocol.events import EngineEvent
from nagents.protocol.events import EventRecord
from nagents.protocol.events import History
from nagents.protocol.events import Notice
from nagents.protocol.events import RunFinished
from nagents.protocol.events import RunStarted
from nagents.protocol.events import TaskFinished
from nagents.protocol.events import TaskStarted
from nagents.protocol.events import TextCompleted
from nagents.protocol.events import TextDelta
from nagents.protocol.events import ToolApprovalTiming
from nagents.protocol.events import ToolCompleted
from nagents.protocol.events import ToolStarted
from nagents.protocol.models import ToolCall
from nagents.protocol.models import ToolResult
from nagents.web._async import finish_on_cancel
from nagents.web.http.routing import RoutingStore

from . import archives
from .task_projection import activation_in
from .task_projection import finish_in
from .task_projection import root_for_child_in
from .task_projection import running_descendants_in
from .task_projection import start_in
from .task_projection import task_run_in

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Literal

    from fastapi import WebSocket
    from starlette.types import Message

    from nagents.protocol.models import EngineInfo
    from nagents.protocol.models import RunStatus
    from nagents.web.execution.service import Run

    from .task_projection import ChildActivation

_PAGE_SIZE = 128
_MAX_LISTENERS = 64
_LIVE_ENTRIES = 256
_LIVE_BYTES = 256 * 1024
_EVENT_BYTES = 256 * 1024
_PAGE_BYTES = 2 * 1024 * 1024
_SEND_TIMEOUT = 10
_POLL_INTERVAL = 0.25
_EVENT = TypeAdapter(EngineEvent)
_JSON = TypeAdapter(JsonValue)
_SCHEMA_VERSION = cast("Literal[1]", PROTOCOL_VERSION)


def _now_ms() -> int:
    return time_ns() // 1_000_000


def _string(value: object) -> str:
    return value if isinstance(value, str) else ""


def _elapsed(value: object, scale: float = 1.0) -> int:
    if isinstance(value, int | float) and math.isfinite(value) and value >= 0:
        return round(value * scale)
    return 0


def _call(record: dict[str, object], *, approval: bool = False) -> ToolCall:
    return ToolCall.model_validate(
        {
            "call_id": record.get("id"),
            "name": record.get("tool" if approval else "name"),
            "arguments": record.get("arguments"),
        }
    )


def _project(run: Run, record: dict[str, object], *, scoped_child: bool = False) -> EngineEvent | None:
    kind = record.get("event")
    if kind == "approval":
        return ApprovalRequested(
            approval_id=_string(record.get("approval_id")),
            call=_call(record, approval=True),
            preview=_string(record.get("preview")),
        )
    if kind == "approval_closed":
        return ApprovalResolved(
            approval_id=_string(record.get("approval_id")), allowed=record.get("decision") == "allow"
        )
    extra = record.get("extra")
    child = (extra.get("session_id") if isinstance(extra, dict) else None) or record.get("session_id")
    if not scoped_child and child and child != run.session_id:
        # These variants describe the root transcript. A child's text/tool
        # identities must not masquerade as an operation of the root.
        return None
    if kind == "text_chunk" and (delta := _string(record.get("chunk"))):
        return TextDelta(text=delta)
    if kind == "text_done" and (text := _string(record.get("text"))):
        return TextCompleted(text=text)
    if kind == "tool_call":
        return ToolStarted(call=_call(record))
    if kind == "tool_result":
        result = _JSON.validate_python(record.get("result"), strict=True)
        return ToolCompleted(
            result=ToolResult(
                call_id=_string(record.get("id")),
                name=_string(record.get("name")),
                result=result,
                error=_string(record.get("error")),
            ),
            duration_ms=_elapsed(record.get("duration_ms")),
        )
    if kind == "tool_approval_timing":
        return ToolApprovalTiming(
            call_id=_string(record.get("call_id")),
            elapsed_ms=_elapsed(record.get("elapsed"), 1000),
            waiting=record.get("waiting") is True,
        )
    if kind in {"error", "notice"} and (text := _string(record.get("message" if kind == "error" else "text"))):
        return Notice(text=text)
    return None


def append_in(db: sqlite3.Connection, session_id: str, run_id: str, event: EngineEvent) -> int:
    """Append inside a mutation's existing transaction."""
    payload = event.model_dump_json()
    if len(payload.encode("utf-8")) > _EVENT_BYTES:
        if isinstance(event, ApprovalRequested):
            raise ValueError("The approval request exceeds the engine event size limit.")
        payload = Notice(text=f"{event.type} exceeded the engine event size limit.").model_dump_json()
    cursor = db.execute(
        "INSERT INTO ngn_engine_events(session_id, run_id, timestamp_ms, payload) VALUES (?, ?, ?, ?)",
        (session_id, run_id, _now_ms(), payload),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def fork_display_in(db: sqlite3.Connection, source: str, target: str) -> None:
    """Copy display evidence in the same transaction as the provider history fork."""
    # Do not copy approval, queue, steering or live task identities. Display
    # IDs exist only in history; they are not addressable engine runs.
    rows = db.execute(
        "SELECT run_id, timestamp_ms, payload FROM ngn_engine_events WHERE session_id = ? "
        "AND json_extract(payload, '$.type') IN "
        "('run.started','text.completed','tool.started','tool.completed','notice','run.finished') "
        "ORDER BY sequence",
        (source,),
    )
    with closing(rows):
        for old_run, stamp, payload in rows:
            if old_run:
                digest = hashlib.sha256(f"{target}\0{old_run}".encode()).hexdigest()[:32]
                run_id = f"fork-history-{digest}"
            else:
                run_id = ""
            db.execute(
                "INSERT INTO ngn_engine_events(session_id, run_id, timestamp_ms, payload) VALUES (?, ?, ?, ?)",
                (target, run_id, stamp, payload),
            )


def _reserve_live_id(db: sqlite3.Connection) -> int:
    # Advance the durable allocator without retaining a live event or payload.
    # The placeholder is never visible outside this transaction.
    cursor = db.execute("INSERT INTO ngn_engine_events(session_id,run_id,timestamp_ms,payload) VALUES ('','',0,'')")
    sequence = cursor.lastrowid
    assert sequence is not None
    db.execute("DELETE FROM ngn_engine_events WHERE sequence = ?", (sequence,))
    return sequence


def _latest_id(db: sqlite3.Connection) -> int:
    row = db.execute(
        "SELECT COALESCE((SELECT seq FROM sqlite_sequence WHERE name = 'ngn_engine_events'), 0)"
    ).fetchone()
    return int(row[0])


def _validate_scope(db: sqlite3.Connection, session: str) -> None:
    root = root_for_child_in(db, session) or session
    if archives.get_in(db, root) is not None:
        return
    RoutingStore.root(db, root)


def _require_owner(db: sqlite3.Connection, run: Run) -> None:
    work = run.work
    if (
        work is None
        or not db.execute(
            "SELECT 1 FROM ngn_web_inbox WHERE id = ? AND session_id = ? AND engine_run_id = ? AND status = 'running'",
            (work.id, run.session_id, run.id),
        ).fetchone()
    ):
        raise RuntimeError("The event no longer belongs to an executing engine input.")


def _finish_child_in(db: sqlite3.Connection, child: ChildActivation, status: RunStatus, error: str) -> None:
    task = finish_in(db, child, status)
    if task is not None:
        append_in(db, task.session_id, task.run_id, RunFinished(status=status, error=error))
        append_in(db, task.root_session_id, task.root_run_id, TaskFinished(task=task, error=error))
        # A retained, completed parent may have a valid descendant
        # continuation. Only stopping the owning worker stops its subtree.
        descendants = running_descendants_in(db, task.run_id) if status in ("cancelled", "interrupted") else []
        for missing in descendants:
            descendant = finish_in(db, missing, "interrupted")
            if descendant is not None:
                reason = "The owning activation ended before this child reported completion."
                append_in(db, descendant.session_id, descendant.run_id, RunFinished(status="interrupted", error=reason))
                append_in(
                    db,
                    descendant.root_session_id,
                    descendant.root_run_id,
                    TaskFinished(task=descendant, error=reason),
                )


@dataclass(frozen=True)
class _Live:
    sequence: int
    session_id: str
    run_id: str
    time_ms: int
    event: TextDelta | EngineConnected
    size: int
    root_session_id: str = ""


@dataclass(eq=False)
class _Listener:
    session_id: str
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    pending: deque[_Live] = field(default_factory=deque)
    size: int = 0
    overflow: bool = False

    def fail(self) -> None:
        self.pending.clear()
        self.size = 0
        self.overflow = True
        self.changed.set()

    def wants(self, session: str, root: str) -> bool:
        return not self.overflow and self.session_id in ("", session, root)

    def offer(self, update: _Live) -> None:
        if not self.wants(update.session_id, update.root_session_id):
            return
        if self.size + update.size > _LIVE_BYTES or len(self.pending) >= _LIVE_ENTRIES:
            self.fail()
        else:
            self.pending.append(update)
            self.size += update.size
        self.changed.set()


class EngineJournal:
    def __init__(self, store: RoutingStore) -> None:
        self.store = store
        self._read_uri = store.db_path.resolve().as_uri() + "?mode=ro"
        self._listeners: set[_Listener] = set()
        # A stream must not snapshot the DB while a delta ID has committed but
        # its in-memory payload has not been offered. Ordinary durable writers
        # need no lock: their IDs and payloads commit together.
        self._publication = asyncio.Lock()

    async def read[T](self, query: Callable[[sqlite3.Connection], T]) -> T:
        def snapshot() -> T:
            with closing(sqlite3.connect(self._read_uri, uri=True, timeout=5)) as db, db:
                db.execute("BEGIN")
                return query(db)

        return await finish_on_cancel(asyncio.to_thread(snapshot))

    def changed(self) -> None:
        for listener in self._listeners:
            listener.changed.set()

    async def capture(self, run: Run, record: dict[str, object]) -> None:
        work = run.work
        if work is None or work.engine is None or work.engine.run_id != run.id:
            return
        kind = record.get("event")
        if kind == "approval" and (run.pending is None or run.pending.record is not record):
            return
        if kind in {"task_started", "task_completed"}:
            await self._capture_task(run, record, work.engine.request_id)
            return

        child = None
        actor, number = record.get("session_id"), record.get("activation")
        if kind in {"task_activity", "approval", "approval_closed", "tool_approval_timing"} and actor not in (
            None,
            "",
            run.session_id,
        ):
            child = await self.read(lambda db: activation_in(db, run.id, actor, number))
        if kind == "task_activity":
            payload = record.get("payload")
            if (
                child is None
                or child.task.status != "running"
                or record.get("root_session_id") != run.session_id
                or not isinstance(payload, dict)
                or payload.get("type") not in ("text_chunk", "text_done", "tool_call", "tool_result")
            ):
                return
            projected_record = {**payload, "event": payload.get("type")}
        else:
            projected_record = record
        try:
            event = _project(run, projected_record, scoped_child=child is not None)
        except ValidationError:
            if kind == "approval":
                # Never present a cut-down approval for different arguments.
                raise ValueError("The approval request cannot be represented by the engine.") from None
            event = Notice(text="An update could not be represented by the engine protocol.")
        if event is None:
            return
        session, run_id = (child.task.session_id, child.task.run_id) if child else (run.session_id, run.id)
        if isinstance(event, TextDelta):
            await self._publish_live(session, run_id, event, run.session_id)
            return

        def persist(db: sqlite3.Connection) -> None:
            _require_owner(db, run)
            if child is not None and ((current := task_run_in(db, run_id)) is None or current.task.status != "running"):
                return
            append_in(db, session, run_id, event)

        await self.store._transaction(persist)
        self.changed()

    async def _capture_task(self, run: Run, record: dict[str, object], request_id: str) -> None:
        actor, number = record.get("session_id"), record.get("activation")
        if record.get("root_session_id") != run.session_id:
            return

        def persist(db: sqlite3.Connection) -> None:
            _require_owner(db, run)
            if record.get("event") == "task_started":
                child = start_in(
                    db,
                    root=run.session_id,
                    root_run=run.id,
                    request_id=request_id,
                    record=record,
                )
                if child is not None:
                    task = child.task
                    append_in(db, run.session_id, task.root_run_id, TaskStarted(task=task))
                    append_in(db, task.session_id, task.run_id, RunStarted(prompt=_string(record.get("prompt"))))
                return
            child = activation_in(db, run.id, actor, number)
            status = record.get("status")
            if child is None or status not in ("completed", "failed", "cancelled"):
                return
            _finish_child_in(db, child, status, _string(record.get("error")))

        await self.store._transaction(persist)
        self.changed()

    async def finish_child(self, child: ChildActivation, status: RunStatus, error: str) -> None:
        await self.store._transaction(lambda db: _finish_child_in(db, child, status, error))
        self.changed()

    async def _publish_live(self, session: str, run_id: str, event: TextDelta, root: str) -> None:
        if not any(sub.wants(session, root) for sub in self._listeners):
            return

        def reserve(db: sqlite3.Connection) -> int | None:
            if session != root and ((current := task_run_in(db, run_id)) is None or current.task.status != "running"):
                return None
            return _reserve_live_id(db)

        async with self._publication:
            try:
                sequence = await self.store._transaction(reserve)
            except sqlite3.Error:
                # Failure to publish an optional delta never changes what the
                # agent did; a bounded client can recover the durable transcript.
                for listener in self._listeners:
                    if listener.wants(session, root):
                        listener.fail()
                return
            if sequence is None:
                return
            update = _Live(
                sequence, session, run_id, _now_ms(), event, len(event.model_dump_json().encode("utf-8")), root
            )
            for listener in self._listeners:
                listener.offer(update)

    async def _page(
        self, session_id: str, after: int, limit: int, *, include_children: bool = False
    ) -> tuple[History, int]:
        def replay(db: sqlite3.Connection) -> tuple[History, int]:
            if session_id:
                _validate_scope(db, session_id)
            params: tuple[str, ...] = ()
            scope = ""
            if session_id:
                scope, params = " AND e.session_id = ?", (session_id,)
                if include_children:
                    scope = (
                        " AND (e.session_id = ? OR EXISTS (SELECT 1 FROM ngn_engine_tasks t "
                        "WHERE t.session_id = e.session_id AND t.root_session_id = ?))"
                    )
                    params = (session_id, session_id)
            values = (after, *params, limit + 1)
            items: list[EventRecord] = []
            size = 0
            has_more = False
            with closing(
                db.execute(
                    "SELECT e.sequence, e.session_id, e.run_id, e.timestamp_ms, e.payload "
                    "FROM ngn_engine_events e WHERE e.sequence > ? "
                    "AND (EXISTS (SELECT 1 FROM harness_sessions h JOIN v2_sessions s ON s.id = h.id "
                    "WHERE h.id = e.session_id) OR EXISTS (SELECT 1 FROM ngn_engine_tasks t "
                    "JOIN harness_sessions h ON h.id = t.root_session_id JOIN v2_sessions s ON s.id = h.id "
                    "WHERE t.session_id = e.session_id) OR EXISTS ("
                    "SELECT 1 FROM ngn_engine_archives a WHERE a.id = e.session_id))"
                    f"{scope} ORDER BY e.sequence LIMIT ?",
                    values,
                )
            ) as rows:
                for sequence, root, run, timestamp_ms, payload in rows:
                    size += len(payload.encode("utf-8"))
                    if len(items) == limit or (items and size > _PAGE_BYTES):
                        has_more = True
                        break
                    items.append(
                        EventRecord(
                            schema_version=_SCHEMA_VERSION,
                            sequence=sequence,
                            session_id=root,
                            run_id=run,
                            timestamp_ms=timestamp_ms,
                            event=_EVENT.validate_json(payload),
                        )
                    )
            return History(events=items, cursor=items[-1].sequence if items else after, has_more=has_more), _latest_id(
                db
            )

        return await self.read(replay)

    async def history(self, session_id: str, after: int, limit: int) -> History:
        page, _ = await self._page(session_id, after, limit)
        return page

    @staticmethod
    async def _send(socket: WebSocket, record: EventRecord) -> None:
        async with asyncio.timeout(_SEND_TIMEOUT):
            await socket.send_text(record.model_dump_json())

    async def _live_before(self, socket: WebSocket, listener: _Listener, before: int, after: int) -> int:
        while not listener.overflow and listener.pending and listener.pending[0].sequence < before:
            update = listener.pending.popleft()
            listener.size -= update.size
            if update.sequence > after:
                await self._send(
                    socket,
                    EventRecord(
                        schema_version=_SCHEMA_VERSION,
                        sequence=update.sequence,
                        session_id=update.session_id,
                        run_id=update.run_id,
                        timestamp_ms=update.time_ms,
                        event=update.event,
                    ),
                )
                after = update.sequence
        return after

    async def events(self, socket: WebSocket, after: int, session_id: str, info: EngineInfo) -> None:
        if len(self._listeners) >= _MAX_LISTENERS:
            await socket.close(1013, "Too many engine event clients.")
            return
        listener = _Listener(session_id)
        self._listeners.add(listener)  # Subscribe before the first durable read.
        receive: asyncio.Task[Message] | None = None
        try:

            def check(db: sqlite3.Connection) -> int:
                if session_id:
                    _validate_scope(db, session_id)
                return _latest_id(db)

            if after > await self.read(check):
                await socket.close(1008, "Cursor is ahead of this workspace's event journal.")
                return
            await socket.accept(subprotocol=EVENTS_SUBPROTOCOL)
            receive = asyncio.create_task(socket.receive())
            async with self._publication:
                # This notification belongs only to this listener. Its ID in
                # the same allocator puts all prior durable replay before it.
                event = EngineConnected(engine=info)
                sequence = await self.store._transaction(_reserve_live_id)
                listener.offer(_Live(sequence, session_id, "", _now_ms(), event, len(event.model_dump_json())))
            while not receive.done():
                listener.changed.clear()
                async with self._publication:
                    page, latest = await self._page(session_id, after, _PAGE_SIZE, include_children=True)
                for record in page.events:
                    if listener.overflow or receive.done():
                        break
                    after = await self._live_before(socket, listener, record.sequence, after)
                    if listener.overflow or receive.done():
                        break
                    await self._send(socket, record)
                    after = record.sequence
                if not page.has_more and not receive.done():
                    after = await self._live_before(socket, listener, latest + 1, after)
                if receive.done():
                    break
                if listener.overflow:
                    await socket.close(1013, "Too many live updates; reconnect with the last event cursor.")
                    return
                if not page.has_more and not listener.changed.is_set():
                    ready = asyncio.create_task(listener.changed.wait())
                    try:
                        await asyncio.wait(
                            (receive, ready), timeout=_POLL_INTERVAL, return_when=asyncio.FIRST_COMPLETED
                        )
                    finally:
                        ready.cancel()
                        await asyncio.gather(ready, return_exceptions=True)
            if (await receive).get("type") != "websocket.disconnect":
                await socket.close(1008, "The engine event stream is receive-only.")
        except (HTTPException, TimeoutError, WebSocketDisconnect, OSError) as error:
            if isinstance(error, HTTPException):
                with suppress(RuntimeError, WebSocketDisconnect, OSError):
                    await socket.close(1008, "Session no longer exists.")
            elif isinstance(error, TimeoutError):
                with suppress(RuntimeError, WebSocketDisconnect, OSError):
                    await socket.close(1013, "Slow consumer; reconnect with the last event cursor.")
        except sqlite3.Error:
            with suppress(RuntimeError, WebSocketDisconnect, OSError):
                await socket.close(1011, "The engine event journal is temporarily unavailable.")
        finally:
            self._listeners.discard(listener)
            if receive is not None:
                receive.cancel()
                await asyncio.gather(receive, return_exceptions=True)
