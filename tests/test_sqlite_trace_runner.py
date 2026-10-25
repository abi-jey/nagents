"""Safety checks for the bounded Windows SQLite diagnostic runner."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from tests.support import sqlite_trace_runner
from tests.support.hang_guard import HANG_GUARD
from tests.support.sqlite_trace import _is_target


def test_diagnostic_command_is_fixed_and_trace_targets_are_registered() -> None:
    assert sqlite_trace_runner.ITERATIONS == 20
    assert sqlite_trace_runner.TIMEOUT_SECONDS > HANG_GUARD * 2
    assert len(sqlite_trace_runner.NODES) == 3
    assert sqlite_trace_runner.PARALLEL_SHARD_ROUNDS == 3
    assert sqlite_trace_runner.PARALLEL_SHARD_TIMEOUT_SECONDS == 900
    for nodeid in sqlite_trace_runner.NODES:
        command = sqlite_trace_runner.pytest_command(nodeid)
        assert command == [
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
        assert _is_target(nodeid)
    with pytest.raises(ValueError, match="not allowlisted"):
        sqlite_trace_runner.pytest_command("arbitrary shell input")
    shard_files = sqlite_trace_runner._shard_files()
    assert len(shard_files) == 103
    assert len(set(shard_files)) == len(shard_files)
    assert all(
        sqlite_trace_runner._valid_shard_file(name) and (sqlite_trace_runner.ROOT / name).is_file()
        for name in shard_files
    )
    assert "tests/web/test_response_metadata.py" in shard_files
    assert "tests/channels/test_channels_runtime.py" in shard_files
    files = ["tests/example_test.py"]
    report_path = Path("private-runner-directory/round-1.xml")
    assert sqlite_trace_runner.parallel_shard_pytest_command(files, junit_xml=report_path) == [
        sys.executable,
        "-m",
        "pytest",
        "--no-header",
        "--tb=short",
        "-q",
        "-n",
        "4",
        "-p",
        "tests.support.sqlite_trace",
        f"--junitxml={report_path}",
        "tests/example_test.py",
    ]
    assert sqlite_trace_runner.parallel_shard_pytest_command(files, busy_snapshot=True, junit_xml=report_path) == [
        sys.executable,
        "-m",
        "pytest",
        "--no-header",
        "--tb=short",
        "-q",
        "-n",
        "4",
        "-p",
        "tests.support.sqlite_busy_snapshot",
        f"--junitxml={report_path}",
        "tests/example_test.py",
    ]
    with pytest.raises(ValueError, match="fixed tests shard"):
        sqlite_trace_runner.parallel_shard_pytest_command(["../arbitrary.py"], junit_xml=report_path)


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

    other_target = {
        "test": "tests/web/test_web_settings.py::test_settings_cancellation_joins_transaction_and_keeps_get_consistent",
        "events": [{"event": "busy_thread_stacks", "threads": []}],
    }
    other_output = "========== NGN_SQLITE_TRACE 1/1 ==========\n" + json.dumps(other_target)
    assert sqlite_trace_runner.safe_trace_sections(other_output) == [json.dumps(other_target, separators=(",", ":"))]

    shard_snapshot = {
        "mode": "busy_snapshot",
        "test": "tests/web/test_web.py::test_unlisted_by_old_trace_targets",
        "events": [{"logger": "nagents.channels.store", "sqlite_code": 5}],
    }
    shard_output = "========== NGN_SQLITE_TRACE busy_snapshot ==========\n" + json.dumps(shard_snapshot)
    assert sqlite_trace_runner.safe_trace_sections(shard_output) == [json.dumps(shard_snapshot, separators=(",", ":"))]

    outside_shard = {**shard_snapshot, "test": "tests/test_sqlite_trace_runner.py::test_not_selected"}
    outside_output = "========== NGN_SQLITE_TRACE busy_snapshot ==========\n" + json.dumps(outside_shard)
    assert sqlite_trace_runner.safe_trace_sections(outside_output) == []


def test_junit_failure_summary_keeps_only_safe_identifiers_and_type(tmp_path: Path) -> None:
    report = tmp_path / "private-runner.xml"
    report.write_text(
        """<testsuite>
<testcase classname="tests.web.test_response_metadata" name="test_child_cancel[False]">
<failure message="PRIVATE_SQL_MESSAGE" type="sqlite3.OperationalError">PRIVATE_DB_PATH and SQL</failure>
</testcase>
<testcase classname="tests.web.test_web" name="test_unrelated[PRIVATE_PARAM]">
<failure message="PRIVATE_MESSAGE" type="RuntimeError">PRIVATE_PATH</failure>
</testcase>
<testcase classname="tests.web.test_web" name="test_bare_private_type">
<failure message="PRIVATE_FAILURE_BODY">PRIVATE_PATH</failure>
</testcase>
</testsuite>""",
        encoding="utf-8",
    )
    summaries = sqlite_trace_runner.safe_junit_failures(report)
    assert len(summaries) == 3
    assert summaries[0] == {
        "module": "test_response_metadata",
        "test": "test_child_cancel",
        "params": "False",
        "failure_type": "sqlite3.OperationalError",
    }
    assert summaries[1] == {
        "module": "test_web",
        "test": "test_unrelated",
        "params": hashlib.sha256(b"PRIVATE_PARAM").hexdigest()[:10],
        "failure_type": "RuntimeError",
    }
    assert summaries[2] == {
        "module": "test_web",
        "test": "test_bare_private_type",
        "params": "",
        "failure_type": "OtherFailure",
    }
    assert "PRIVATE" not in json.dumps(summaries)
