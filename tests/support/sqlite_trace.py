"""Opt-in, failure-only SQLite operation tracing for selected Windows tests.

The trace deliberately records no SQL, values, paths, or exception text. It is
diagnostic only: connection factories, transaction modes, and busy timeouts are
left unchanged. Set ``NGN_SQLITE_TRACE=1`` and load this module as a pytest plugin
to enable tracing for ``_TARGETS``.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import sqlite3
import sys
import threading
import time
import weakref
from collections import deque
from collections.abc import Callable
from contextlib import AbstractContextManager
from contextlib import suppress
from typing import TYPE_CHECKING
from typing import Literal
from typing import Self
from typing import TypeVar
from typing import cast
from typing import overload

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable
    from collections.abc import Generator
    from collections.abc import Iterable
    from types import FrameType
    from types import TracebackType

    from pluggy import Result


import pytest

_LIMIT = 2000
_SAFE_VERBS = frozenset(
    {
        "SELECT",
        "INSERT",
        "UPDATE",
        "DELETE",
        "REPLACE",
        "CREATE",
        "ALTER",
        "DROP",
        "PRAGMA",
        "BEGIN",
        "COMMIT",
        "END",
        "ROLLBACK",
        "SAVEPOINT",
        "RELEASE",
        "ATTACH",
        "DETACH",
        "OTHER",
    }
)
_TARGETS = (
    "tests/channels/test_channels_runtime.py::test_durable_acceptance_and_dedup_survive_a_new_agent",
    "tests/web/test_web_child_failure_scope.py::test_stop_still_cancels_chat_and_voice_trees[True]",
    "tests/web/test_web_child_failure_scope.py::test_recovered_root_keeps_child_failures_and_safe_scope_in_chat_and_voice",
    "tests/web/test_agent_models.py::test_next_real_response_uses_the_saved_provider_and_model_together",
    "tests/web/test_web_settings.py::test_settings_cancellation_joins_transaction_and_keeps_get_consistent",
    "tests/web/test_web_settings.py::test_settings_save_and_reset_while_run_or_approval_continues",
    "tests/web/test_web_trash.py::test_policy_is_separate_revisioned_persistent_and_affects_only_future_deletions",
    "tests/web/test_subagent_limits.py::test_approved_limits_are_read_on_next_model_turn_without_resetting_usage",
    "tests/test_web_live_update_edges.py::test_attention_cue_rechecks_quiet_state_and_never_speaks_native_instruction_text",
)
_IDS = itertools.count(1)
_TRACE_LOCAL = threading.local()
_FETCH_DEFAULT = object()
_CURSOR_FACTORY_DEFAULT = object()
_CursorT = TypeVar("_CursorT", bound=sqlite3.Cursor)


def _set_attribute(target: object, name: str, value: object) -> None:
    setattr(target, name, value)


def _verb(statement: object) -> str:
    if not isinstance(statement, str):
        return "OTHER"
    first = statement.lstrip().split(None, 1)[0].split("(", 1)[0].upper() if statement.strip() else "OTHER"
    return first if first in _SAFE_VERBS else "OTHER"


def _source() -> list[dict[str, str | int]]:
    inherited = getattr(_TRACE_LOCAL, "source", None)
    if inherited is not None:
        return list(inherited)
    current: FrameType | None = sys._getframe(1)
    result: list[dict[str, str | int]] = []
    try:
        while current is not None and len(result) < 4:
            filename = os.path.basename(current.f_code.co_filename)
            if filename != "sqlite_trace.py":
                result.append(
                    {
                        "file": filename[:80],
                        "function": current.f_code.co_name[:80],
                        "line": current.f_lineno,
                    }
                )
            current = current.f_back
        return result
    finally:
        del current


def _thread_stacks() -> list[dict[str, object]]:
    frames = sys._current_frames()
    stacks: list[dict[str, object]] = []
    items = list(frames.items())[:16]
    try:
        for thread_id, frame in items:
            functions: list[dict[str, str | int]] = []
            current: FrameType | None = frame
            try:
                while current is not None and len(functions) < 12:
                    functions.append(
                        {
                            "file": os.path.basename(current.f_code.co_filename)[:80],
                            "function": current.f_code.co_name[:80],
                            "line": current.f_lineno,
                        }
                    )
                    current = current.f_back
            finally:
                del current
                del frame
            stacks.append({"thread": thread_id, "functions": functions})
        return stacks
    finally:
        del items
        frames.clear()
        del frames


class TraceCapture(AbstractContextManager["TraceCapture"]):
    """Temporarily trace SQLite operation classes and lifecycle metadata."""

    def __init__(self) -> None:
        self.events: deque[dict[str, object]] = deque(maxlen=_LIMIT)
        self._start = time.monotonic()
        self._original_connect = sqlite3.connect
        self._aio_restore: Callable[[], None] | None = None
        self._installed = False
        self._lock = threading.RLock()
        self._connection_ids: dict[int, int] = {}
        self._database_ids: dict[int, str] = {}
        self._cursor_ids: weakref.WeakKeyDictionary[sqlite3.Cursor, int] = weakref.WeakKeyDictionary()
        self._transactions: dict[int, float] = {}
        self._cursors: dict[int, dict[str, object]] = {}
        self._open_connections: set[int] = set()

    def _connection_id(self, connection: sqlite3.Connection) -> int | None:
        with self._lock:
            return self._connection_ids.get(id(connection))

    def _database_id(self, connection: sqlite3.Connection) -> str | None:
        with self._lock:
            return self._database_ids.get(id(connection))

    def _cursor_opened(self, cursor: sqlite3.Cursor, connection: sqlite3.Connection) -> int:
        cursor_id = next(_IDS)
        with self._lock:
            self._cursor_ids[cursor] = cursor_id
            self._cursors[cursor_id] = {
                "connection": self._connection_ids.get(id(connection)),
                "source": _source(),
                "opened_ms": round((time.monotonic() - self._start) * 1000, 3),
                "potentially_unfinished": True,
            }
        weakref.finalize(cursor, self._cursor_collected, cursor_id)
        self._record("cursor_open", connection=self._connection_id(connection), cursor=cursor_id)
        return cursor_id

    def _cursor_collected(self, cursor_id: int) -> None:
        with self._lock:
            self._cursors.pop(cursor_id, None)

    def _cursor_exhausted(self, cursor_id: int | None) -> None:
        if cursor_id is None:
            return
        with self._lock:
            cursor = self._cursors.get(cursor_id)
            if cursor is not None:
                cursor["potentially_unfinished"] = False
        self._record("cursor_exhausted", cursor=cursor_id)

    def _cursor_id(self, cursor: sqlite3.Cursor) -> int | None:
        with self._lock:
            return self._cursor_ids.get(cursor)

    def _database_hash(self, database: object) -> str | None:
        try:
            path = cast("str | bytes | os.PathLike[str] | os.PathLike[bytes]", database)
            return hashlib.sha256(os.fsencode(path)).hexdigest()[:16]
        except (TypeError, ValueError, OSError):
            return None

    def _connection_opened(self, connection: sqlite3.Connection, database: object) -> int:
        connection_id = next(_IDS)
        database_hash = self._database_hash(database)
        object_id = id(connection)
        with self._lock:
            self._connection_ids[object_id] = connection_id
            if database_hash is not None:
                self._database_ids[object_id] = database_hash
            self._open_connections.add(connection_id)
        weakref.finalize(connection, self._connection_collected, object_id, connection_id)
        self._record("connection_open", connection=connection_id, database=database_hash, source=_source())
        return connection_id

    def _connection_collected(self, object_id: int, connection_id: int) -> None:
        try:
            with self._lock:
                self._connection_ids.pop(object_id, None)
                self._database_ids.pop(object_id, None)
                self._transactions.pop(object_id, None)
                self._open_connections.discard(connection_id)
        except BaseException:
            pass

    def _connection_closed(self, connection: sqlite3.Connection, connection_id: int | None) -> None:
        with self._lock:
            self._transactions.pop(id(connection), None)
            self._connection_ids.pop(id(connection), None)
            self._database_ids.pop(id(connection), None)
            if connection_id is not None:
                self._open_connections.discard(connection_id)

    def _record(self, event: str, **fields: object) -> None:
        with self._lock:
            self.events.append(
                {
                    "ms": round((time.monotonic() - self._start) * 1000, 3),
                    "thread": threading.get_ident(),
                    "event": event,
                    **fields,
                }
            )

    def _track_transaction(self, connection: sqlite3.Connection, cursor: int | None = None) -> None:
        try:
            active = connection.in_transaction
        except sqlite3.Error:
            return
        connection_key = id(connection)
        with self._lock:
            started = self._transactions.get(connection_key)
            if active and started is None:
                started = time.monotonic()
                self._transactions[connection_key] = started
            elif not active and started is not None:
                self._transactions.pop(connection_key, None)
            else:
                return
        if active:
            self._record(
                "transaction_begin",
                connection=self._connection_id(connection),
                database=self._database_id(connection),
                cursor=cursor,
            )
        else:
            self._record(
                "transaction_end",
                connection=self._connection_id(connection),
                database=self._database_id(connection),
                cursor=cursor,
                duration_ms=round((time.monotonic() - started) * 1000, 3),
            )

    def _operation(
        self,
        connection: sqlite3.Connection,
        operation: str,
        callback: Callable[[], object],
        *,
        cursor: int | None = None,
        verb: str = "OTHER",
    ) -> object:
        connection_id = self._connection_id(connection)
        started = time.monotonic()
        database_id = self._database_id(connection)
        self._record(
            "operation_start",
            connection=connection_id,
            database=database_id,
            cursor=cursor,
            operation=operation,
            verb=verb,
            source=_source(),
        )
        try:
            result = callback()
        except sqlite3.Error as error:
            try:
                self._track_transaction(connection, cursor)
                code = getattr(error, "sqlite_errorcode", None)
                self._record(
                    "operation_error",
                    connection=connection_id,
                    database=database_id,
                    cursor=cursor,
                    operation=operation,
                    verb=verb,
                    sqlite_code=code if type(code) is int and 0 <= code <= 2**31 - 1 else None,
                    duration_ms=round((time.monotonic() - started) * 1000, 3),
                )
                if type(code) is int and code & 255 in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                    self._record("busy_thread_stacks", threads=_thread_stacks())
            except BaseException:
                # Diagnostics must never replace the database failure.
                pass
            raise
        self._track_transaction(connection, cursor)
        self._record(
            "operation_end",
            connection=connection_id,
            database=database_id,
            cursor=cursor,
            operation=operation,
            verb=verb,
            duration_ms=round((time.monotonic() - started) * 1000, 3),
        )
        return result

    def _make_connection_type(self) -> type[sqlite3.Connection]:
        capture = self

        class TracedCursor(sqlite3.Cursor):
            def __init__(self, connection: sqlite3.Connection) -> None:
                super().__init__(connection)
                capture._cursor_opened(self, connection)

            def execute(self, sql: str, parameters: object = ()) -> Self:
                result = capture._operation(
                    self.connection,
                    "execute",
                    lambda: super(TracedCursor, self).execute(sql, cast("sqlite3._Parameters", parameters)),
                    cursor=capture._cursor_id(self),
                    verb=_verb(sql),
                )
                return cast("Self", result)

            def executemany(self, sql: str, parameters: object) -> Self:
                result = capture._operation(
                    self.connection,
                    "executemany",
                    lambda: super(TracedCursor, self).executemany(
                        sql, cast("Iterable[sqlite3._Parameters]", parameters)
                    ),
                    cursor=capture._cursor_id(self),
                    verb=_verb(sql),
                )
                return cast("Self", result)

            def executescript(self, script: str) -> sqlite3.Cursor:
                return capture._operation(
                    self.connection,
                    "executescript",
                    lambda: super(TracedCursor, self).executescript(script),
                    cursor=capture._cursor_id(self),
                    verb="OTHER",
                )  # type: ignore[return-value]

            def fetchone(self) -> object | None:
                cursor_id = capture._cursor_id(self)
                result = capture._operation(self.connection, "fetchone", super().fetchone, cursor=cursor_id)
                if result is None:
                    capture._cursor_exhausted(cursor_id)
                return result

            def fetchall(self) -> list[object]:
                cursor_id = capture._cursor_id(self)
                result = capture._operation(self.connection, "fetchall", super().fetchall, cursor=cursor_id)
                capture._cursor_exhausted(cursor_id)
                return cast("list[object]", result)

            def fetchmany(self, size: object = _FETCH_DEFAULT) -> list[object]:
                cursor_id = capture._cursor_id(self)
                requested: int | None
                if size is _FETCH_DEFAULT:
                    result = capture._operation(self.connection, "fetchmany", super().fetchmany, cursor=cursor_id)
                    requested = self.arraysize
                else:
                    result = capture._operation(
                        self.connection,
                        "fetchmany",
                        lambda: super(TracedCursor, self).fetchmany(cast("int", size)),
                        cursor=cursor_id,
                    )
                    requested = int(size) if isinstance(size, int) else None
                rows = cast("list[object]", result)
                capture._record("cursor_fetch", cursor=cursor_id, rows=len(rows))
                if requested is not None and len(rows) < requested:
                    capture._cursor_exhausted(cursor_id)
                return rows

            def __next__(self) -> object:
                cursor_id = capture._cursor_id(self)
                try:
                    return super().__next__()
                except StopIteration:
                    capture._cursor_exhausted(cursor_id)
                    raise

            def close(self) -> None:
                cursor_id = capture._cursor_id(self)
                capture._record(
                    "cursor_close_start", connection=capture._connection_id(self.connection), cursor=cursor_id
                )
                super().close()
                capture._record(
                    "cursor_close_end", connection=capture._connection_id(self.connection), cursor=cursor_id
                )
                if cursor_id is not None:
                    capture._cursor_collected(cursor_id)

        class TracedConnection(sqlite3.Connection):
            def __init__(self, database: object, *args: object, **kwargs: object) -> None:
                cast("Callable[..., None]", sqlite3.Connection.__init__)(self, database, *args, **kwargs)
                capture._connection_opened(self, database)

            @overload
            def cursor(self, factory: None = None) -> sqlite3.Cursor: ...

            @overload
            def cursor(self, factory: Callable[[sqlite3.Connection], _CursorT]) -> _CursorT: ...

            def cursor(self, factory: object = _CURSOR_FACTORY_DEFAULT) -> sqlite3.Cursor:
                connection_id = capture._connection_id(self)
                if factory is _CURSOR_FACTORY_DEFAULT:
                    return super().cursor(TracedCursor)
                capture._record("custom_cursor_factory", connection=connection_id)
                return super().cursor(cast("Callable[[sqlite3.Connection], sqlite3.Cursor]", factory))

            def execute(self, sql: str, parameters: object = ()) -> sqlite3.Cursor:
                return self.cursor().execute(sql, cast("sqlite3._Parameters", parameters))

            def executemany(self, sql: str, parameters: object) -> sqlite3.Cursor:
                return self.cursor().executemany(sql, cast("Iterable[sqlite3._Parameters]", parameters))

            def executescript(self, script: str) -> sqlite3.Cursor:
                return self.cursor().executescript(script)

            def commit(self) -> None:
                capture._operation(self, "commit", lambda: super(TracedConnection, self).commit(), cursor=None)

            def rollback(self) -> None:
                capture._operation(self, "rollback", lambda: super(TracedConnection, self).rollback(), cursor=None)

            def close(self) -> None:
                connection_id = capture._connection_id(self)
                try:
                    active_transaction = self.in_transaction
                except sqlite3.Error:
                    active_transaction = False
                capture._record(
                    "connection_close_start", connection=connection_id, active_transaction=active_transaction
                )
                super().close()
                capture._connection_closed(self, connection_id)
                capture._record("connection_close_end", connection=connection_id)

            def __exit__(
                self,
                exc_type: type[BaseException] | None,
                exc: BaseException | None,
                traceback: TracebackType | None,
            ) -> Literal[False]:
                result = capture._operation(
                    self,
                    "context_exit",
                    lambda: super(TracedConnection, self).__exit__(exc_type, exc, traceback),
                )
                return cast("Literal[False]", result)

        return TracedConnection

    def __enter__(self) -> TraceCapture:
        if self._installed:
            raise RuntimeError("TraceCapture cannot be entered twice")
        original_connect = self._original_connect
        connection_type = self._make_connection_type()

        def connect(*args: object, **kwargs: object) -> sqlite3.Connection:
            positional = list(args)
            factory = positional[5] if len(positional) > 5 else kwargs.get("factory", sqlite3.Connection)
            if factory is sqlite3.Connection:
                if len(positional) > 5:
                    positional[5] = connection_type
                else:
                    kwargs["factory"] = connection_type
            return cast("Callable[..., sqlite3.Connection]", original_connect)(*positional, **kwargs)

        sqlite3.connect = connect  # type: ignore[assignment]
        try:
            import aiosqlite
        except ImportError:
            pass
        else:
            original_execute = cast("Callable[..., Awaitable[object]]", aiosqlite.Connection._execute)

            async def traced_aio_execute(
                connection: object, fn: Callable[..., object], *args: object, **kwargs: object
            ) -> object:
                origin = _source()

                def annotated(*inner_args: object, **inner_kwargs: object) -> object:
                    previous = getattr(_TRACE_LOCAL, "source", None)
                    _TRACE_LOCAL.source = origin
                    try:
                        return fn(*inner_args, **inner_kwargs)
                    finally:
                        _TRACE_LOCAL.source = previous

                return await original_execute(cast("aiosqlite.Connection", connection), annotated, *args, **kwargs)

            _set_attribute(aiosqlite.Connection, "_execute", traced_aio_execute)

            def restore_aio() -> None:
                _set_attribute(aiosqlite.Connection, "_execute", original_execute)

            self._aio_restore = restore_aio
        self._installed = True
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> Literal[False]:
        if self._installed:
            try:
                if self._aio_restore is not None:
                    self._aio_restore()
                    self._aio_restore = None
            finally:
                sqlite3.connect = self._original_connect
                self._installed = False
        return False

    def safe_snapshot(self) -> dict[str, object]:
        with self._lock:
            now = time.monotonic()
            return {
                "events": list(self.events),
                "open_connections": len(self._open_connections),
                "connections": [
                    {
                        "connection": connection_id,
                        "database": self._database_ids.get(object_id),
                    }
                    for object_id, connection_id in self._connection_ids.items()
                ],
                "transactions": [
                    {
                        "connection": self._connection_ids.get(object_id),
                        "database": self._database_ids.get(object_id),
                        "duration_ms": round((now - started) * 1000, 3),
                    }
                    for object_id, started in self._transactions.items()
                ],
                "potentially_unfinished_cursors": [
                    dict(cursor) for cursor in self._cursors.values() if cursor.get("potentially_unfinished")
                ],
            }


_ACTIVE: TraceCapture | None = None


def _enabled() -> bool:
    return os.environ.get("NGN_SQLITE_TRACE") == "1"


def _is_target(nodeid: str) -> bool:
    return any(nodeid.startswith(target) for target in _TARGETS)


def _report_trace(report: pytest.TestReport, item: pytest.Item, capture: TraceCapture) -> None:
    snapshot = capture.safe_snapshot()
    events = cast("list[dict[str, object]]", snapshot.pop("events"))
    base = {"test": item.nodeid, **snapshot}
    chunks: list[list[dict[str, object]]] = []
    current: list[dict[str, object]] = []
    for event in events:
        candidate = [*current, event]
        size = len(json.dumps({"events": candidate}, separators=(",", ":")))
        if current and size > 24_000:
            chunks.append(current)
            current = [event]
        else:
            current = candidate
    if current:
        chunks.append(current)
    for index, chunk in enumerate(chunks):
        payload = json.dumps(
            {**base, "chunk": index + 1, "chunks": len(chunks), "events": chunk}, separators=(",", ":")
        )
        report.sections.append((f"NGN_SQLITE_TRACE {index + 1}/{len(chunks)}", payload))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None) -> Generator[None, Result[bool], None]:
    global _ACTIVE
    if not _enabled() or not _is_target(item.nodeid):
        yield
        return
    capture = TraceCapture()
    _ACTIVE = capture
    try:
        with capture:
            yield
    finally:
        _ACTIVE = None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[object]
) -> Generator[None, Result[pytest.TestReport], None]:
    outcome = yield
    report = outcome.get_result()
    if report.failed and _ACTIVE is not None:
        with suppress(BaseException):
            _report_trace(report, item, _ACTIVE)
