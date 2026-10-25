"""Safety checks for the bounded Windows SQLite diagnostic runner."""

from __future__ import annotations

import json
import sys

import pytest

from tests.support import sqlite_trace_runner
from tests.support.hang_guard import HANG_GUARD
from tests.support.sqlite_trace import _is_target


def test_diagnostic_command_is_fixed_and_trace_targets_are_registered() -> None:
    assert sqlite_trace_runner.ITERATIONS == 20
    assert sqlite_trace_runner.TIMEOUT_SECONDS > HANG_GUARD * 2
    assert len(sqlite_trace_runner.NODES) == 3
    for nodeid in sqlite_trace_runner.NODES:
        command = sqlite_trace_runner.pytest_command(nodeid)
        assert command == [
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
        assert _is_target(nodeid)
    with pytest.raises(ValueError, match="not allowlisted"):
        sqlite_trace_runner.pytest_command("arbitrary shell input")


def test_failure_output_keeps_only_allowlisted_trace_payloads() -> None:
    payload = {
        "test": sqlite_trace_runner.NODES[0],
        "chunk": 1,
        "events": [{"event": "operation_error", "sqlite_code": 5}],
    }
    output = (
        """FAILURE PATH C:/private/test.py
SQL UPDATE private_table SET secret='payload'
================ NGN_SQLITE_TRACE 1/1 ================
"""
        + json.dumps(payload)
        + """
================ short test summary info ================
unsafe traceback content
"""
    )

    assert sqlite_trace_runner.safe_trace_sections(output) == [json.dumps(payload, separators=(",", ":"))]
    assert sqlite_trace_runner.safe_trace_sections("SQL only, no trace section") == []
