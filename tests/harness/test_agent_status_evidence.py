"""Pure status projections never become a prompt, provider trace, or storage API."""

import json
from dataclasses import asdict
from typing import Literal

import pytest

from nagents.events import TokenUsage
from nagents.events import Usage
from nagents.harness.agent_status import AgentStatusCollection
from nagents.harness.agent_status import AgentStatusResult
from nagents.harness.agent_status import SessionEvidence
from nagents.harness.agent_status import make_status
from nagents.harness.subagents import TaskInfo


class Opaque:
    def __repr__(self) -> str:
        raise AssertionError("Opaque provider values must never be inspected")

    def __str__(self) -> str:
        raise AssertionError("Opaque provider values must never be rendered")


def info() -> TaskInfo:
    return TaskInfo(
        session_id="s2",
        title="Review the change",
        root_session_id="ngn-private-root",
        parent_session_id="s1",
        _storage_id="ngn-private-child",
        _parent_storage_id="ngn-private-parent",
        prompt="Private request is not status evidence",
        profile="reviewer",
        provider="saved-openai",
        model="accepted-model",
        phase="running",
        next_settings={"model": "not-yet-accepted-model"},
    )


def test_assistant_text_is_a_utf8_suffix_of_one_response_and_done_is_authoritative() -> None:
    evidence = SessionEvidence()
    evidence.observe({"type": "text_chunk", "response_id": "one", "chunk": "First "})
    evidence.observe({"type": "text_chunk", "response_id": "one", "chunk": "answer"})
    assert evidence.last_text == "First answer"
    assert len(evidence.history) == 1
    evidence.observe({"type": "text_done", "response_id": "one", "text": "Corrected final answer"})
    evidence.observe({"type": "text_chunk", "response_id": "one", "chunk": "late old chunk"})
    assert evidence.last_text == "Corrected final answer"
    evidence.observe({"type": "text_chunk", "response_id": "two", "chunk": "新" * 1000 + "🙂 done"})
    assert len(evidence.last_text.encode("utf-8")) <= 2048
    assert evidence.last_text.endswith("🙂 done")
    assert "Corrected" not in evidence.last_text
    assert evidence.truncated
    evidence.observe({"type": "text_done", "response_id": "two", "text": "Short new answer"})
    assert evidence.last_text == "Short new answer"
    evidence.observe({"type": "text_chunk", "chunk": "Unidentified new response"})
    assert evidence.last_text == "Unidentified new response"


def test_tool_evidence_contains_names_and_published_output_without_arguments_or_provider_extras() -> None:
    evidence = SessionEvidence()
    evidence.observe(
        {
            "type": "tool_call_progress",
            "id": "call",
            "name": "shell",
            "status": "streaming",
            "arguments_text": "PRIVATE_ARGUMENTS",
            "extra": Opaque(),
        }
    )
    assert evidence.last_tool == {"name": "shell", "status": "preparing"}
    evidence.observe({"type": "tool_call", "id": "call", "name": "shell", "arguments": Opaque()})
    evidence.observe({"type": "tool_execution_started", "id": "call", "name": "shell"})
    evidence.observe({"type": "tool_output", "id": "call", "name": "shell", "text": "one "})
    evidence.observe({"type": "tool_output", "id": "call", "text": "two"})
    assert evidence.last_output == "one two"
    assert evidence.last_tool == {"name": "shell", "status": "running"}
    evidence.observe(
        {
            "type": "tool_result",
            "id": "call",
            "name": "shell",
            "result": {
                "stdout": "Published result",
                "arguments": "PRIVATE_ARGUMENTS",
                "extra": "PRIVATE_EXTRA",
                "opaque": Opaque(),
            },
        }
    )
    assert evidence.last_tool == {"name": "shell", "status": "completed"}
    assert evidence.last_output == '{"stdout": "Published result"}'
    rendered = json.dumps(make_status(info(), evidence))
    assert "PRIVATE_ARGUMENTS" not in rendered
    assert "PRIVATE_EXTRA" not in rendered
    assert "opaque" not in rendered
    assert "arguments" not in rendered
    assert "Published result" in rendered
    evidence.observe({"type": "tool_call", "id": "new-call", "arguments": Opaque()})
    assert evidence.last_output == ""
    assert evidence.last_tool == {"name": "", "status": "requested"}


def test_tool_output_has_a_bounded_utf8_tail_and_failure_stays_a_tool_status() -> None:
    evidence = SessionEvidence()
    evidence.observe({"type": "tool_output", "id": "one", "name": "find", "text": "🙂" * 400 + " end"})
    assert len(evidence.last_output.encode("utf-8")) <= 1024
    assert evidence.last_output.endswith(" end")
    assert evidence.truncated
    evidence.observe({"type": "tool_output", "id": "two", "name": "read_file", "text": "Separate output"})
    assert evidence.last_output == "Separate output"
    evidence.observe({"type": "tool_result", "id": "two", "name": "read_file", "error": "File missing"})
    assert evidence.last_tool == {"name": "read_file", "status": "failed"}
    assert evidence.last_output == "File missing"


