"""Offline checks for prompt fidelity, isolation and pre-call trace capture."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from benchmarks.orchestration import telemetry
from benchmarks.orchestration.telemetry import example_usage
from nagents.events import ErrorEvent
from nagents.events import FinishReason
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent

from . import run
from .audit import audit_trace
from .cases import NaturalCase
from .run import trial

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from nagents.events import Event
    from nagents.types import GenerationConfig
    from nagents.types import Message
    from nagents.types import RetryConfig
    from nagents.types import ToolDefinition

from benchmarks.orchestration.run import sha256

from .run import Trace
from .run import permitted_path


def test_approval_rejects_escape_and_non_authorized_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "note.txt").write_text("old")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    (workspace / "link.txt").symlink_to(outside)
    allowed = ("note.txt", "link.txt")
    assert permitted_path(workspace, "note.txt", allowed)
    assert permitted_path(workspace, str(workspace / "note.txt"), allowed)
    assert not permitted_path(workspace, "../outside.txt", allowed)
    assert not permitted_path(workspace, "link.txt", allowed)
    assert not permitted_path(workspace, "other.txt", allowed)
    assert not permitted_path(workspace, None, allowed)


def test_trace_freezes_offered_contract_before_execution() -> None:
    trace = Trace()
    tools = [{"name": "read_file", "description": "Native description", "parameters": {"type": "object"}}]
    messages = [{"role": "user", "content": "could you check the notes?"}]
    trace.observe("model_context", {"messages": messages, "tools": tools, "model_call_id": "g1", "session_id": "s1"})
    tools[0]["description"] = "later change"
    messages[0]["content"] = "later change"
    request = trace.requests[0]
    assert request["tools"] == [
        {"name": "read_file", "description": "Native description", "parameters": {"type": "object"}}
    ]
    assert request["messages"] == [{"role": "user", "content": "could you check the notes?"}]
    assert request["sequence"] == 1
    assert trace.tick() == 2
    assert len(str(request["input_sha256"])) == len(sha256(""))
    trace.session_actors["s1"] = ("child", "task1")
    trace.annotate()
    assert request["actor"] == "child"
    assert request["task_id"] == "task1"


@pytest.mark.parametrize("runtime_error", [False, True])
def test_native_harness_preserves_prompt_and_stale_failure(
    monkeypatch: pytest.MonkeyPatch,
    runtime_error: bool,
) -> None:
    calls = 0
    seen_users: list[str] = []

    class FakeLive:
        def __init__(self, model: str, timeout: float, retry_config: RetryConfig) -> None:
            self.model = model

        async def verify_model(self, force: bool = False) -> bool:
            return True

        async def close(self) -> None:
            pass

        async def generate(
            self,
            messages: list[Message],
            tools: list[ToolDefinition] | None = None,
            config: GenerationConfig | None = None,
            stream: bool = True,
            verify_model: bool = False,
        ) -> AsyncIterator[Event]:
            nonlocal calls
            calls += 1
            seen_users.extend(str(message.content) for message in messages if message.role == "user")
            assert tools
            assert "shell" not in {tool.name for tool in tools}
            assert "delegate" not in {tool.name for tool in tools}
            usage = example_usage(100, 10)
            if calls in {1, 3}:
                yield ToolCallEvent(id=f"c{calls}", name="read_file", arguments={"path": "note.txt"}, usage=usage)
            elif calls in {2, 4}:
                yield ToolCallEvent(
                    id=f"c{calls}", name="edit", arguments={"path": "note.txt", "old": "old", "new": "new"}, usage=usage
                )
            else:
                if runtime_error:
                    yield ErrorEvent(message="offline interruption", code="OFFLINE_ERROR", recoverable=False)
                yield TextDoneEvent(text="Updated the note.", finish_reason=FinishReason.STOP, usage=usage)
                return
            yield TextDoneEvent(finish_reason=FinishReason.TOOL_CALLS, usage=usage)

    monkeypatch.setattr(telemetry, "OpenAIProvider", FakeLive)
    if runtime_error:
        # A file-only grader can pass after edits even when transport then fails.
        monkeypatch.setattr(run, "grade", lambda case, workspace, final_text: [])
    case = NaturalCase(
        case_id="offline",
        split="development",
        seed=1,
        prompt="can you update old to new in note.txt?",
        files={"note.txt": "old\n"},
        authorized_paths=("note.txt",),
        expected_files={"note.txt": "new\nextra\n"},
        external_edit_path="note.txt",
        external_edit_content="old\nextra\n",
    )
    result = asyncio.run(trial(case, "offline", 20))
    assert result["errors"] == (["OFFLINE_ERROR"] if runtime_error else [])
    assert result["saved_files"] == case.expected_files
    assert result["artifact_correct"] is True
    assert result["task_correct"] is (not runtime_error)
    assert result["run_complete"] is (not runtime_error)
    assert result["external_edit_applied"] is True
    assert seen_users and set(seen_users) == {case.prompt}
    events = result["tool_events"]
    assert isinstance(events, list)
    assert [event["tool"] for event in events] == ["read_file", "edit", "read_file", "edit"]
    assert "changed since read" in events[1]["error"]
    assert events[3]["error"] == ""
    assert all(event["generation_id"] for event in events)
    assert all(event["actor"] == "root" for event in events)
    requests = result["requests"]
    assert isinstance(requests, list)
    assert all(request["tools"] for request in requests)
    mapped = {request["generation_id"]: request for request in requests}
    for event in events:
        request = mapped[event["generation_id"]]
        assert event["sequence"] > request["sequence"]
        assert event["tool"] in {tool["name"] for tool in request["tools"]}


def test_matrix_parallel_bound_retains_success_failure_timeout_and_launch_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = maximum = 0
    killed: list[str] = []
    selected = tuple(NaturalCase(str(i), "development", i, "check this", {}) for i in range(4))

    class FakeProcess:
        def __init__(self, name: str, path: Path) -> None:
            self.name, self.path = name, path
            self.returncode = 1 if name == "1" else 0
            self.attempts = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            nonlocal active
            self.attempts += 1
            await asyncio.sleep(0.01)
            active -= 1
            if self.name == "2" and self.attempts == 1:
                raise TimeoutError
            if self.name == "0":
                self.path.write_text(json.dumps({"case_id": self.name, "task_correct": True}))
            return b"", b"private failure"

        def kill(self) -> None:
            nonlocal active
            active += 1  # The watchdog drains the terminated subprocess.
            killed.append(self.name)

    async def create(*command: str, **options: object) -> FakeProcess:
        nonlocal active, maximum
        name = command[command.index("--case") + 1]
        if name == "3":
            raise OSError("launch failed")
        active += 1
        maximum = max(maximum, active)
        return FakeProcess(name, Path(command[command.index("--output") + 1]))

    monkeypatch.setattr(run, "cases", lambda suite: selected)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    args = argparse.Namespace(
        suite="original",
        output=tmp_path / "results",
        case="all",
        split="development",
        model="offline",
        timeout=20,
        concurrency=2,
    )
    summary = asyncio.run(run.matrix(args))
    assert maximum == 2
    assert active == 0
    assert killed == ["2"]
    results = summary["results"]
    assert isinstance(results, list)
    assert [result["case_id"] for result in results] == ["0", "1", "2", "3"]
    assert results[0]["task_correct"] is True
    assert [result.get("worker_error") for result in results[1:]] == [
        "subprocess_failure",
        "parent_timeout",
        "worker_exception",
    ]
    assert len(list(args.output.glob("*.error.json"))) == 3
    saved = json.loads((args.output / "summary.json").read_text())
    assert saved["results"] == results


def test_unmapped_generation_does_not_borrow_other_requests_schema() -> None:
    requests: list[dict[str, object]] = [
        {
            "generation_id": "known",
            "messages": [],
            "tools": [
                {
                    "name": "read_file",
                    "parameters": {"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}}},
                }
            ],
        }
    ]
    events: list[dict[str, object]] = [
        {
            "generation_id": "unknown",
            "actor": "root",
            "task_id": "",
            "tool": "read_file",
            "call_id": "call",
            "arguments": {"wrong": True},
            "error": "Invalid arguments",
        }
    ]
    audit = audit_trace(requests, events)
    assert len(audit.failures) == 1
    assert audit.failures[0].category == "ambiguous"
    events[0]["generation_id"] = "known"
    assert audit_trace(requests, events).failures[0].category == "avoidable"


def test_novel_blocked_control_keeps_private_metadata_out_of_model_context(monkeypatch: pytest.MonkeyPatch) -> None:
    from .cases import cases

    case = next(case for case in cases("generalization") if case.blocked_reason == "missing_file")
    users: list[str] = []

    class FakeLive:
        def __init__(self, model: str, timeout: float, retry_config: RetryConfig) -> None:
            self.model = model

        async def verify_model(self, force: bool = False) -> bool:
            return True

        async def close(self) -> None:
            pass

        async def generate(
            self,
            messages: list[Message],
            tools: list[ToolDefinition] | None = None,
            config: GenerationConfig | None = None,
            stream: bool = True,
            verify_model: bool = False,
        ) -> AsyncIterator[Event]:
            users.extend(str(message.content) for message in messages if message.role == "user")
            context = " ".join(str(message.content) for message in messages)
            assert case.case_id not in context
            assert case.blocked_reason not in context
            assert "expected_blocked" not in context
            assert "answer_terms" not in context
            yield TextDoneEvent(text="Where can I find that file?", finish_reason=FinishReason.STOP)

    monkeypatch.setattr(telemetry, "OpenAIProvider", FakeLive)
    result = asyncio.run(trial(case, "offline", 20))
    assert users == [case.prompt]
    assert result["task_correct"] is False
    assert result["artifact_correct"] is True
    assert result["clarification_review_candidate"] is True
    assert result["manual_honesty_review_required"] is True
    assert result["blocked_reason"] == "missing_file"
