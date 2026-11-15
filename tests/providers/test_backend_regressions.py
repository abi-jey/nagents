"""Regression coverage for backend request boundaries and resource ownership."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING
from typing import cast
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from nagents.adapters._validation import list_data
from nagents.adapters._validation import object_data
from nagents.events import ErrorEvent
from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.provider import Provider
from nagents.provider import ProviderType
from nagents.types import GenerationConfig
from nagents.types import Message
from nagents.types import ToolCall
from tests.providers.test_gateway_provider import frames
from tests.providers.test_gateway_provider import payload
from tests.providers.test_tool_stream_boundaries import generate

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from collections.abc import AsyncIterator

    from nagents.events import Event


@pytest.mark.asyncio
@pytest.mark.parametrize("earlier", ["max_tokens", "end_turn", "refusal", "future_reason"])
async def test_messages_conflicting_terminal_reason_rejects_all_tools(earlier: str) -> None:
    wire = frames(
        "messages", payload("messages", "OK", calls=(ToolCall(id="call-1", name="work", arguments={"value": "valid"}),))
    )
    wire.insert(-2, {"type": "message_delta", "delta": {"stop_reason": earlier}})
    events = await generate("messages", wire)
    assert any(isinstance(event, ErrorEvent) and event.code == "PROVIDER_PROTOCOL_ERROR" for event in events)
    assert not any(isinstance(event, ToolCallEvent | TextDoneEvent) for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("reasoning", [{"secret": "do-not-expose"}, ["do-not-expose"]])
@pytest.mark.parametrize("field", ["reasoning", "reasoning_content"])
async def test_nonstream_chat_rejects_nontext_reasoning(field: str, reasoning: object) -> None:
    wire = payload("chat_completions", "OK", calls=(ToolCall(id="call-1", name="work", arguments={"value": "valid"}),))
    choice = object_data(list_data(wire["choices"])[0])
    object_data(choice["message"])[field] = reasoning
    events = await generate("chat_completions", wire)
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert len(errors) == 1 and errors[0].code == "PROVIDER_PROTOCOL_ERROR"
    assert "do-not-expose" not in str(errors[0])
    assert not any(isinstance(event, ToolCallEvent | TextDoneEvent) for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["gemini-3.8-flash", "models/gemini-3.8-flash"])
async def test_gemini_normalizes_model_and_does_not_mutate_config(model: str) -> None:
    config = GenerationConfig(reasoning_summary="auto", thinking_config={"thinkingBudget": 2048})
    async with Provider(ProviderType.GEMINI_NATIVE, "fixture", model) as provider:
        with patch.object(
            provider._http,
            "post_json",
            AsyncMock(return_value={"candidates": [{"content": {"parts": [{"text": "OK"}]}, "finishReason": "STOP"}]}),
        ) as post:
            events = [event async for event in provider.generate([], config=config, stream=False)]
        assert not any(isinstance(event, ErrorEvent) for event in events)
        assert "/models/gemini-3.8-flash:generateContent?" in post.call_args.args[0]
        assert "/models/models/" not in post.call_args.args[0]
        assert post.call_args.args[1]["generationConfig"]["thinkingConfig"] == {
            "thinkingBudget": 2048,
            "includeThoughts": True,
        }
        assert config.thinking_config == {"thinkingBudget": 2048}


@pytest.mark.asyncio
async def test_native_anthropic_default_keeps_v1_prefix() -> None:
    async with Provider(ProviderType.ANTHROPIC, "fixture", "fixture") as provider:
        with patch.object(provider._http, "post_json", AsyncMock(return_value=payload("messages", "OK"))) as post:
            _ = [event async for event in provider.generate([Message(role="user", content="Hello")], stream=False)]
        assert post.call_args.args[0] == "https://api.anthropic.com/v1/messages"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["early_close", "cancel", "complete", "protocol_error"])
async def test_anthropic_transport_closes_deterministically_through_facade(mode: str) -> None:
    closed = False
    waiting = asyncio.Event()
    forever = asyncio.Event()

    async def stream(*args: object) -> AsyncIterator[str]:
        nonlocal closed
        try:
            yield json.dumps(
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": "OK"}}
            )
            if mode == "cancel":
                waiting.set()
                await forever.wait()
            elif mode == "complete":
                yield json.dumps({"type": "content_block_stop", "index": 0})
                yield json.dumps({"type": "message_delta", "delta": {"stop_reason": "end_turn"}})
                yield json.dumps({"type": "message_stop"})
                raise AssertionError("Transport consumed beyond message_stop")
            elif mode == "protocol_error":
                yield json.dumps({"type": "error", "error": {"message": "secret"}})
        finally:
            closed = True

    async with Provider(ProviderType.ANTHROPIC, "fixture", "fixture") as provider:
        with patch.object(provider._http, "post_stream", stream):
            events = cast("AsyncGenerator[Event]", provider.generate([Message(role="user", content="Hello")]))
            assert isinstance(await anext(events), TextChunkEvent)
            if mode == "early_close":
                await events.aclose()
            elif mode == "cancel":

                async def next_event() -> Event:
                    return await anext(events)

                task = asyncio.create_task(next_event())
                await asyncio.wait_for(waiting.wait(), 2)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                tail = [event async for event in events]
                assert any(isinstance(event, TextDoneEvent if mode == "complete" else ErrorEvent) for event in tail)
            assert closed