def test_a_public_tool_summary_is_output_while_a_provider_reasoning_summary_is_excluded() -> None:
    evidence = SessionEvidence()
    evidence.observe(
        {"type": "tool_result", "id": "call", "name": "summarize", "result": {"summary": "PUBLIC_SUMMARY"}}
    )
    assert evidence.last_output == '{"summary": "PUBLIC_SUMMARY"}'
    evidence.observe({"type": "response_metadata", "summary": "PRIVATE_REASONING_SUMMARY", "status": "thinking"})
    record = make_status(info(), evidence)
    assert "PUBLIC_SUMMARY" in json.dumps(record)
    assert "PRIVATE_REASONING_SUMMARY" not in json.dumps(record)


def test_reasoning_events_and_response_summaries_are_never_collected() -> None:
    evidence = SessionEvidence()
    for kind in ("reasoning_chunk", "reasoning_done", "provider_event", "history_committed", "resync_required"):
        evidence.observe({"type": kind, "text": "PRIVATE_REASONING", "chunk": "PRIVATE_REASONING", "extra": Opaque()})
    assert evidence.history == []
    assert not evidence.truncated
    evidence.observe(
        {
            "type": "response_metadata",
            "response_id": "response",
            "summary": "PRIVATE_REASONING",
            "status": "thinking",
            "extra": Opaque(),
            "provider_extras": Opaque(),
            "usage_reported": False,
        }
    )
    assert evidence.history == [
        {"type": "response_metadata", "preview": "", "name": "", "status": "thinking", "truncated": False}
    ]
    assert "PRIVATE_REASONING" not in json.dumps(make_status(info(), evidence))


def test_usage_is_reported_numeric_evidence_without_booleans_negatives_or_nested_provider_values() -> None:
    evidence = SessionEvidence()
    usage: dict[str, object] = {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
        "cached_tokens": 0,
        "cached_tokens_reported": True,
        "reasoning_tokens": 3,
        "audio_tokens": -1,
        "audio_tokens_reported": False,
        "reported": True,
        "session": {"total_tokens": 999},
        "provider_extra": Opaque(),
        "unknown_count": 7,
    }
    evidence.observe({"type": "response_metadata", "usage": usage, "usage_reported": True})
    assert evidence.usage == {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
        "cached_tokens": 0,
        "reasoning_tokens": 3,
    }
    usage["total_tokens"] = 999
    returned = evidence.usage
    returned["total_tokens"] = 888
    assert evidence.usage["total_tokens"] == 15
    evidence.observe({"type": "response_metadata", "usage": {"total_tokens": 0}, "usage_reported": False})
    evidence.observe(
        {"type": "response_metadata", "usage": {"prompt_tokens": True, "total_tokens": 1.5, "completion_tokens": -2}}
    )
    assert evidence.usage["total_tokens"] == 15
    evidence.observe({"type": "response_metadata", "usage": {"total_tokens": 0}, "usage_reported": True})
    assert evidence.usage == {"total_tokens": 0}


def test_usage_counter_flags_override_overall_reported_and_preserve_only_reported_zeroes() -> None:
    absent = asdict(
        Usage(
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            reasoning_tokens=19,
            reasoning_reported=False,
            reported=True,
            prompt_reported=False,
            completion_reported=False,
            total_reported=False,
            session=TokenUsage(
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                reasoning_tokens=19,
                reasoning_reported=False,
            ),
        )
    )
    absent.update(
        cached_tokens=0,
        audio_tokens=0,
        cached_tokens_reported=False,
        audio_tokens_reported=False,
    )
    missing_evidence = SessionEvidence()
    missing_evidence.observe(
        {
            "type": "response_metadata",
            "usage": absent,
            "usage_reported": True,
            "summary": "PRIVATE_PROVIDER_THINKING",
            "reasoning": "PRIVATE_PROVIDER_THINKING",
        }
    )
    assert missing_evidence.usage == {}
    assert "PRIVATE_PROVIDER_THINKING" not in json.dumps(make_status(info(), missing_evidence))

    reported_zero = asdict(
        Usage(
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            reasoning_tokens=0,
            reasoning_reported=True,
            reported=True,
            prompt_reported=True,
            completion_reported=True,
            total_reported=True,
            session=TokenUsage(
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                reasoning_tokens=0,
                reasoning_reported=True,
            ),
        )
    )
    reported_zero.update(
        cached_tokens=0,
        audio_tokens=0,
        cached_tokens_reported=True,
        audio_tokens_reported=True,
    )
    zero_evidence = SessionEvidence()
    zero_evidence.observe({"type": "response_metadata", "usage": reported_zero, "usage_reported": True})
    assert zero_evidence.usage == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
        "audio_tokens": 0,
        "reasoning_tokens": 0,
    }


