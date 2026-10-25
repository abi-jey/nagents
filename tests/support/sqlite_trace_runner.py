"""Bounded, failure-only Windows reruns for SQLite contention diagnostics."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tests.support.hang_guard import HANG_GUARD

ROOT = Path(__file__).resolve().parents[2]
ITERATIONS = 20
TIMEOUT_SECONDS = 3 * HANG_GUARD + 30
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


def safe_trace_sections(output: str | bytes | None, error_output: str | bytes | None = None) -> list[str]:
    """Extract only plugin JSON sections for the fixed diagnostic nodes."""

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
            if isinstance(payload, dict) and payload.get("test") in NODES:
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


if __name__ == "__main__":
    raise SystemExit(_run())
