"""Safety and privacy checks for the low-interference SQLite snapshot plugin."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from typing import cast

import aiosqlite

from nagents.channels.store import InboxStore
from nagents.session.manager import SessionManager
from tests.support import sqlite_busy_snapshot as snapshot
from tests.support.hang_guard import HANG_GUARD

if TYPE_CHECKING:
    import pytest


def test_busy_snapshot_filter_never_wraps_sqlite_and_rejects_other_warnings(tmp_path: Path) -> None:
    connect = sqlite3.connect
    aio_execute = aiosqlite.Connection._execute
    capture = snapshot._BusyCapture("tests/web/test_response_metadata.py::test_child_cancel")
    warning_filter = snapshot._BusyWarningFilter(capture)
    private_path = str(tmp_path / "private-database-name.sqlite")
    record = logging.LogRecord(
        snapshot._STORE_LOGGER,
        logging.WARNING,
        private_path,
        31,
        snapshot._MESSAGES[snapshot._STORE_LOGGER],
        ("begin", "OperationalError", 5),
        None,
    )

    assert warning_filter.filter(record)
    assert len(capture.events) == 1
    assert sqlite3.connect is connect
    assert aiosqlite.Connection._execute is aio_execute
    assert warning_filter.filter(
        logging.LogRecord(
            snapshot._STORE_LOGGER,
            logging.WARNING,
            private_path,
            32,
            snapshot._MESSAGES[snapshot._STORE_LOGGER],
            ("begin", "OperationalError", 6),
            None,
        )
    )  # Filters keep logging behavior unchanged for unrelated warnings.
    assert len(capture.events) == 1


def test_snapshot_is_bounded_and_does_not_copy_sql_paths_or_record_arguments(tmp_path: Path) -> None:
    secret_sql = "UPDATE secret_table SET payload='PRIVATE_SQL_PAYLOAD'"
    secret_path = str(tmp_path / "PRIVATE_DB_PATH.sqlite")
    capture = snapshot._BusyCapture("tests/web/test_response_metadata.py::test_child_cancel")
    warning_filter = snapshot._BusyWarningFilter(capture)
    record = logging.LogRecord(
        snapshot._SESSION_LOGGER,
        logging.WARNING,
        secret_path,
        45,
        snapshot._MESSAGES[snapshot._SESSION_LOGGER],
        ("insert", 12.5, secret_sql, 5),
        None,
    )

    warning_filter.filter(record)
    payload = capture.payload()
    decoded = json.loads(payload)
    assert "PRIVATE_SQL_PAYLOAD" not in payload and "PRIVATE_DB_PATH.sqlite" not in payload
    assert decoded["mode"] == "busy_snapshot" and decoded["events"][0]["phase"] == "insert"
    event = decoded["events"][0]
    assert len(event["threads"]) <= snapshot._MAX_THREADS
    assert all(len(thread["frames"]) <= snapshot._MAX_DEPTH for thread in event["threads"])
    assert all(Path(frame["file"]).name == frame["file"] for thread in event["threads"] for frame in thread["frames"])
    assert all(len(task["frames"]) <= snapshot._MAX_DEPTH for task in event["tasks"])


def test_phase_metadata_is_owner_scoped_allowlisted_and_path_hashed(tmp_path: Path) -> None:
    database = tmp_path / "PRIVATE_DB_PATH.sqlite"
    owner = InboxStore(database, "session-id", 10)
    owner_metadata = snapshot._phase_owner(
        "nagents.channels.store",
        "execute",
        {"self": owner, "phase": "begin", "sql": "PRIVATE_SQL"},
    )
    assert owner_metadata == {
        "owner": "InboxStore",
        "phase": "begin",
        "database": hashlib.sha256(os.fsencode(database)).hexdigest()[:16],
    }
    assert str(database) not in json.dumps(owner_metadata)
    assert snapshot._phase_owner("nagents.channels.store", "execute", {"self": owner, "phase": "not-allowed"}) is None
    assert (
        snapshot._phase_owner("tests.test_sqlite_busy_snapshot", "execute", {"self": owner, "phase": "begin"}) is None
    )

    manager = SessionManager(database)
    manager_metadata = snapshot._phase_owner(
        "nagents.session.manager", "add_message", {"self": manager, "phase": "response_anchor"}
    )
    assert manager_metadata is not None and manager_metadata["owner"] == "SessionManager"
    assert manager_metadata["phase"] == "response_anchor"

    class HarnessSession(SessionManager):
        pass

    harness_owner = HarnessSession(database)
    subclass_metadata = snapshot._phase_owner(
        "nagents.session.manager", "add_message", {"self": harness_owner, "phase": "insert"}
    )
    assert subclass_metadata is not None and subclass_metadata["owner"] == "SessionManager"
    assert subclass_metadata["database"] == hashlib.sha256(os.fsencode(database)).hexdigest()[:16]

    class NonReadingSessionManager(SessionManager):
        def __getattribute__(self, name: str) -> object:
            if name == "db_path":
                raise AssertionError("Database path must be read from owned instance storage")
            return super().__getattribute__(name)

    nonreading_owner = object.__new__(NonReadingSessionManager)
    object.__setattr__(nonreading_owner, "db_path", database)
    safe_metadata = snapshot._phase_owner(
        "nagents.session.manager", "add_message", {"self": nonreading_owner, "phase": "updated_at"}
    )
    assert safe_metadata is not None and safe_metadata["database"] == subclass_metadata["database"]
    assert str(database) not in json.dumps(safe_metadata)


def test_busy_snapshot_finds_pending_tasks_on_a_worker_loop() -> None:
    loop = asyncio.new_event_loop()
    entered = threading.Event()
    thread_started = threading.Event()

    async def pending_writer() -> None:
        entered.set()
        await asyncio.Event().wait()

    def run_loop() -> None:
        asyncio.set_event_loop(loop)
        loop.call_soon(lambda: asyncio.create_task(pending_writer()))
        thread_started.set()
        loop.run_forever()

    worker = threading.Thread(target=run_loop)
    worker.start()
    try:
        assert thread_started.wait(HANG_GUARD)
        assert entered.wait(HANG_GUARD)
        thread_stacks, loops = snapshot._thread_stacks()
        assert len(thread_stacks) <= snapshot._MAX_THREADS
        assert any(candidate is loop for candidate in loops)
        tasks = snapshot._task_stacks(loops)
        assert len(tasks) == 1
        task_frames = cast("list[dict[str, object]]", tasks[0]["frames"])
        assert any(frame["function"] == "pending_writer" for frame in task_frames)
        assert all(len(cast("list[dict[str, object]]", task["frames"])) <= snapshot._MAX_DEPTH for task in tasks)
    finally:

        async def stop_loop() -> None:
            tasks = [task for task in asyncio.all_tasks(loop) if task is not asyncio.current_task()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            loop.stop()

        loop.call_soon_threadsafe(lambda: asyncio.create_task(stop_loop()))
        worker.join(HANG_GUARD)
        if worker.is_alive():
            loop.call_soon_threadsafe(loop.stop)
            worker.join(HANG_GUARD)
        loop.close()


def test_snapshot_failure_never_replaces_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    capture = snapshot._BusyCapture("tests/web/test_response_metadata.py::test_child_cancel")
    warning_filter = snapshot._BusyWarningFilter(capture)
    monkeypatch.setattr(snapshot, "_thread_stacks", lambda: (_ for _ in ()).throw(RuntimeError("capture failed")))
    record = logging.LogRecord(
        snapshot._STORE_LOGGER,
        logging.WARNING,
        "private/path.sqlite",
        31,
        snapshot._MESSAGES[snapshot._STORE_LOGGER],
        ("begin", "OperationalError", 5),
        None,
    )

    assert warning_filter.filter(record)
    assert capture.events == []


def test_busy_snapshot_report_section_is_failure_only() -> None:
    capture = snapshot._BusyCapture("tests/web/test_response_metadata.py::test_child_cancel")
    capture.add({"logger": snapshot._STORE_LOGGER, "sqlite_code": 5, "phase": "begin"})
    report = cast(
        "pytest.TestReport",
        SimpleNamespace(failed=False, sections=[]),
    )
    item = cast("pytest.Item", SimpleNamespace(nodeid=capture.nodeid))

    snapshot._append_report(report, item, capture)
    assert report.sections == []
    failed_report = cast(
        "pytest.TestReport",
        SimpleNamespace(failed=True, sections=[]),
    )
    snapshot._append_report(failed_report, item, capture)
    assert failed_report.sections[0][0] == "NGN_SQLITE_TRACE busy_snapshot"
    assert json.loads(failed_report.sections[0][1])["mode"] == "busy_snapshot"