def test_history_has_twenty_detached_utf8_previews_and_status_can_request_a_smaller_suffix() -> None:
    evidence = SessionEvidence()
    for index in range(30):
        evidence.observe({"type": "user_message", "text": f"Message {index}: " + "🙂" * 400})
    assert len(evidence.history) == 20
    assert evidence.history[0]["preview"].startswith("Message 10:")
    assert all(len(row["preview"].encode("utf-8")) <= 1024 for row in evidence.history)
    record = make_status(info(), evidence, history_limit=3)
    assert len(record["history"]) == 3
    assert record["history"][0]["preview"].startswith("Message 27:")
    assert record["truncated"]
    record["history"][0]["preview"] = "Changed externally"
    detached = evidence.history
    detached.clear()
    assert evidence.history[-3]["preview"].startswith("Message 27:")
    assert make_status(info(), evidence, history_limit=0)["history"] == []


@pytest.mark.parametrize("state", ["running", "completed", "failed", "cancelled"])
def test_status_exposes_only_effective_public_identity_and_current_state(
    state: Literal["running", "completed", "failed", "cancelled"],
) -> None:
    task = info()
    task.status = state
    task.result = "result" if state == "completed" else ""
    task.error = "error" if state == "failed" else ""
    record = make_status(task, SessionEvidence())
    assert set(record) == {
        "session_id",
        "title",
        "status",
        "agent",
        "parent_session_id",
        "provider",
        "model",
        "phase",
        "context",
        "compaction",
        "usage",
        "last_text",
        "last_tool",
        "last_output",
        "result",
        "error",
        "artifacts",
        "history",
        "truncated",
    }
    assert (record["session_id"], record["parent_session_id"], record["title"], record["agent"]) == (
        "s2",
        "s1",
        "Review the change",
        "reviewer",
    )
    assert record["model"] == "accepted-model"
    assert record["provider"] == "saved-openai"
    assert record["status"] == state
    assert record["result"] == task.result
    assert record["error"] == task.error
    assert record["artifacts"] == []
    assert not record["truncated"]
    serialized = json.dumps(record)
    assert "ngn-private" not in serialized
    assert "Private request" not in serialized
    assert "not-yet-accepted" not in serialized
    task.title = ""
    assert make_status(task, SessionEvidence())["title"] == ""
    collection: AgentStatusCollection = {"sessions": [record], "more": 1}
    result: AgentStatusResult = collection
    assert result.get("more") == 1


def test_configuration_projection_is_detached_and_drops_private_or_opaque_context() -> None:
    task = info()
    task.context = {
        "total_tokens": 30,
        "remaining_tokens": -2,
        "observed_prompt_tokens": -5,
        "context_window": None,
        "model": "accepted-model",
        "components": [{"key": "history", "label": "History", "tokens": 30, "extra": Opaque()}],
        "compaction": {"trigger": "tokens", "token_budget": 1000, "provider_extras": Opaque()},
        "session_id": "ngn-private-child",
        "prompt": "PRIVATE_PROMPT",
        "reasoning": "PRIVATE_REASONING",
        "extra": Opaque(),
    }
    task.compaction = {"trigger": "tokens", "token_threshold": 900, "token_budget": 1000, "extra": Opaque()}
    record = make_status(task, SessionEvidence())
    assert record["context"] == {
        "total_tokens": 30,
        "remaining_tokens": -2,
        "context_window": None,
        "model": "accepted-model",
        "components": [{"key": "history", "label": "History", "tokens": 30}],
        "compaction": {"trigger": "tokens", "token_budget": 1000},
    }
    assert record["compaction"] == {"trigger": "tokens", "token_threshold": 900, "token_budget": 1000}
    record["context"]["model"] = "changed"
    record["compaction"]["trigger"] = "off"
    assert task.context["model"] == "accepted-model"
    assert task.compaction["trigger"] == "tokens"
    assert "PRIVATE" not in json.dumps(record)
    task.compaction["trigger"] = {"opaque": Opaque()}
    assert "trigger" not in make_status(task, SessionEvidence())["compaction"]


def test_result_error_and_configuration_previews_are_bounded_without_mutating_the_retained_record() -> None:
    task = info()
    task.result = "🙂" * 1000
    task.error = "失" * 1000
    task.context = {
        "model": "m" * 1000,
        "components": [{"key": str(index), "label": "x", "tokens": 1} for index in range(40)],
    }
    record = make_status(task, SessionEvidence())
    assert len(record["result"].encode("utf-8")) <= 2048
    assert len(record["error"].encode("utf-8")) <= 2048
    assert record["truncated"]
    assert len(task.result.encode("utf-8")) == 4000
    assert len(task.error.encode("utf-8")) == 3000
    components = record["context"]["components"]
    assert isinstance(components, list)
    assert len(components) == 20


@pytest.mark.parametrize("history_limit", [-1, 21, True])
def test_invalid_history_limits_cannot_disable_the_bounds(history_limit: int) -> None:
    with pytest.raises(ValueError, match="between 0 and 20"):
        make_status(info(), SessionEvidence(), history_limit)
