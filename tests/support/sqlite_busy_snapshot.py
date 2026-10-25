"""Failure-only stack snapshots for bounded SQLite BUSY warnings.

Unlike ``sqlite_trace``, this plugin does not wrap or instrument SQLite calls.
It is enabled only with ``NGN_SQLITE_BUSY_SNAPSHOT=1`` and captures metadata
when the two owned SQLite components log ``sqlite_code=5``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
import threading
from contextlib import suppress
from pathlib import Path
from types import AsyncGeneratorType
from types import CoroutineType
from types import FrameType
from types import GeneratorType
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Generator

    from pluggy import Result

_STORE_LOGGER = "nagents.channels.store"
_SESSION_LOGGER = "nagents.session.manager"
_MESSAGES = {
    _STORE_LOGGER: "SQLite transaction failed: phase=%s exception=%s sqlite_code=%s",
    _SESSION_LOGGER: "Session message write failed: phase=%s elapsed_ms=%.1f exception=%s sqlite_code=%s",
}
_PHASES = frozenset({"connect", "begin", "body", "commit", "close", "insert", "updated_at", "response_anchor"})
_MAX_EVENTS = 4
_MAX_THREADS = 64
_MAX_DEPTH = 12
_ROOT = Path(__file__).resolve().parents[2]
_SHARD_FILE = _ROOT / "tests" / "support" / "sqlite_failed_shard1.txt"
try:
    _SHARD_TEST_FILES = frozenset(_SHARD_FILE.read_text(encoding="utf-8").splitlines())
except OSError:
    _SHARD_TEST_FILES = frozenset()


def _phase_owner(module: str, function: str, local_values: dict[str, object]) -> dict[str, str] | None:
    """Return only a whitelisted phase and a hash from a known owner object."""
    owner = local_values.get("self")
    phase = local_values.get("phase")
    if not isinstance(phase, str) or phase not in _PHASES or owner is None:
        return None
    expected = {
        ("nagents.channels.store", "execute"): ("nagents.channels.store", "InboxStore"),
        ("nagents.session.manager", "add_message"): ("nagents.session.manager", "SessionManager"),
    }.get((module, function))
    if expected is None or not any((base.__module__, base.__name__) == expected for base in type(owner).__mro__):
        return None
    owner_details = {"owner": expected[1], "phase": phase}
    try:
        database = vars(owner).get("db_path")
    except TypeError:
        database = None
    if isinstance(database, (Path, str, bytes)):
        with suppress(OSError, TypeError, ValueError):
            owner_details["database"] = hashlib.sha256(os.fsencode(database)).hexdigest()[:16]
    return owner_details


def _frame_summary(frame: FrameType) -> dict[str, object]:
    code = frame.f_code
    result: dict[str, object] = {
        "file": os.path.basename(code.co_filename)[:80],
        "function": code.co_name[:80],
        "line": frame.f_lineno,
    }
    owner = _phase_owner(frame.f_globals.get("__name__", ""), code.co_name, frame.f_locals)
    if owner is not None:
        result["owner"] = owner
    return result


def _thread_stacks() -> tuple[list[dict[str, object]], list[asyncio.AbstractEventLoop]]:
    """Read bounded thread frames and retain, but never serialize, loop objects."""
    frames = sys._current_frames()
    items = list(frames.items())[:_MAX_THREADS]
    stacks: list[dict[str, object]] = []
    loops: dict[int, asyncio.AbstractEventLoop] = {}
    try:
        for thread_id, frame in items:
            functions: list[dict[str, object]] = []
            current: FrameType | None = frame
            scanned = 0
            try:
                while current is not None and scanned < _MAX_DEPTH * 4:
                    if current.f_code.co_name in {"_run_once", "run_forever", "run_until_complete"}:
                        candidate = current.f_locals.get("self")
                        if isinstance(candidate, asyncio.BaseEventLoop):
                            loops.setdefault(id(candidate), candidate)
                    if len(functions) < _MAX_DEPTH:
                        functions.append(_frame_summary(current))
                    scanned += 1
                    current = current.f_back
            finally:
                del current
                del frame
            stacks.append({"thread": thread_id, "frames": functions})
        return stacks, list(loops.values())[:_MAX_THREADS]
    finally:
        del items
        frames.clear()
        del frames


def _await_chain_frames(awaitable: object) -> list[dict[str, object]]:
    frames: list[dict[str, object]] = []
    visited: set[int] = set()
    current = awaitable
    while len(frames) < _MAX_DEPTH and id(current) not in visited:
        visited.add(id(current))
        if isinstance(current, CoroutineType):
            frame = current.cr_frame
            current = current.cr_await
        elif isinstance(current, GeneratorType):
            frame = current.gi_frame
            current = current.gi_yieldfrom
        elif isinstance(current, AsyncGeneratorType):
            frame = current.ag_frame
            current = current.ag_await
        else:
            break
        if frame is not None:
            frames.append(_frame_summary(frame))
        if current is None:
            break
    return frames


def _task_stacks(loops: list[asyncio.AbstractEventLoop]) -> list[dict[str, object]]:
    tasks_seen: set[int] = set()
    result: list[dict[str, object]] = []
    for loop in loops[:_MAX_THREADS]:
        try:
            # Cross-thread reads use only CPython's GIL-protected task/frame
            # objects and are bounded; races with loop shutdown are skipped.
            tasks = asyncio.all_tasks(loop)
        except (RuntimeError, TypeError):
            continue
        for task in tasks:
            if len(result) >= _MAX_THREADS:
                return result
            task_id = id(task)
            if task_id in tasks_seen:
                continue
            tasks_seen.add(task_id)
            try:
                if task.done():
                    continue
                frames = _await_chain_frames(task.get_coro())
            except (AttributeError, RuntimeError, TypeError):
                continue
            if frames:
                result.append({"frames": frames})
    return result


def _busy_warning(record: logging.LogRecord) -> tuple[str, int] | None:
    if record.levelno != logging.WARNING or record.name not in _MESSAGES:
        return None
    if record.msg != _MESSAGES[record.name] or not isinstance(record.args, tuple):
        return None
    expected_length = 3 if record.name == _STORE_LOGGER else 4
    if len(record.args) != expected_length:
        return None
    phase = record.args[0]
    code = record.args[-1]
    if not isinstance(phase, str) or phase not in _PHASES or type(code) is not int or code != 5:
        return None
    return phase, code


class _BusyWarningFilter(logging.Filter):
    def __init__(self, capture: _BusyCapture) -> None:
        super().__init__()
        self.capture = capture

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            warning = _busy_warning(record)
        except Exception:
            return True
        if warning is None:
            return True
        phase, code = warning
        event: dict[str, object] = {
            "logger": record.name,
            "phase": phase,
            "sqlite_code": code,
        }
        try:
            threads, loops = _thread_stacks()
            event["threads"] = threads
            event["tasks"] = _task_stacks(loops)
        except Exception as error:
            event["snapshot_error"] = type(error).__name__[:80]
        with suppress(Exception):
            self.capture.add(event)
        return True


class _BusyCapture:
    def __init__(self, nodeid: str) -> None:
        self.nodeid = nodeid
        self.events: list[dict[str, object]] = []
        self.filter = _BusyWarningFilter(self)
        self._lock = threading.Lock()
        self.reported = False

    def add(self, event: dict[str, object]) -> None:
        with self._lock:
            if len(self.events) < _MAX_EVENTS:
                self.events.append(event)

    def payload(self) -> str:
        with self._lock:
            return json.dumps(
                {"mode": "busy_snapshot", "test": self.nodeid, "events": list(self.events)},
                separators=(",", ":"),
            )


_ACTIVE: _BusyCapture | None = None
_LOGGERS = (_STORE_LOGGER, _SESSION_LOGGER)


def _enabled() -> bool:
    return os.environ.get("NGN_SQLITE_BUSY_SNAPSHOT") == "1"


def _is_shard_test(nodeid: str) -> bool:
    return nodeid.split("::", 1)[0] in _SHARD_TEST_FILES


def _append_report(report: pytest.TestReport, item: pytest.Item, capture: _BusyCapture) -> None:
    if not report.failed or capture.nodeid != item.nodeid or capture.reported:
        return
    with capture._lock:
        has_events = bool(capture.events)
        capture.reported = True
    if has_events:
        report.sections.append(("NGN_SQLITE_TRACE busy_snapshot", capture.payload()))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None) -> Generator[None, Result[bool], None]:
    global _ACTIVE
    if not _enabled() or not _is_shard_test(item.nodeid):
        yield
        return
    capture = _BusyCapture(item.nodeid)
    _ACTIVE = capture
    for name in _LOGGERS:
        logging.getLogger(name).addFilter(capture.filter)
    try:
        yield
    finally:
        for name in _LOGGERS:
            logging.getLogger(name).removeFilter(capture.filter)
        _ACTIVE = None


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[object]
) -> Generator[None, Result[pytest.TestReport], None]:
    outcome = yield
    report = outcome.get_result()
    capture = _ACTIVE
    if capture is not None:
        _append_report(report, item, capture)
