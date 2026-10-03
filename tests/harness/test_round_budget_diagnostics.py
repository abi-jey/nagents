"""Budget exhaustion stays terminal while its cause is safe to explain."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest

from nagents.events import DoneEvent
from nagents.events import ErrorEvent
from nagents.events import FinishReason
from nagents.events import ToolCallEvent
from nagents.events import ToolResultEvent
from nagents.events import Usage
from nagents.harness.config import HarnessConfig
from nagents.harness.provider import HarnessProvider
from nagents.web.provider_setup import provider_error
from tests.support.hang_guard import HANG_GUARD
from tests.support.providers import collect
from tests.support.providers import setup_harness
from tests.support.web import client_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.types import GenerationConfig
    from nagents.types import Message
    from nagents.types import ToolDefinition
    from tests.support.providers import FakeProvider


@pytest.mark.requires_posix
@pytest.mark.parametrize(
    ("limit", "calls", "metadata"),
    [(2, 2, {"max_tool_rounds": 2}), (True, 1, {}), (False, 0, {}), (0, 0, {}), (-1, 0, {})],
)
def test_actual_harness_budget_code_preserves_counts_usage_and_terminal_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: int, calls: int, metadata: dict[str, int]
) -> None:
    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        yield ToolCallEvent(
            id=f"list-{len(provider.requests)}",
            name="list_files",
            arguments={"limit": 1},
            usage=Usage(prompt_tokens=7, completion_tokens=3, total_tokens=10),
        )

    async def scenario() -> None:
        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        # Programmatic Agent values retain their existing behavior; this change
        # validates emitted metadata without changing constructor validation.
        harness.agent.max_tool_rounds = limit
        try:
            events = await collect(harness)
            errors = [event for event in events if isinstance(event, ErrorEvent)]
            assert len(errors) == 1
            error = errors[0]
            assert error.code == "MAX_TOOL_ROUNDS"
            assert error.message == f"Max tool rounds ({limit}) exceeded"
            assert error.recoverable is False and error.extra == metadata
            done = next(event for event in events if isinstance(event, DoneEvent))
            assert done.finish_reason is FinishReason.UNKNOWN and done.final_text == ""
            assert done.usage == error.usage
            assert done.usage.total_tokens == (10 if calls else 0)
            assert len(providers[0].requests) == calls
            assert len([event for event in events if isinstance(event, ToolResultEvent)]) == calls
            assert harness.config.max_tool_rounds == 30
            assert len([message for message in await harness.history() if message.role == "tool"]) == calls
        finally:
            await harness.close()
        assert harness._closed and providers[0].closed

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"max_tool_rounds": 30},
        {"max_tool_rounds": True},
        {"max_tool_rounds": "SECRET"},
        {"max_tool_rounds": -1},
        {"transport": {"headers": "SECRET"}},
        {"transport": {"category": "dns", "phase": "request", "generation_elapsed_ms": 10}},
    ],
)
def test_web_budget_explanation_ignores_upstream_text_and_metadata(metadata: dict[str, object]) -> None:
    reason, message = provider_error(ErrorEvent(code="MAX_TOOL_ROUNDS", message="SECRET", extra=metadata))
    assert reason == "tool_round_limit"
    assert message == (
        "This run reached its tool-round limit. Review the progress before continuing with a new message "
        "or adjusting the round limit in Settings."
    )
    assert "SECRET" not in message
    assert provider_error(ErrorEvent(message="Max tool rounds (30) exceeded"))[0] == "provider_failure"


@pytest.mark.requires_posix
def test_actual_web_stream_reports_budget_failure_without_an_extra_model_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generate = HarnessProvider.generate
    calls = 0

    async def count(
        provider: HarnessProvider,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: GenerationConfig | None = None,
        stream: bool = True,
        verify_model: bool = False,
    ) -> AsyncIterator[Event]:
        nonlocal calls
        calls += 1
        async for event in generate(provider, messages, tools, config, stream, verify_model):
            yield event

    monkeypatch.setattr(HarnessProvider, "generate", count)

    async def scenario() -> None:
        config = HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "data", demo=True, max_tool_rounds=1)
        async with client_app(tmp_path, config=config, controlled=False) as (_, client, headers, harnesses):
            session = (await client.get("/api/sessions", headers=headers)).json()["session_id"]
            response = await client.post(
                "/api/run", headers=headers, json={"session_id": session, "prompt": "offline budget probe"}
            )
            assert response.status_code == 200
            records = [json.loads(line) for line in response.text.splitlines()]
            error = next(record for record in records if record["event"] == "error")
            assert error["message"] == provider_error(ErrorEvent(code="MAX_TOOL_ROUNDS"))[1]
            assert error["recoverable"] is False
            assert "extra" not in error and "code" not in error
            assert next(record for record in records if record["event"] == "done")["finish_reason"] == "unknown"
            assert records[-1]["event"] == "run_finished" and records[-1]["status"] == "failed"
            assert calls == 1
            assert not any(record["event"] == "task_notification" for record in records)
        assert harnesses[0]._closed

    asyncio.run(scenario())


@pytest.mark.requires_posix
@pytest.mark.parametrize("cause", ["budget", "other"])
def test_child_budget_explanation_keeps_failed_lifecycle_and_retained_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cause: str
) -> None:
    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        assert provider.index == 1, "A child failure must not start a parent model turn on its own"
        if cause == "other":
            yield ErrorEvent(message="SECRET provider response", code="OTHER")
        else:
            yield ToolCallEvent(id=f"list-{len(provider.requests)}", name="list_files", arguments={"limit": 1})

    async def scenario() -> None:
        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        await harness.initialize()
        harness.tasks.begin(harness.session_id)
        try:
            ack = await harness.tasks.delegate("offline child budget probe")
            await asyncio.wait_for(asyncio.gather(*harness.tasks._workers.values()), HANG_GUARD)
            info = harness.tasks.list()[0]
            assert info.id == ack["task_id"] and info.status == "failed" and info.result == ""
            assert info.error == (
                "Subagent reached its tool-round limit; its response is not a successful result."
                if cause == "budget"
                else "Subagent reported a provider or tool-loop error; its response is not a successful result."
            )
            assert info.followups == info.activation == 0 and "SECRET" not in info.error
            assert not providers[0].requests
            assert len(providers[1].requests) == (12 if cause == "budget" else 1)
            child = harness.tasks._children[info.id]
            assert harness.config.max_tool_rounds == 30 and child.agent.max_tool_rounds == 12
            assert providers[1].closed and child._closed
            history = await harness.task_history(info.id)
            assert history and len([message for message in history if message.role == "tool"]) == (
                12 if cause == "budget" else 0
            )
            assert all(task.done() for task in harness.tasks._workers.values())
        finally:
            await harness.tasks.end()
            await harness.close()
        assert all(provider.closed for provider in providers)

    asyncio.run(scenario())
