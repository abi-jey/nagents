from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from benchmarks.diagnostics import prepare
from benchmarks.diagnostics.audit import audit_events
from benchmarks.diagnostics.audit import audit_trial

if TYPE_CHECKING:
    from collections.abc import Callable


def complete_trace() -> list[dict[str, object]]:
    return [
        {"event": "benchmark_start", "session_id": "ngn-fixture"},
        {"event": "tool_call", "id": "edit1", "name": "edit", "arguments": {"path": "/app/report.py"}},
        {"event": "tool_result", "id": "edit1", "name": "edit", "result": {"path": "report.py", "changed": True}},
        {
            "event": "tool_call",
            "id": "test1",
            "name": "shell",
            "arguments": {"command": "python3 -m unittest -v test_basic"},
        },
        {
            "event": "tool_result",
            "id": "test1",
            "name": "shell",
            "result": {
                "exit_code": 0,
                "timed_out": False,
                "truncated": False,
                "output": "Ran 2 tests in 0.001s\n\nOK\n",
            },
        },
        {"event": "tool_call", "id": "delegate", "name": "delegate", "arguments": {"agent": "assistant"}},
        {"event": "task_started", "task_id": "child", "parent_task_id": "", "depth": 1},
        {
            "event": "tool_result",
            "id": "delegate",
            "name": "delegate",
            "result": {"task_id": "child", "status": "running"},
        },
        {
            "event": "task_completed",
            "task_id": "child",
            "parent_task_id": "",
            "depth": 1,
            "status": "completed",
            "error": "",
        },
        {"event": "task_notification", "source_task_id": "child", "recipient_task_id": "", "cause": "completion"},
        {"event": "tool_call", "id": "edit2", "name": "edit", "arguments": {"path": "/app/report.py"}},
        {"event": "tool_result", "id": "edit2", "name": "edit", "result": {"path": "report.py", "changed": True}},
        {
            "event": "tool_call",
            "id": "test2",
            "name": "shell",
            "arguments": {"command": "python3 -m unittest discover -v"},
        },
        {
            "event": "tool_result",
            "id": "test2",
            "name": "shell",
            "result": {
                "exit_code": 0,
                "timed_out": False,
                "truncated": False,
                "output": "Ran 2 tests in 0.001s\n\nOK\n",
            },
        },
        {"event": "done", "finish_reason": "stop", "session_id": "ngn-fixture"},
        {
            "event": "benchmark_task_history",
            "task": {"id": "child", "parent_task_id": "", "depth": 1, "status": "completed", "error": ""},
            "messages": [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {"id": "read-report", "name": "read_file", "arguments": {"path": "/app/report.py"}},
                        {"id": "read-review", "name": "read_file", "arguments": {"path": "/app/REVIEW.md"}},
                    ],
                },
                {"role": "tool", "name": "read_file", "tool_call_id": "read-report", "content": "report file contents"},
                {"role": "tool", "name": "read_file", "tool_call_id": "read-review", "content": "review file contents"},
            ],
        },
        {"event": "benchmark_end", "status": "completed"},
    ]


def test_workflow_requires_action_after_direct_review() -> None:
    rows = complete_trace()
    assert audit_events(rows)["workflow_passed"] is True
    acknowledgement_only = [row for row in rows if row.get("id") not in {"edit2", "test2"}]
    assert audit_events(acknowledgement_only)["workflow_passed"] is False
    missing_notification = [row for row in rows if row.get("event") != "task_notification"]
    assert audit_events(missing_notification)["workflow_passed"] is False


@pytest.mark.parametrize("recoverable", [True, False, "missing"])
def test_recovered_provider_observations_remain_visible_without_failing_the_workflow(recoverable: bool | str) -> None:
    rows = complete_trace()
    error: dict[str, object] = {"event": "error", "message": "Provider observation"}
    if recoverable != "missing":
        error["recoverable"] = recoverable
    rows.insert(1, error)
    report = audit_events(rows)
    assert report["workflow_passed"] is (recoverable is True)
    assert bool(report["recoverable_errors"]) is (recoverable is True)
    assert bool(report["failures"]) is (recoverable is not True)


@pytest.mark.parametrize("ending", ["missing", "foreign", "length", "compaction", "cancelled"])
def test_successful_completion_cannot_be_borrowed_from_an_unrelated_or_incomplete_turn(ending: str) -> None:
    rows = complete_trace()
    position = next(index for index, row in enumerate(rows) if row.get("event") == "done")
    if ending == "missing":
        rows.pop(position)
    elif ending == "foreign":
        rows[position]["session_id"] = "other-root"
    elif ending == "length":
        rows[position]["finish_reason"] = "length"
    elif ending == "compaction":
        rows.insert(position, {"event": "compaction_started"})
        rows.insert(position + 2, {"event": "compaction_done"})
    else:
        rows.insert(position, {"event": "benchmark_cancelled"})
    assert audit_events(rows)["workflow_passed"] is False


