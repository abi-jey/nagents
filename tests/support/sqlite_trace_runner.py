"""Bounded, failure-only Windows reruns for SQLite contention diagnostics."""

from __future__ import annotations

import json
import os
import subprocess
import sys
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
        "--tb=no",
        "-q",
        "-n",
        "0",
        "-p",
        "tests.support.sqlite_trace",
        nodeid,
    ]


def parallel_shard_pytest_command(files: Sequence[str]) -> list[str]:
    """Build the fixed four-worker pytest command for shard one of three."""
    if not files or len(set(files)) != len(files) or any(not _valid_shard_file(name) for name in files):
        raise ValueError("SQLite diagnostics require the fixed tests shard file list")
    return [
        sys.executable,
        "-m",
        "pytest",
        "--no-header",
        "--tb=no",
        "-q",
        "-n",
        "4",
        "-p",
        "tests.support.sqlite_trace",
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
    """Extract only plugin JSON sections for any registered trace target."""

    def decode(value: str | bytes | None) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value or ""

    combined = f"{decode(output)}\n{decode(error_output)}"
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
            if isinstance(payload, dict) and isinstance(payload.get("test"), str) and _is_target(payload["test"]):
                payloads.append(json.dumps(payload, separators=(",", ":")))
            break
    return payloads


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


def _run_parallel_shard() -> int:
    environment = os.environ.copy()
    environment["NGN_SQLITE_TRACE"] = "1"
    try:
        files = _shard_files()
        command = parallel_shard_pytest_command(files)
    except (OSError, RuntimeError, ValueError):
        print("FAIL fixed shard-one selection")
        return 1
    for run in range(1, PARALLEL_SHARD_ROUNDS + 1):
        try:
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
            for payload in safe_trace_sections(error.stdout, error.stderr):
                print(payload)
            return 1
        if result.returncode != 0:
            print(f"FAIL shard-round={run}/{PARALLEL_SHARD_ROUNDS} exit={result.returncode}")
            for payload in safe_trace_sections(result.stdout, result.stderr):
                print(payload)
            return 1
        print(f"PASS shard-round={run}/{PARALLEL_SHARD_ROUNDS} files={len(files)}")
    return 0


if __name__ == "__main__":
    arguments = sys.argv[1:]
    if arguments == ["--parallel-shard"]:
        raise SystemExit(_run_parallel_shard())
    if arguments:
        raise SystemExit("Only the fixed --parallel-shard mode is supported")
    raise SystemExit(_run())
