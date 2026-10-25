"""Bounded, failure-only Windows reruns for SQLite contention diagnostics."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from typing import TYPE_CHECKING

from tests.support.hang_guard import HANG_GUARD
from tests.support.sqlite_trace import _is_target

if TYPE_CHECKING:
    from collections.abc import Sequence

ROOT = Path(__file__).resolve().parents[2]
ITERATIONS = 20
TIMEOUT_SECONDS = 3 * HANG_GUARD + 30
PARALLEL_SHARD_ROUNDS = 3
PARALLEL_SHARD_TIMEOUT_SECONDS = 900
SHARD_FILES = Path("tests/support/sqlite_failed_shard1.txt")
_TEST_NAME = re.compile(r"^(test_[A-Za-z0-9_]+)(?:\[(.*)\])?$")
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_FAILURE_TYPE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
_BOOLEAN_PARAMS = re.compile(r"^(?:True|False)(?:-(?:True|False))*$")
_KNOWN_FAILURE_TYPES = frozenset(
    {
        "AssertionError",
        "AttributeError",
        "CancelledError",
        "Exception",
        "Failed",
        "ImportError",
        "IndexError",
        "KeyError",
        "NotImplementedError",
        "OSError",
        "RuntimeError",
        "StopAsyncIteration",
        "TimeoutError",
        "TypeError",
        "ValueError",
        "asyncio.CancelledError",
        "sqlite3.OperationalError",
    }
)
NODES = (
    "tests/web/test_response_metadata.py::test_child_cancel_keeps_scope_and_monotonic_sequence[False]",
    "tests/web/test_web_child_failure_scope.py::test_stop_still_cancels_chat_and_voice_trees[True]",
    "tests/web/test_web_child_failure_scope.py::test_recovered_root_keeps_child_failures_and_safe_scope_in_chat_and_voice[False-True]",
)


def pytest_command(nodeid: str) -> list[str]:
    """Build the only command this diagnostic runner permits."""
    if nodeid not in NODES:
        raise ValueError("SQLite diagnostic node is not allowlisted")
    return [
        sys.executable,
        "-m",
        "pytest",
        "--no-header",
        "--tb=short",
        "-q",
        "-n",
        "0",
        "-p",
        "tests.support.sqlite_trace",
        nodeid,
    ]


def parallel_shard_pytest_command(files: Sequence[str], *, busy_snapshot: bool = False, junit_xml: Path) -> list[str]:
    """Build the fixed four-worker pytest command for shard one of three."""
    if not files or len(set(files)) != len(files) or any(not _valid_shard_file(name) for name in files):
        raise ValueError("SQLite diagnostics require the fixed tests shard file list")
    plugin = "tests.support.sqlite_busy_snapshot" if busy_snapshot else "tests.support.sqlite_trace"
    return [
        sys.executable,
        "-m",
        "pytest",
        "--no-header",
        "--tb=short",
        "-q",
        "-n",
        "4",
        "-p",
        plugin,
        f"--junitxml={junit_xml}",
        *files,
    ]


def _valid_shard_file(name: str) -> bool:
    relative = Path(name)
    tests_root = (ROOT / "tests").resolve()
    candidate = (ROOT / relative).resolve()
    return (
        name.startswith("tests/")
        and relative.as_posix() == name
        and not relative.is_absolute()
        and ".." not in relative.parts
        and candidate.is_relative_to(tests_root)
    )


def safe_trace_sections(output: str | bytes | None, error_output: str | bytes | None = None) -> list[str]:
    """Extract only plugin JSON sections for registered targets or this shard."""

    def decode(value: str | bytes | None) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value or ""

    combined = f"{decode(output)}\n{decode(error_output)}"
    try:
        shard_files = frozenset(_shard_files())
    except (OSError, RuntimeError, ValueError):
        shard_files = frozenset()
    lines = iter(combined.splitlines())
    payloads: list[str] = []
    for line in lines:
        if "NGN_SQLITE_TRACE " not in line:
            continue
        for candidate in lines:
            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError:
                if candidate.strip():
                    break
                continue
            if isinstance(payload, dict) and isinstance(payload.get("test"), str):
                nodeid = payload["test"]
                path = nodeid.split("::", 1)[0]
                safe_mode = payload.get("mode") == "busy_snapshot" and path in shard_files
                safe_target = payload.get("mode") != "busy_snapshot" and _is_target(nodeid)
                if safe_mode or safe_target:
                    payloads.append(json.dumps(payload, separators=(",", ":")))
            break
    return payloads


def safe_junit_failures(report_path: Path) -> list[dict[str, str]]:
    """Read failure identifiers and exception types, never report messages."""
    try:
        root = ElementTree.parse(report_path).getroot()
    except (ElementTree.ParseError, OSError):
        return []
    failures: list[dict[str, str]] = []
    for testcase in root.iter("testcase"):
        failure = testcase.find("failure")
        if failure is None:
            failure = testcase.find("error")
        if failure is None:
            continue
        raw_classname = testcase.attrib.get("classname", "")
        module = next(
            (part for part in raw_classname.split(".") if part.startswith("test_") and _IDENTIFIER.fullmatch(part)),
            "test_unknown",
        )
        raw_name = testcase.attrib.get("name", "")
        matched = _TEST_NAME.fullmatch(raw_name)
        test_name = matched.group(1) if matched is not None else "test_unknown"
        params = matched.group(2) if matched is not None else None
        if params is not None:
            safe_params = (
                params if _BOOLEAN_PARAMS.fullmatch(params) else hashlib.sha256(params.encode()).hexdigest()[:10]
            )
        else:
            safe_params = ""
        raw_type = failure.attrib.get("type", "").split(":", 1)[0]
        if raw_type:
            failure_type = raw_type if _FAILURE_TYPE.fullmatch(raw_type) else "OtherFailure"
        else:
            message_prefix, separator, _message_body = failure.attrib.get("message", "").partition(":")
            failure_type = (
                message_prefix.strip()
                if separator and message_prefix.strip() in _KNOWN_FAILURE_TYPES
                else "OtherFailure"
            )
        failures.append(
            {
                "module": module,
                "test": test_name,
                "params": safe_params,
                "failure_type": failure_type,
            }
        )
        if len(failures) >= 50:
            break
    return failures


def _run() -> int:
    environment = os.environ.copy()
    environment["NGN_SQLITE_TRACE"] = "1"
    for iteration in range(1, ITERATIONS + 1):
        for nodeid in NODES:
            try:
                result = subprocess.run(
                    pytest_command(nodeid),
                    cwd=ROOT,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=TIMEOUT_SECONDS,
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                print(f"TIMEOUT iteration={iteration}/{ITERATIONS} node={nodeid}")
                for payload in safe_trace_sections(error.stdout, error.stderr):
                    print(payload)
                return 1
            if result.returncode != 0:
                print(f"FAIL iteration={iteration}/{ITERATIONS} node={nodeid} exit={result.returncode}")
                for payload in safe_trace_sections(result.stdout, result.stderr):
                    print(payload)
                return 1
            print(f"PASS iteration={iteration}/{ITERATIONS} node={nodeid}")
    print(f"PASS all {ITERATIONS} rounds of {len(NODES)} fixed SQLite diagnostic nodes")
    return 0


def _shard_files() -> list[str]:
    files = SHARD_FILES.read_text(encoding="utf-8").splitlines()
    if not files or any(not _valid_shard_file(name) for name in files) or len(set(files)) != len(files):
        raise RuntimeError("Fixed shard-one file selection returned an invalid file list")
    return files


def _run_parallel_shard(*, busy_snapshot: bool = False) -> int:
    environment = os.environ.copy()
    environment["NGN_SQLITE_TRACE"] = "0" if busy_snapshot else "1"
    environment["NGN_SQLITE_BUSY_SNAPSHOT"] = "1" if busy_snapshot else "0"
    try:
        files = _shard_files()
    except (OSError, RuntimeError, ValueError):
        print("FAIL fixed shard-one selection")
        return 1
    with tempfile.TemporaryDirectory(prefix="ngn-sqlite-busy-") as report_directory:
        report_root = Path(report_directory)
        for run in range(1, PARALLEL_SHARD_ROUNDS + 1):
            junit_xml = report_root / f"round-{run}.xml"
            try:
                command = parallel_shard_pytest_command(files, busy_snapshot=busy_snapshot, junit_xml=junit_xml)
                result = subprocess.run(
                    command,
                    cwd=ROOT,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=PARALLEL_SHARD_TIMEOUT_SECONDS,
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                print(f"TIMEOUT shard-round={run}/{PARALLEL_SHARD_ROUNDS}")
                for failure in safe_junit_failures(junit_xml):
                    print(
                        "CASE "
                        f"module={failure['module']} test={failure['test']} "
                        f"params={failure['params'] or '-'} failure_type={failure['failure_type']}"
                    )
                for payload in safe_trace_sections(error.stdout, error.stderr):
                    print(payload)
                return 1
            if result.returncode != 0:
                print(f"FAIL shard-round={run}/{PARALLEL_SHARD_ROUNDS} exit={result.returncode}")
                for failure in safe_junit_failures(junit_xml):
                    print(
                        "CASE "
                        f"module={failure['module']} test={failure['test']} "
                        f"params={failure['params'] or '-'} failure_type={failure['failure_type']}"
                    )
                for payload in safe_trace_sections(result.stdout, result.stderr):
                    print(payload)
                return 1
            print(f"PASS shard-round={run}/{PARALLEL_SHARD_ROUNDS} files={len(files)}")
    return 0


if __name__ == "__main__":
    arguments = sys.argv[1:]
    if arguments == ["--parallel-shard"]:
        raise SystemExit(_run_parallel_shard())
    if arguments == ["--busy-snapshot"]:
        raise SystemExit(_run_parallel_shard(busy_snapshot=True))
    if arguments:
        raise SystemExit("Only fixed SQLite diagnostic modes are supported")
    raise SystemExit(_run())