@pytest.mark.parametrize(
    ("source", "recipient"), [("grandchild", "child"), ("grandchild", ""), ("child", "other-child")]
)
def test_nested_or_wrong_recipient_notification_is_not_a_main_review(source: str, recipient: str) -> None:
    rows = complete_trace()
    index = next(index for index, row in enumerate(rows) if row.get("event") == "task_notification")
    rows[index] = {**rows[index], "source_task_id": source, "recipient_task_id": recipient}
    audited = audit_events(rows)
    assert audited["workflow_passed"] is False
    assert audited["review_notification_lines"] == []


def test_inspection_requires_native_ack_and_rejects_child_mutation() -> None:
    rows = complete_trace()
    assert (
        audit_events([row for row in rows if row.get("id") != "delegate" or row.get("event") != "tool_result"])[
            "workflow_passed"
        ]
        is False
    )
    index = next(index for index, row in enumerate(rows) if row.get("event") == "benchmark_task_history")
    rows[index] = {
        "event": "benchmark_task_history",
        "task": {"id": "child", "parent_task_id": "", "depth": 1, "status": "completed", "error": ""},
        "messages": [{"tool_calls": [{"name": "edit"}]}],
    }
    assert audit_events(rows)["workflow_passed"] is False


def test_real_failed_unittest_masked_by_true_is_not_success() -> None:
    command = "python3 -B -m unittest -v test_basic; true"
    process = subprocess.run(
        ["/bin/sh", "-c", command],
        cwd=prepare.TASK_SOURCE / "environment/app",
        env={**os.environ, "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        check=True,
    )
    output = process.stdout + process.stderr
    assert process.returncode == 0 and "FAILED" in output
    rows = complete_trace()
    for row in rows:
        if row.get("name") != "shell":
            continue
        if row.get("event") == "tool_call":
            row["arguments"] = {"command": command}
        else:
            row["result"] = {"exit_code": 0, "timed_out": False, "truncated": False, "output": output}
    report = audit_events(rows)
    assert report["workflow_passed"] is False
    assert report["manual_confirmation_required"]
    assert report["failures"] == []  # Unsupported/test-failure evidence is not a Harness crash.


@pytest.mark.parametrize(
    "command",
    [
        "cd /app && python -m unittest -v test_basic",
        "python3 -B -m unittest discover -q",
        "PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v test_basic.py",
    ],
)
def test_supported_unittest_forms_with_actual_summary(command: str) -> None:
    rows = complete_trace()
    for row in rows:
        if row.get("event") == "tool_call" and row.get("name") == "shell":
            row["arguments"] = {"command": command}
    assert audit_events(rows)["workflow_passed"] is True


@pytest.mark.parametrize(
    "output",
    [
        "",
        "Ran 0 tests in 0.001s\n\nOK\n",
        "Ran 2 tests in 0.001s\n\nFAILED (failures=1)\n",
        "Ran 2 tests in 0.001s\n\nOK (skipped=2)\n",
        "FAILED (failures=1)\nRan 2 tests in 0.001s\n\nOK\n",
    ],
)
def test_exit_zero_requires_a_nonzero_successful_unittest_summary(output: str) -> None:
    rows = complete_trace()
    for row in rows:
        if row.get("event") == "tool_result" and row.get("name") == "shell":
            row["result"] = {"exit_code": 0, "timed_out": False, "truncated": False, "output": output}
    report = audit_events(rows)
    assert report["workflow_passed"] is False
    assert report["failures"] == []


def test_an_earlier_passing_run_cannot_mask_a_later_failed_test() -> None:
    rows = complete_trace()
    done = next(index for index, row in enumerate(rows) if row.get("event") == "done")
    rows[done:done] = [
        {
            "event": "tool_call",
            "id": "failed-last",
            "name": "shell",
            "arguments": {"command": "python3 -m unittest discover -v"},
        },
        {
            "event": "tool_result",
            "id": "failed-last",
            "name": "shell",
            "result": {
                "exit_code": 1,
                "timed_out": False,
                "truncated": False,
                "output": "Ran 2 tests in 0.001s\n\nFAILED (failures=1)\n",
            },
        },
    ]
    report = audit_events(rows)
    assert report["workflow_passed"] is False
    assert report["failures"] == []


def test_failed_review_cannot_borrow_an_unrelated_grandchild_history() -> None:
    rows = complete_trace()
    for row in rows:
        if row.get("event") == "task_completed":
            row.update(status="failed", error="Subagent provider failed")
        if row.get("event") == "benchmark_task_history":
            row["task"] = {
                "id": "grandchild",
                "parent_task_id": "child",
                "depth": 2,
                "status": "completed",
                "error": "",
            }
    rows.insert(-1, {"event": "benchmark_task_history_error", "task_id": "child", "class": "RuntimeError"})
    report = audit_events(rows)
    assert report["workflow_passed"] is False
    assert report["review_notification_lines"] == []
    assert report["history_read_errors"]


@pytest.mark.parametrize("problem", ["empty", "missing", "wrong_id", "failed", "read_error", "no_completion"])
def test_required_review_needs_its_own_completed_native_inspection(problem: str) -> None:
    rows = complete_trace()
    history = next(row for row in rows if row.get("event") == "benchmark_task_history")
    if problem == "empty":
        history["messages"] = []
    elif problem == "missing":
        rows.remove(history)
    elif problem == "wrong_id":
        history["task"] = {"id": "different", "parent_task_id": "", "depth": 1, "status": "completed", "error": ""}
    elif problem == "failed":
        history["task"] = {"id": "child", "parent_task_id": "", "depth": 1, "status": "failed", "error": "failed"}
    elif problem == "read_error":
        messages = history["messages"]
        assert isinstance(messages, list)
        messages[1]["content"] = "Error: File could not be read"
    else:
        rows = [row for row in rows if row.get("event") != "task_completed"]
    assert audit_events(rows)["workflow_passed"] is False


@pytest.mark.parametrize(
    "path", ["/app/unrelated/report.py", "unrelated/report.py", {"path": "report.py"}, ["report.py"]]
)
def test_report_edit_requires_exact_target_and_malformed_paths_do_not_crash(path: object) -> None:
    rows = complete_trace()
    edit = next(row for row in rows if row.get("event") == "tool_call" and row.get("id") == "edit2")
    edit["arguments"] = {"path": path}
    assert audit_events(rows)["workflow_passed"] is False


def test_malformed_path_in_required_child_history_does_not_crash() -> None:
    rows = complete_trace()
    history = next(row for row in rows if row.get("event") == "benchmark_task_history")
    messages = history["messages"]
    assert isinstance(messages, list)
    messages[0]["tool_calls"][0]["arguments"]["path"] = {"bad": "report.py"}
    messages[1]["content"] = "Error: arguments.path must be string"
    assert audit_events(rows)["workflow_passed"] is False


def test_audit_hashes_completed_artifacts_but_never_follows_symlinks(tmp_path: Path) -> None:
    (tmp_path / "agent").mkdir()
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "report.py").write_text("observed artifact")
    private = tmp_path / "not-an-artifact"
    private.write_text("do not hash me")
    (artifacts / "link").symlink_to(private)
    (tmp_path / "result.json").write_text(json.dumps({"finished_at": "2026-01-01T00:00:00", "verifier_result": None}))
    (tmp_path / "agent/events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in complete_trace()))
    report = audit_trial(tmp_path)
    assert report["workflow_passed"] is True
    assert report["artifact_sha256"] == {"report.py": hashlib.sha256(b"observed artifact").hexdigest()}
    assert report["unhashed_symlink_artifacts"] == ["link"]
    (tmp_path / "result.json").write_text(json.dumps({"finished_at": None}))
    with pytest.raises(ValueError, match="completed trials"):
        audit_trial(tmp_path)


def test_finished_corrupt_trace_preserves_line_numbers_exception_and_artifacts(tmp_path: Path) -> None:
    (tmp_path / "agent").mkdir()
    (tmp_path / "artifacts").mkdir()
    (tmp_path / "artifacts/result.txt").write_text("retained result")
    exception = {"exception_type": "AgentTimeoutError", "exception_message": "interrupted"}
    (tmp_path / "result.json").write_text(
        json.dumps({"finished_at": "2026-01-01T00:00:00", "exception_info": exception})
    )
    lines = [json.dumps(row) for row in complete_trace()]
    lines.insert(4, "[]")
    lines.append('{"event":')
    (tmp_path / "agent/events.jsonl").write_text("\n".join(lines))
    report = audit_trial(tmp_path)
    assert report["workflow_passed"] is False
    assert report["trace_read_errors"] == [
        {"line": 5, "kind": "non_object_record"},
        {"line": len(lines), "kind": "invalid_json_or_utf8"},
    ]
    expected_line = next(
        index + 1 for index, row in enumerate(complete_trace(), 1) if row.get("event") == "task_notification"
    )
    assert report["review_notification_lines"] == [expected_line]
    assert report["harbor_exception"] == exception
    assert report["artifact_sha256"] == {"result.txt": hashlib.sha256(b"retained result").hexdigest()}
    assert report["failures"] == []  # Trace damage is not a fabricated Harness error.


@pytest.fixture
def snapshot_stub(monkeypatch: pytest.MonkeyPatch) -> Callable[[Path, Path], dict[str, object]]:
    def snapshot(repo: Path, destination: Path) -> dict[str, object]:
        destination.mkdir()
        (destination / "measured.whl").write_bytes(b"wheel build is covered by the adapter tests")
        return {"dataset": {"name": "must-be-replaced"}, "tasks": ["must-be-replaced"], "git_dirty": True}

    monkeypatch.setattr(prepare, "build_bundle", snapshot)
    return snapshot


def test_preparation_freezes_local_task_and_never_copies_credentials(
    tmp_path: Path, snapshot_stub: Callable[[Path, Path], dict[str, object]]
) -> None:
    credentials = tmp_path / "access.json"
    credentials.write_text("secret-marker-not-for-bundles")
    bundle = tmp_path / "bundle"
    config_path = prepare.prepare_bundle(tmp_path, bundle, tmp_path / "jobs", credentials)
    config = json.loads(config_path.read_text())
    provenance = json.loads((bundle / "provenance.json").read_text())
    assert config["agents"][0]["kwargs"]["credentials_path"] == str(credentials)
    assert config["agents"][0]["import_path"] == "benchmarks.terminal_bench.agent:NgnAgent"
    assert config["tasks"] == [{"path": str(bundle / "diagnostic-task")}]
    assert provenance["dataset"]["terminal_bench"] is False
    assert provenance["job_sha256"] == hashlib.sha256(config_path.read_bytes()).hexdigest()
    manifest = json.loads((bundle / "task-provenance.json").read_text())
    assert "tests/Dockerfile" in manifest["files_sha256"]
    for relative, expected in manifest["files_sha256"].items():
        assert hashlib.sha256((bundle / "diagnostic-task" / relative).read_bytes()).hexdigest() == expected
    assert not any(
        b"secret-marker-not-for-bundles" in path.read_bytes() for path in bundle.rglob("*") if path.is_file()
    )
    with pytest.raises(FileExistsError):
        prepare.prepare_bundle(tmp_path, bundle, tmp_path / "jobs", credentials)


def test_verifier_only_config_contains_no_model_or_credential_reference(
    tmp_path: Path, snapshot_stub: Callable[[Path, Path], dict[str, object]]
) -> None:
    bundle = tmp_path / "bundle"
    path = prepare.prepare_verifier_check(tmp_path, bundle, tmp_path / "jobs")
    config = json.loads(path.read_text())
    assert config["agents"] == [{"name": "nop"}]
    assert "credential" not in path.read_text()
    assert json.loads((bundle / "provenance.json").read_text())["run_kind"] == "verifier-setup-check-no-model"


def test_authored_broken_baseline_is_rejected_by_independent_verifier(tmp_path: Path) -> None:
    process = subprocess.run(
        [
            sys.executable,
            "-B",
            str(prepare.TASK_SOURCE / "tests/verify_contract.py"),
            "--app",
            str(prepare.TASK_SOURCE / "environment/app"),
            "--logs",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "Ran 9 tests" in process.stderr
    summary = json.loads((tmp_path / "diagnostic-verifier.json").read_text())
    assert summary["tests"] == 9 and summary["passed"] is False
    assert (tmp_path / "reward.txt").read_text() == "0\n"


def test_task_snapshot_excludes_generated_caches_and_bytecode(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "instruction.md").write_text("task")
    (source / "loose.pyc").write_bytes(b"stale bytecode")
    for name in ["__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"]:
        (source / name).mkdir()
        (source / name / "generated").write_text("must not be copied")
    target = tmp_path / "snapshot"
    copied = prepare._copy_task(source, target)
    assert copied == {"instruction.md": hashlib.sha256(b"task").hexdigest()}
    assert [path.name for path in target.iterdir()] == ["instruction.md"]


@pytest.mark.parametrize("directory_link", [False, True])
def test_task_snapshot_rejects_symlinks_before_copying(tmp_path: Path, directory_link: bool) -> None:
    source = tmp_path / "source"
    source.mkdir()
    external = tmp_path / "external"
    if directory_link:
        external.mkdir()
        (external / "file").write_text("outside task")
    else:
        external.write_text("outside task")
    (source / "linked").symlink_to(external, target_is_directory=directory_link)
    target = tmp_path / "snapshot"
    with pytest.raises(ValueError, match="symlinks"):
        prepare._copy_task(source, target)
    assert not target.exists()


@pytest.mark.skipif(os.name != "posix", reason="FIFO is a POSIX fixture")
def test_task_snapshot_rejects_nonregular_files_without_opening_them(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    os.mkfifo(source / "pipe")
    with pytest.raises(ValueError, match="regular files"):
        prepare._copy_task(source, tmp_path / "snapshot")
