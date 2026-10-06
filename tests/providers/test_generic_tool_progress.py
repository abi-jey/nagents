"""Streaming tool previews remain visible without releasing unvalidated calls."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from nagents.adapters._tool_progress import ARGUMENT_PREVIEW_LIMIT
from nagents.adapters._validation import object_data
from nagents.adapters._validation import tool_arguments
from nagents.events import ErrorEvent
from nagents.events import RateLimitEvent
from nagents.events import ToolCallEvent
from nagents.events import ToolCallProgressEvent
from nagents.http import HTTPError
from nagents.provider import Provider
from nagents.provider import ProviderType
from nagents.types import Message
from nagents.types import RetryConfig
from nagents.types import ToolCall
from tests.providers.test_gateway_provider import TOOL
from tests.providers.test_gateway_provider import frames
from tests.providers.test_gateway_provider import payload
from tests.providers.test_tool_stream_boundaries import generate

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


@pytest.mark.asyncio
@pytest.mark.parametrize("api", ["chat_completions", "responses", "messages"])
async def test_tool_previews_arrive_before_stream_ends_and_bind_parallel_calls(api: str) -> None:
    wire = frames(
        api,
        payload(api, "", (ToolCall("one", "work", {"value": "first"}), ToolCall("two", "work", {"value": "second"}))),
    )
    received = 0

    async def chunks(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
        nonlocal received
        for chunk in wire:
            received += 1
            yield chunk if isinstance(chunk, str) else json.dumps(chunk)

    provider = Provider(
        ProviderType.OPENAI_COMPATIBLE, api_key="fixture", model="fixture", api=api, base_url="http://localhost"
    )
    previews: list[ToolCallProgressEvent] = []
    calls: list[ToolCallEvent] = []
    try:
        with patch.object(provider._http, "post_stream", side_effect=chunks):
            async for event in provider.generate([Message(role="user", content="Work")], [TOOL]):
                assert not isinstance(event, ErrorEvent)
                if isinstance(event, ToolCallProgressEvent):
                    if event.status == "streaming":
                        assert received < len(wire)
                        assert not calls
                    previews.append(event)
                elif isinstance(event, ToolCallEvent):
                    calls.append(event)
    finally:
        await provider.close()

    assert [call.id for call in calls] == ["one", "two"]
    assert len({preview.generation_id for preview in previews}) == 1
    ready = [preview for preview in previews if preview.status == "ready"]
    assert [preview.id for preview in ready] == ["one", "two"]
    for call in calls:
        snapshots = [preview for preview in previews if preview.id == call.id]
        assert any(preview.arguments_text == '{"val' for preview in snapshots)
        assert json.loads(snapshots[-1].arguments_text) == call.arguments
        assert call.extra["generation_id"] == snapshots[-1].generation_id
        assert call.extra["index"] == snapshots[-1].index


@pytest.mark.asyncio
@pytest.mark.parametrize("api", ["chat_completions", "responses", "messages"])
async def test_truncated_stream_abandons_previews_without_releasing_tools(api: str) -> None:
    wire = frames(api, payload(api, "", (ToolCall("one", "work", {"value": "first"}),)))
    events = await generate(api, wire[:-1])
    previews = [event for event in events if isinstance(event, ToolCallProgressEvent)]
    assert any(event.status == "streaming" and event.name == "work" for event in previews)
    assert previews[-1].status == "abandoned"
    assert not any(event.status == "ready" for event in previews)
    assert any(isinstance(event, ErrorEvent) for event in events)
    assert not any(isinstance(event, ToolCallEvent) for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("api", ["chat_completions", "responses", "messages"])
async def test_invalid_later_call_abandons_every_preview_without_partial_execution(api: str) -> None:
    wire = frames(
        api,
        payload(
            api,
            "",
            (ToolCall("duplicate", "work", {"value": "first"}), ToolCall("duplicate", "work", {"value": "second"})),
        ),
    )
    events = await generate(api, wire)
    previews = [event for event in events if isinstance(event, ToolCallProgressEvent)]
    assert len({event.index for event in previews if event.status == "streaming"}) == 2
    assert sum(event.status == "abandoned" for event in previews) == 2
    assert not any(event.status == "ready" for event in previews)
    assert any(isinstance(event, ErrorEvent) for event in events)
    assert not any(isinstance(event, ToolCallEvent) for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("api", ["chat_completions", "responses", "messages"])
async def test_bounded_preview_preserves_complete_executable_arguments(api: str) -> None:
    arguments = tool_arguments({"value": "x" * (ARGUMENT_PREVIEW_LIMIT + 100)})
    events = await generate(api, frames(api, payload(api, "", (ToolCall("one", "work", arguments),))))
    previews = [event for event in events if isinstance(event, ToolCallProgressEvent)]
    calls = [event for event in events if isinstance(event, ToolCallEvent)]
    assert len(calls) == 1 and calls[0].arguments == arguments
    assert all(len(event.arguments_text) <= ARGUMENT_PREVIEW_LIMIT for event in previews)
    assert previews[-1].arguments_truncated and previews[-1].status == "ready"


@pytest.mark.asyncio
@pytest.mark.parametrize("api", ["chat_completions", "responses", "messages"])
async def test_nonstreaming_calls_do_not_emit_progress_or_preview_metadata(api: str) -> None:
    events = await generate(api, payload(api, "", (ToolCall("one", "work", {"value": "first"}),)))
    calls = [event for event in events if isinstance(event, ToolCallEvent)]
    assert len(calls) == 1
    assert not any(isinstance(event, ToolCallProgressEvent) for event in events)
    assert "generation_id" not in calls[0].extra


@pytest.mark.asyncio
async def test_preview_only_retry_abandons_old_generation_and_accepts_new_one() -> None:
    wire = frames("chat_completions", payload("chat_completions", "", (ToolCall("one", "work", {"value": "first"}),)))
    attempts = 0

    async def chunks(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            yield json.dumps(wire[1])
            raise HTTPError(503, "fixture transient failure")
        for chunk in wire:
            yield chunk if isinstance(chunk, str) else json.dumps(chunk)

    provider = Provider(
        ProviderType.OPENAI_COMPATIBLE,
        api_key="fixture",
        model="fixture",
        retry_config=RetryConfig(max_retries=1, base_delay=0),
    )
    try:
        with patch.object(provider._http, "post_stream", side_effect=chunks):
            events = [event async for event in provider.generate([Message(role="user", content="Work")], [TOOL])]
    finally:
        await provider.close()
    previews = [event for event in events if isinstance(event, ToolCallProgressEvent)]
    assert attempts == 2
    assert len({event.generation_id for event in previews}) == 2
    assert previews[0].status == "streaming" and previews[1].status == "abandoned"
    assert previews[0].generation_id == previews[1].generation_id
    assert previews[-1].status == "ready" and previews[-1].generation_id != previews[0].generation_id
    assert sum(isinstance(event, RateLimitEvent) for event in events) == 1
    assert sum(isinstance(event, ToolCallEvent) for event in events) == 1
    assert not any(isinstance(event, ErrorEvent) for event in events)


@pytest.mark.asyncio
async def test_chat_preview_index_survives_late_call_identity() -> None:
    wire = frames("chat_completions", payload("chat_completions", "", (ToolCall("one", "work", {"value": "first"}),)))
    initial = object_data(wire[1])
    initial["choices"] = [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"val'}}]}}]
    wire[1] = initial
    wire.insert(
        2,
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "one", "function": {"name": "work"}}]}}]},
    )
    events = await generate("chat_completions", wire)
    previews = [event for event in events if isinstance(event, ToolCallProgressEvent)]
    assert previews[0].id == previews[0].name == ""
    assert previews[1].id == "one" and previews[1].name == "work"
    assert len({(event.generation_id, event.index) for event in previews}) == 1
    assert previews[-1].status == "ready"


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", [True, False])
async def test_gemini_preview_requires_whole_stream_completion(finish: bool) -> None:
    provider = Provider(ProviderType.GEMINI_NATIVE, api_key="fixture", model="fixture")

    async def chunks(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
        yield json.dumps(
            {
                "candidates": [
                    {"index": 0, "content": {"parts": [{"functionCall": {"name": "work", "args": {"value": "first"}}}]}}
                ]
            }
        )
        if finish:
            yield json.dumps({"candidates": [{"index": 0, "finishReason": "STOP", "content": {"parts": []}}]})

    try:
        with patch.object(provider._http, "post_stream", side_effect=chunks):
            events = [event async for event in provider.generate([Message(role="user", content="Work")], [TOOL])]
    finally:
        await provider.close()
    previews = [event for event in events if isinstance(event, ToolCallProgressEvent)]
    assert previews[0].status == "streaming" and previews[0].name == "work"
    assert previews[-1].status == ("ready" if finish else "abandoned")
    calls = [event for event in events if isinstance(event, ToolCallEvent)]
    assert len(calls) == int(finish)
    if calls:
        assert calls[0].id == previews[0].id
        assert calls[0].extra["generation_id"] == previews[0].generation_id
