"""Audit completed diagnostic evidence; functional reward and workflow are separate."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
from dataclasses import asdict
from dataclasses import dataclass
from pathlib import Path
from typing import cast


def _mapping(value: object) -> dict[str, object]:
    return cast("dict[str, object]", value) if isinstance(value, dict) else {}


def _items(value: object) -> list[object]:
    return cast("list[object]", value) if isinstance(value, list) else []


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _json_file(path: Path) -> dict[str, object]:
    value: object = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return _mapping(value)


@dataclass(frozen=True)
class Edit:
    call_line: int
    result_line: int


@dataclass(frozen=True)
class TestRun:
    call_line: int
    result_line: int
    exit_code: int | None
    command_supported: bool
    summary_tests: int
    successful: bool


@dataclass(frozen=True)
class Delegation:
    line: int
    profile: str


@dataclass(frozen=True)
class Child:
    line: int
    task_id: str
    status: str
    error: str
    direct_completed: bool
    tool_names: list[str]
    unsupported_calls: list[str]
    successful_inspection_paths: list[str]

    def inspection_evidenced(self) -> bool:
        return (
            self.direct_completed
            and not self.unsupported_calls
            and {"report.py", "REVIEW.md"}.issubset(self.successful_inspection_paths)
        )


def _task_file(value: object) -> str:
    if not isinstance(value, str):
        return ""
    for name in ("report.py", "REVIEW.md"):
        if value in {name, "./" + name, "/app/" + name}:
            return name
    return ""


def _supported_unittest(command: str) -> tuple[bool, str]:
    """Recognize a small explicit subset, not arbitrary shell execution."""
    body = command.strip()
    prefix = re.match(r"^cd[ \t]+/app[ \t]+&&[ \t]+", body)
    if prefix:
        body = body[prefix.end() :]
    if any(character in body for character in ";&|<>`$()\n\r"):
        return False, "unsupported_shell_control_or_expansion"
    try:
        tokens = shlex.split(body)
    except ValueError:
        return False, "unparseable_command"
    if tokens[:1] == ["PYTHONDONTWRITEBYTECODE=1"]:
        tokens = tokens[1:]
    if not tokens or not re.fullmatch(r"python(?:3(?:\.\d+)?)?", tokens[0]):
        return False, "unsupported_interpreter_or_wrapper"
    tokens = tokens[1:]
    if tokens[:1] == ["-B"]:
        tokens = tokens[1:]
    if tokens[:2] != ["-m", "unittest"]:
        return False, "requires_unittest_module_invocation"
    arguments = tokens[2:]
    discovery = arguments[:1] == ["discover"]
    if discovery:
        arguments = arguments[1:]
    for argument in arguments:
        if argument in {"-v", "--verbose", "-q", "--quiet"}:
            continue
        if not discovery and re.fullmatch(r"test_[A-Za-z0-9_]+(?:\.py)?", argument):
            continue
        return False, "unsupported_unittest_arguments"
    return True, ""


def _successful_summary(output: str) -> int:
    clean = output.replace("\r\n", "\n").strip()
    if re.search(r"(?m)^(?:FAILED\b|FAIL:|ERROR:|Traceback \(most recent call last\):)", clean):
        return 0
    matches = list(re.finditer(r"(?m)^Ran ([1-9][0-9]*) tests? in [0-9]+(?:\.[0-9]+)?s$", clean))
    if len(matches) != 1 or clean[matches[0].end() :].strip() != "OK":
        return 0
    return int(matches[0].group(1))


def _child_history(index: int, event: dict[str, object]) -> Child:
    task = _mapping(event.get("task"))
    names: list[str] = []
    reads: dict[str, str] = {}
    inspected: set[str] = set()
    for item in _items(event.get("messages")):
        message = _mapping(item)
        for item_call in _items(message.get("tool_calls")):
            call = _mapping(item_call)
            name = _text(call.get("name"))
            names.append(name)
            call_id = _text(call.get("id"))
            path = _task_file(_mapping(call.get("arguments")).get("path"))
            if name == "read_file" and call_id and path:
                reads[call_id] = path
        call_id = _text(message.get("tool_call_id"))
        content = _text(message.get("content")).strip()
        if (
            message.get("role") == "tool"
            and message.get("name") == "read_file"
            and call_id in reads
            and content
            and not content.startswith("Error:")
        ):
            inspected.add(reads[call_id])
    direct_completed = (
        task.get("parent_task_id") == ""
        and type(task.get("depth")) is int
        and task.get("depth") == 1
        and task.get("status") == "completed"
        and task.get("error") == ""
    )
    return Child(
        index,
        _text(task.get("id")),
        _text(task.get("status")),
        _text(task.get("error")),
        direct_completed,
        names,
        [name for name in names if name not in {"read_file", "list_files", "find", "search"}],
        sorted(inspected),
    )


def audit_events(rows: list[dict[str, object]]) -> dict[str, object]:
    """Require correlated native review evidence and observed, unmasked test success."""
    calls: dict[str, tuple[int, dict[str, object]]] = {}
    edits: list[Edit] = []
    tests: list[TestRun] = []
    delegates: list[Delegation] = []
    delegated_ids: set[str] = set()
    direct_started: set[str] = set()
    completed: dict[str, dict[str, object]] = {}
    notifications: list[tuple[int, str]] = []
    ignored_notifications: list[int] = []
    children: list[Child] = []
    history_errors: list[dict[str, object]] = []
    failures: list[dict[str, object]] = []
    recoverable_errors: list[dict[str, object]] = []
    manual: list[dict[str, object]] = []
    root = next((_text(row.get("session_id")) for row in rows if row.get("event") == "benchmark_start"), "")
    declares_root = any(row.get("event") == "benchmark_start" and "session_id" in row for row in rows)
    compacting = normal_completion = False
    for index, event in enumerate(rows, 1):
        kind = event.get("event")
        if kind == "compaction_started":
            compacting = True
        elif kind == "compaction_done":
            compacting = False
        elif kind == "done" and not compacting and (not declares_root or (root and event.get("session_id") == root)):
            normal_completion = event.get("finish_reason") == "stop"
        if kind == "tool_call":
            calls[_text(event.get("id"))] = (index, event)
            if event.get("name") == "delegate":
                delegates.append(Delegation(index, _text(_mapping(event.get("arguments")).get("agent", "assistant"))))
        elif kind == "tool_result":
            call_line, call = calls.get(_text(event.get("id")), (0, {}))
            arguments = _mapping(call.get("arguments"))
            result = _mapping(event.get("result"))
            if event.get("error"):
                failures.append({"line": index, "kind": "tool_error", "message": event["error"]})
            if not result or not call_line or call.get("name") != event.get("name"):
                continue
            task_id = result.get("task_id")
            if event.get("name") == "delegate" and not event.get("error") and isinstance(task_id, str) and task_id:
                delegated_ids.add(task_id)
            if (
                event.get("name") == "edit"
                and not event.get("error")
                and _task_file(arguments.get("path")) == "report.py"
                and _task_file(result.get("path")) == "report.py"
                and result.get("changed") is True
            ):
                edits.append(Edit(call_line, index))
            if event.get("name") == "shell":
                command = _text(arguments.get("command"))
                looks_like_test = "unittest" in command or re.search(r"\bpython\S*\b.*test_\w+\.py", command)
                if looks_like_test:
                    supported, reason = _supported_unittest(command)
                    raw_code = result.get("exit_code")
                    code = raw_code if type(raw_code) is int else None
                    count = _successful_summary(_text(result.get("output")))
                    passed = (
                        supported
                        and count > 0
                        and code == 0
                        and result.get("timed_out") is False
                        and result.get("truncated") is False
                    )
                    tests.append(TestRun(call_line, index, code, supported, count, passed))
                    if not supported or code is None or (code == 0 and count == 0):
                        manual.append({"line": index, "kind": reason or "missing_successful_unittest_evidence"})
        elif kind == "task_started" and event.get("parent_task_id") == "" and event.get("depth") == 1:
            direct_started.add(_text(event.get("task_id")))
        elif kind == "task_completed":
            completed[_text(event.get("task_id"))] = event
        elif kind == "task_notification" and event.get("cause") == "completion":
            source = _text(event.get("source_task_id"))
            outcome = completed.get(source, {})
            if (
                event.get("recipient_task_id") == ""
                and source in delegated_ids & direct_started
                and outcome.get("status") == "completed"
                and outcome.get("error") == ""
                and outcome.get("parent_task_id") == ""
                and outcome.get("depth") == 1
            ):
                notifications.append((index, source))
            else:
                ignored_notifications.append(index)
        elif kind == "benchmark_task_history":
            children.append(_child_history(index, event))
        elif kind == "benchmark_task_history_error":
            history_errors.append({"line": index, "task_id": event.get("task_id"), "class": event.get("class")})
        elif kind == "error" and event.get("recoverable") is True:
            recoverable_errors.append({"line": index, "kind": "recoverable_error", "event": event})
        elif kind in {"error", "benchmark_exception", "benchmark_timeout", "benchmark_cancelled"}:
            failures.append({"line": index, "kind": kind, "message": event.get("message", "")})
    notified_ids = {task_id for _, task_id in notifications}
    first_review = notifications[0][0] if notifications else 0
    first_delegate = delegates[0].line if delegates else 0
    initial_tests = [test for test in tests if test.result_line < first_delegate]
    history_ok = bool(delegated_ids)
    for task_id in sorted(delegated_ids):
        matched = [child for child in children if child.task_id == task_id]
        if any(error.get("task_id") == task_id for error in history_errors):
            history_ok = False
            manual.append({"task_id": task_id, "kind": "required_review_history_error"})
        elif len(matched) != 1 or not matched[0].inspection_evidenced():
            history_ok = False
            manual.append({"task_id": task_id, "kind": "required_review_inspection_not_evidenced"})
    last_review_edit = max((edit.result_line for edit in edits if edit.call_line > first_review > 0), default=0)
    checks = {
        "phase1_native_edit_before_delegation": any(edit.result_line < first_delegate for edit in edits),
        "phase1_tests_before_delegation": bool(initial_tests) and initial_tests[-1].successful,
        "uses_configured_assistant_delegation": bool(delegates)
        and all(item.profile == "assistant" for item in delegates),
        "received_native_review_notification": bool(delegated_ids) and delegated_ids <= notified_ids,
        "native_report_edit_after_review": last_review_edit > 0,
        "successful_tests_after_review_edit": last_review_edit > 0
        and bool(tests)
        and tests[-1].call_line > last_review_edit
        and tests[-1].successful,
        "child_history_is_completed_and_inspect_only": history_ok,
        "normal_final_completion": normal_completion
        and any(event.get("event") == "benchmark_end" and event.get("status") == "completed" for event in rows),
        "no_observed_root_tool_errors": not any(item["kind"] == "tool_error" for item in failures),
        "no_terminal_root_harness_errors": not any(item["kind"] != "tool_error" for item in failures),
    }
    return {
        "workflow_observed": bool(rows),
        "workflow_checks": checks,
        "workflow_passed": all(checks.values()),
        "edits": [asdict(edit) for edit in edits],
        "tests": [asdict(test) for test in tests],
        "delegates": [asdict(delegate) for delegate in delegates],
        "required_review_task_ids": sorted(delegated_ids),
        "review_notification_lines": [line for line, _ in notifications],
        "ignored_non_main_or_unmatched_notification_lines": ignored_notifications,
        "children": [asdict(child) for child in children],
        "history_read_errors": history_errors,
        "manual_confirmation_required": manual,
        "failures": failures,
        "recoverable_errors": recoverable_errors,
        "completion_scope": "session-bound root"
        if declares_root
        else "legacy root-stream evidence without session binding",
    }


def audit_trial(trial: Path) -> dict[str, object]:
    result = _json_file(trial / "result.json")
    if not result.get("finished_at"):
        raise ValueError("Audit only completed trials; do not read artifacts while the agent is running")
    events = trial / "agent/events.jsonl"
    rows: list[dict[str, object]] = []
    trace_errors: list[dict[str, object]] = []
    if events.exists():
        with events.open("rb") as stream:
            for line_number, raw in enumerate(stream, 1):
                try:
                    value: object = json.loads(raw.decode("utf-8"))
                except ValueError:
                    trace_errors.append({"line": line_number, "kind": "invalid_json_or_utf8"})
                    rows.append({})
                    continue
                if not isinstance(value, dict):
                    trace_errors.append({"line": line_number, "kind": "non_object_record"})
                    rows.append({})
                else:
                    rows.append(_mapping(value))
    workflow = audit_events(rows)
    if trace_errors:
        workflow["workflow_passed"] = False
    artifacts = trial / "artifacts"
    hashes: dict[str, str] = {}
    skipped: list[str] = []
    for path in sorted(artifacts.rglob("*")):
        if path.is_symlink():
            skipped.append(str(path.relative_to(artifacts)))
        elif path.is_file():
            hasher = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    hasher.update(block)
            hashes[str(path.relative_to(artifacts))] = hasher.hexdigest()
    verifier = trial / "verifier/diagnostic-verifier.json"
    provenance = trial / "agent/provenance.json"
    return {
        "diagnostic": "ngn-owned-review-followthrough-v2",
        "terminal_bench_score": False,
        **workflow,
        "trace_read_errors": trace_errors,
        "functional_verifier": _json_file(verifier) if verifier.exists() else {},
        "harbor_verifier_result": result.get("verifier_result"),
        "harbor_exception": result.get("exception_info"),
        "source_provenance": _json_file(provenance) if provenance.exists() else {},
        "artifact_sha256": hashes,
        "unhashed_symlink_artifacts": skipped,
        "coverage": "Root events plus bounded child history, not an adversarial anti-tamper proof. Manually inspect new test files and match them to executed test output. A nop verifier check has no model workflow.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trial", type=Path)
    args = parser.parse_args()
    print(json.dumps(audit_trial(args.trial), indent=2))
