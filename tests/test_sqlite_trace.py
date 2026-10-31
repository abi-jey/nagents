"""Safety and signal checks for the opt-in SQLite CI tracer."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from types import SimpleNamespace
from typing import TYPE_CHECKING
from typing import cast

import aiosqlite
import pytest

from tests.support.sqlite_trace import TraceCapture
from tests.support.sqlite_trace import _report_trace

if TYPE_CHECKING:
    from pathlib import Path


def test_busy_trace_identifies_active_writer_without_recording_sqlite_payloads(tmp_path: Path) -> None:
    database = tmp_path / "private-database-name.sqlite"
    original_connect = sqlite3.connect

    with TraceCapture() as trace:
        writer = sqlite3.connect(database, timeout=0.01)
        blocked = sqlite3.connect(database, timeout=0.01)
        writer.execute("CREATE TABLE private_table (value TEXT)")
        writer.commit()
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO private_table VALUES (?)", ("SECRET_PARAMETER",))

        with pytest.raises(sqlite3.OperationalError):
            blocked.execute("UPDATE private_table SET value = 'SECRET_SQL_LITERAL'")

        events = list(trace.events)
        writer_transaction = next(event for event in events if event["event"] == "transaction_begin")
        busy = next(event for event in events if event["event"] == "operation_error")
        assert writer_transaction["connection"] != busy["connection"]
        assert busy["verb"] == "UPDATE"
        assert isinstance(busy["duration_ms"], float)
        assert busy["sqlite_code"] in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
        assert any(
            event["event"] == "transaction_begin" and event["connection"] == writer_transaction["connection"]
            for event in events
        )

        snapshot = json.dumps(trace.safe_snapshot())
        for secret in (
            "SECRET_PARAMETER",
            "SECRET_SQL_LITERAL",
            "private_table",
            "private-database-name.sqlite",
            str(database),
            "database is locked",
        ):
            assert secret not in snapshot

        blocked.close()
        writer.rollback()
        writer.close()

    assert sqlite3.connect is original_connect


def test_busy_begin_snapshot_identifies_live_writer_at_failure_point(tmp_path: Path) -> None:
    database = tmp_path / "private-busy-begin.sqlite"
    with TraceCapture() as trace:
        writer = sqlite3.connect(database, timeout=0.01)
        blocked = sqlite3.connect(database, timeout=0.01)
        writer.execute("CREATE TABLE private_table (value TEXT)")
        writer.commit()
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO private_table VALUES (?)", ("SECRET_PARAMETER",))

        with pytest.raises(sqlite3.OperationalError):
            blocked.execute("BEGIN IMMEDIATE")

        events = list(trace.events)
        busy = next(event for event in events if event["event"] == "operation_error")
        point = next(event for event in events if event["event"] == "busy_point_snapshot")
        assert busy["verb"] == "BEGIN"
        assert busy["sqlite_code"] in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
        transactions = point["transactions"]
        assert isinstance(transactions, list)
        writer_transaction = next(
            transaction for transaction in transactions if transaction["connection"] != busy["connection"]
        )
        assert isinstance(writer_transaction["thread"], int)
        assert writer_transaction["source"]
        assert writer_transaction["duration_ms"] >= 0
        assert all(transaction["connection"] != busy["connection"] for transaction in transactions)
        connections = point["connections"]
        assert isinstance(connections, list)
        assert any(connection["connection"] == busy["connection"] for connection in connections)
        assert all(connection["last_operation"] is not None for connection in connections)
        assert point["threads"]

        serialized = json.dumps(point)
        for secret in (
            "SECRET_PARAMETER",
            "private_table",
            "private-busy-begin.sqlite",
            str(database),
            "database is locked",
        ):
            assert secret not in serialized

        blocked.close()
        writer.rollback()
        writer.close()


def test_trace_records_successful_commit_and_connection_close(tmp_path: Path) -> None:
    with TraceCapture() as trace:
        connection = sqlite3.connect(tmp_path / "success.sqlite")
        connection.execute("CREATE TABLE t (value INTEGER)")
        connection.execute("INSERT INTO t VALUES (?)", (7,))
        connection.commit()
        connection.close()

    events = list(trace.events)
    assert any(event["event"] == "operation_end" and event["operation"] == "commit" for event in events)
    assert any(event["event"] == "connection_close_end" for event in events)
    assert trace.safe_snapshot()["open_connections"] == 0


def test_trace_preserves_custom_connection_factory_and_restores_after_exception(tmp_path: Path) -> None:
    class CustomConnection(sqlite3.Connection):
        pass

    original_connect = sqlite3.connect
    original_aio_execute = aiosqlite.Connection._execute
    trace = TraceCapture()
    with pytest.raises(RuntimeError, match="sentinel"), trace:
        assert sqlite3.connect is not original_connect
        assert aiosqlite.Connection._execute is not original_aio_execute
        connection = sqlite3.connect(tmp_path / "custom.sqlite", factory=CustomConnection)
        assert isinstance(connection, CustomConnection)
        connection.execute("SELECT 1").close()
        connection.close()
        assert not any(event["event"] == "connection_open" for event in trace.events)
        raise RuntimeError("sentinel")

    assert sqlite3.connect is original_connect
    assert aiosqlite.Connection._execute is original_aio_execute
    with closing(sqlite3.connect(":memory:")) as plain_connection:
        assert plain_connection.execute("SELECT 1").fetchone() == (1,)


def test_wrapped_cursor_none_matches_sqlite_and_connection_close_is_idempotent(tmp_path: Path) -> None:
    plain = sqlite3.connect(tmp_path / "plain.sqlite")
    with pytest.raises(TypeError):
        plain.cursor(None)
    plain.close()
    plain.close()

    with TraceCapture():
        traced = sqlite3.connect(tmp_path / "traced.sqlite")
        with pytest.raises(TypeError):
            traced.cursor(None)
        traced.close()
        traced.close()


def test_failed_report_splits_safe_diagnostics_into_bounded_sections() -> None:
    trace = TraceCapture()
    trace.events.extend(
        {
            "ms": float(index),
            "thread": 123,
            "event": "operation_end",
            "connection": 1,
            "database": "0123456789abcdef",
            "cursor": index,
            "operation": "execute",
            "verb": "SELECT",
            "duration_ms": 0.1,
            "source": [{"file": "safe_test.py", "function": "bounded_operation", "line": 42}],
        }
        for index in range(500)
    )
    report = pytest.TestReport(
        nodeid="tests/test_safe_trace.py::test_failure",
        location=("tests/test_safe_trace.py", 1, "test_failure"),
        keywords={},
        outcome="failed",
        longrepr="sanitized test failure",
        when="call",
    )
    item = cast("pytest.Item", SimpleNamespace(nodeid="tests/test_safe_trace.py::test_failure"))

    _report_trace(report, item, trace)

    assert len(report.sections) > 1
    serialized_sections = [json.loads(content) for _name, content in report.sections]
    assert [section["chunk"] for section in serialized_sections] == list(range(1, len(report.sections) + 1))
    for section in serialized_sections:
        assert section["chunks"] == len(report.sections)
        encoded = json.dumps(section)
        assert "SELECT" in encoded
        assert "sqlite" not in encoded.lower()
