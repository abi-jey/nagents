"""Streamed previews stay observable without becoming executable tool calls."""

from __future__ import annotations

import asyncio
import json
from contextlib import aclosing
from typing import TYPE_CHECKING

import pytest
from aiohttp import web

from nagents import Agent
from nagents import SessionManager
from nagents.adapters._tool_progress import ARGUMENT_PREVIEW_LIMIT
from nagents.adapters._tool_progress import ToolCallProgressTracker
from nagents.events import ErrorEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.events import ToolCallProgressEvent
from nagents.events import ToolExecutionStartedEvent
from nagents.events import ToolResultEvent
from nagents.provider.openai import OpenAIProvider
from nagents.types import Message
from tests.agent.test_agent_reliability import ScriptedProvider
from tests.providers.test_openai_provider import call_item
from tests.providers.test_openai_provider import completion
from tests.providers.test_openai_provider import credentials
from tests.providers.test_openai_provider import endpoint
from tests.providers.test_openai_provider import sse
from tests.providers.test_openai_provider import text_item
from tests.support.hang_guard import HANG_GUARD

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_codex_previews_arrive_before_completion_and_never_execute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    finish = asyncio.Event()
    requests = 0
    effects: list[str] = []

    async def handle(request: web.Request) -> web.StreamResponse:
        nonlocal requests
        requests += 1
        if requests > 1:
            return web.Response(text=sse([completion([text_item("Done")])]), content_type="text/event-stream")
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        await response.write(
            sse(
                [
                    {
                        "type": "response.output_item.added",
                        "output_index": 0,
                        "item": {**call_item(""), "status": "in_progress"},
                    },
                    {
                        "type": "response.function_call_arguments.delta",
                        "output_index": 0,
                        "item_id": "item-a",
                        "delta": '{"value":',
                    },
                ]
            ).encode()
        )
        await finish.wait()
        await response.write(
            sse(
                [
                    {
                        "type": "response.function_call_arguments.delta",
                        "output_index": 0,
                        "item_id": "item-a",
                        "delta": '"hello"}',
                    },
                    completion([call_item()]),
                ]
            ).encode()
        )
        await response.write_eof()
        return response

    def work(value: str) -> str:
        """Record the authorized invocation."""
        effects.append(value)
        return "done"

    async with asyncio.timeout(HANG_GUARD), endpoint(monkeypatch, handle):
        agent = Agent(
            OpenAIProvider(credentials),
            SessionManager(tmp_path / "events.db"),
            tools=[work],
            compactor=None,
            streaming=True,
        )
        try:
            async with aclosing(agent.run("work", session_id="s")) as stream:
                first = await anext(stream)
                assert isinstance(first, ToolCallProgressEvent) and first.arguments_text == ""
                second = await anext(stream)
                assert isinstance(second, ToolCallProgressEvent) and second.arguments_text == '{"value":'
                assert first.arguments_text == ""  # Later deltas cannot mutate a published snapshot.
                assert not effects
                assert first.generation_id == second.generation_id
                finish.set()
                events = [event async for event in stream]
            call = next(event for event in events if isinstance(event, ToolCallEvent))
            started = next(event for event in events if isinstance(event, ToolExecutionStartedEvent))
            result = next(event for event in events if isinstance(event, ToolResultEvent))
            ready = next(
                event for event in events if isinstance(event, ToolCallProgressEvent) and event.status == "ready"
            )
            assert call.arguments == {"value": "hello"} and effects == ["hello"]
            assert call.extra["generation_id"] == first.generation_id == started.extra["generation_id"]
            assert call.extra["index"] == first.index == 0
            assert events.index(ready) < events.index(call) < events.index(started) < events.index(result)
        finally:
            finish.set()
            await agent.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["incomplete", "malformed", "identity", "nonobject"])
async def test_failed_codex_preview_is_abandoned_without_ready_or_executable_call(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    final = call_item("{" if failure == "malformed" else '{"value":"hello"}')
    if failure == "identity":
        final["call_id"] = "replaced-id"
    wire = [
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {**call_item(""), "status": "in_progress"},
        },
        {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": '{"value":'},
    ]
    if failure == "nonobject":
        wire[1]["delta"] = '["private-upstream-value"]'
    if failure != "incomplete":
        wire.append(completion([final]))

    async def handle(request: web.Request) -> web.Response:
        return web.Response(text=sse(wire), content_type="text/event-stream")

    async with endpoint(monkeypatch, handle), OpenAIProvider(credentials) as provider:
        events = [event async for event in provider.generate([Message(role="user", content="work")])]
    progress = [event for event in events if isinstance(event, ToolCallProgressEvent)]
    assert [event.status for event in progress] == (
        ["streaming", "abandoned"] if failure == "nonobject" else ["streaming", "streaming", "abandoned"]
    )
    assert len({event.generation_id for event in progress}) == 1
    assert isinstance(events[-1], ErrorEvent) and events[-1].code == "CODEX_STREAM_INVALID"
    assert "private-upstream-value" not in repr(events)
    assert not any(isinstance(event, ToolCallEvent) for event in events)


def test_preview_bounds_do_not_truncate_canonical_arguments_or_repeat_capped_chunks() -> None:
    tracker = ToolCallProgressTracker()
    raw = '{"value":"' + "x" * (ARGUMENT_PREVIEW_LIMIT * 2) + '"}'
    preview = tracker.preview(3, "call", "work", raw)[0]
    assert len(preview.arguments_text) == ARGUMENT_PREVIEW_LIMIT and preview.arguments_truncated
    assert tracker.preview(3, "call", "work", raw + " ") == []
    call = ToolCallEvent(id="call", name="work", arguments=json.loads(raw))
    ready = tracker.ready(3, call)
    assert ready.status == "ready" and ready.arguments_truncated
    assert len(call.arguments["value"]) == ARGUMENT_PREVIEW_LIMIT * 2
    assert call.extra == {"generation_id": tracker.generation_id, "index": 3}
    other = ToolCallProgressTracker().preview(3, "call", "work", raw)[0]
    assert other.generation_id != preview.generation_id


@pytest.mark.asyncio
async def test_execution_start_marks_each_invocation_separately_and_previews_are_inert(tmp_path: Path) -> None:
    order: list[str] = []
    provider = ScriptedProvider(
        [
            [
                ToolCallProgressEvent(generation_id="g", id="not-executable", name="work", arguments_text="{}"),
                ToolCallEvent(id="one", name="work", arguments={"value": "one"}),
                ToolCallEvent(id="two", name="work", arguments={"value": "two"}),
            ],
            [TextDoneEvent(text="Done")],
        ]
    )

    def work(value: str) -> str:
        """Record actual execution separately from observer notifications."""
        order.append("effect:" + value)
        return value

    agent = Agent(provider, SessionManager(tmp_path / "order.db"), tools=[work], compactor=None)
    try:
        async for event in agent.run("work", session_id="s"):
            if isinstance(event, ToolCallEvent):
                order.append("call:" + event.id)
            elif isinstance(event, ToolExecutionStartedEvent):
                order.append("start:" + event.id)
            elif isinstance(event, ToolResultEvent):
                order.append("result:" + event.id)
    finally:
        await agent.close()
    assert order == [
        "call:one",
        "call:two",
        "start:one",
        "effect:one",
        "result:one",
        "start:two",
        "effect:two",
        "result:two",
    ]
