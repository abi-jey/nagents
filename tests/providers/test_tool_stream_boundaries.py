"""Malformed candidate/block boundaries must never release executable calls."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from aiohttp import web

from nagents.events import ErrorEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.provider import Provider
from nagents.provider import ProviderType
from nagents.types import Message
from nagents.types import ToolCall
from tests.providers.test_gateway_provider import TOOL
from tests.providers.test_gateway_provider import endpoint
from tests.providers.test_gateway_provider import frames
from tests.providers.test_gateway_provider import payload
from tests.providers.test_gateway_provider import sse

if TYPE_CHECKING:
    from nagents.events import Event


async def generate(api: str, wire: dict[str, object] | list[dict[str, object] | str]) -> list[Event]:
    streamed = isinstance(wire, list)

    async def handle(request: web.Request) -> web.Response:
        assert request.method == "POST"
        if isinstance(wire, list):
            return web.Response(body=sse(wire), content_type="text/event-stream")
        return web.json_response(wire)

    async with endpoint(handle) as url:
        provider = Provider(ProviderType.OPENAI_COMPATIBLE, api_key="fixture", model="fixture", api=api, base_url=url)
        try:
            return [
                event
                async for event in provider.generate(
                    [Message(role="user", content="Run work")],
                    tools=None if api == "completions" else [TOOL],
                    stream=streamed,
                )
            ]
        finally:
            await provider.close()


@pytest.mark.asyncio
async def test_fragments_from_different_chat_choices_cannot_combine_into_one_tool_call() -> None:
    events = await generate(
        "chat_completions",
        [
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "id": "call-one", "function": {"name": "work", "arguments": '{"value":'}}
                            ]
                        },
                    }
                ]
            },
            {
                "choices": [
                    {
                        "index": 1,
                        "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"from another choice"}'}}]},
                    }
                ]
            },
            {"choices": [{"index": 1, "delta": {}, "finish_reason": "tool_calls"}]},
            "[DONE]",
        ],
    )
    assert any(isinstance(event, ErrorEvent) and "choice index" in event.message for event in events)
    assert not any(isinstance(event, ToolCallEvent | TextDoneEvent) for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("api", ["chat_completions", "completions"])
@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("index", [1, -1, True, None])
async def test_a_single_choice_still_requires_the_requested_zero_index(api: str, streamed: bool, index: object) -> None:
    result = payload(api, "fixture", (ToolCall("one", "work", {"value": "fixture"}),) if api != "completions" else ())
    wire: dict[str, object] | list[dict[str, object] | str]
    if streamed:
        wire = frames(api, result)
        for event in wire:
            if isinstance(event, dict):
                choices = event.get("choices")
                if isinstance(choices, list):
                    for choice in choices:
                        choice["index"] = index
    else:
        choices = result["choices"]
        assert isinstance(choices, list)
        choices[0]["index"] = index
        wire = result
    events = await generate(api, wire)
    assert any(isinstance(event, ErrorEvent) and "choice index" in event.message for event in events)
    assert not any(isinstance(event, ToolCallEvent | TextDoneEvent) for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("streamed", [False, True])
async def test_gateways_omitting_the_single_choice_index_keep_valid_call_correlations(streamed: bool) -> None:
    result = payload(
        "chat_completions",
        calls=(ToolCall("first", "work", {"value": "one"}), ToolCall("second", "work", {"value": "two"})),
    )
    wire: dict[str, object] | list[dict[str, object] | str]
    if streamed:
        wire = frames("chat_completions", result)
        for event in wire:
            if isinstance(event, dict) and isinstance(choices := event.get("choices"), list):
                for choice in choices:
                    choice.pop("index", None)
    else:
        choices = result["choices"]
        assert isinstance(choices, list)
        choices[0].pop("index")
        wire = result
    events = await generate("chat_completions", wire)
    calls = [event for event in events if isinstance(event, ToolCallEvent)]
    assert [(call.id, call.arguments) for call in calls] == [("first", {"value": "one"}), ("second", {"value": "two"})]
    assert not any(isinstance(event, ErrorEvent) for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "block,delta",
    [
        ("tool_use", {"type": "text_delta", "text": "must not become arguments or narration"}),
        ("tool_use", {"type": "thinking_delta", "thinking": "wrong block"}),
        ("tool_use", {"type": "signature_delta", "signature": "wrong block"}),
        ("tool_use", {"type": "future_unknown_delta"}),
        ("text", {"type": "input_json_delta", "partial_json": "{}"}),
        ("thinking", {"type": "text_delta", "text": "must not become visible narration"}),
    ],
)
async def test_messages_delta_type_must_match_its_block_before_any_tools_are_released(
    block: str, delta: dict[str, object]
) -> None:
    events = await generate(
        "messages",
        [
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "valid-first", "name": "work", "input": {"value": "valid"}},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {"type": block, "id": "second", "name": "work", "input": {}},
            },
            {"type": "content_block_delta", "index": 1, "delta": delta},
            {"type": "content_block_stop", "index": 1},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
            {"type": "message_stop"},
        ],
    )
    assert any(isinstance(event, ErrorEvent) and "wrong content block" in event.message for event in events)
    assert not any(isinstance(event, ToolCallEvent | TextDoneEvent) for event in events)


@pytest.mark.asyncio
async def test_valid_messages_thinking_text_and_multiple_tool_blocks_remain_separate() -> None:
    events = await generate(
        "messages",
        [
            {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "private"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "fixture"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": "Checking "}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "now."}},
            {"type": "content_block_stop", "index": 1},
            {
                "type": "content_block_start",
                "index": 2,
                "content_block": {"type": "tool_use", "id": "first", "name": "work", "input": {}},
            },
            {
                "type": "content_block_delta",
                "index": 2,
                "delta": {"type": "input_json_delta", "partial_json": '{"value": "one"}'},
            },
            {"type": "content_block_stop", "index": 2},
            {
                "type": "content_block_start",
                "index": 3,
                "content_block": {"type": "tool_use", "id": "second", "name": "work", "input": {}},
            },
            {"type": "content_block_stop", "index": 3},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
            {"type": "message_stop"},
        ],
    )
    calls = [event for event in events if isinstance(event, ToolCallEvent)]
    assert [(call.id, call.arguments) for call in calls] == [("first", {"value": "one"}), ("second", {})]
    assert [event.text for event in events if isinstance(event, TextDoneEvent)] == ["Checking now."]
    assert not any(isinstance(event, ErrorEvent) for event in events)


def messages_with_tool_input(initial: dict[str, object], arguments: str) -> list[dict[str, object] | str]:
    wire: list[dict[str, object] | str] = [
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "first", "name": "work", "input": {"value": "valid"}},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "tool_use", "id": "second", "name": "work", "input": initial},
        },
    ]
    if arguments:
        wire.extend(
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": fragment},
            }
            for fragment in (arguments[:5], arguments[5:])
        )
    return [
        *wire,
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
        {"type": "message_stop"},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initial", "arguments"),
    [
        ({"value": True}, '{"value":1}'),
        ({"value": False}, '{"value":0}'),
        ({"value": [{"flag": False}]}, '{"value":[{"flag":0}]}'),
        ({"value": {"flag": True}}, '{"value":{"flag":1.0}}'),
    ],
)
async def test_messages_changed_argument_types_block_every_tool_call(
    initial: dict[str, object], arguments: str
) -> None:
    events = await generate("messages", messages_with_tool_input(initial, arguments))
    assert any(
        isinstance(event, ErrorEvent)
        and event.code == "PROVIDER_PROTOCOL_ERROR"
        and "inconsistent tool block arguments" in event.message
        for event in events
    )
    assert not any(isinstance(event, ToolCallEvent | TextDoneEvent) for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initial", "arguments", "expected"),
    [
        ({}, '{"value":true}', {"value": True}),
        ({}, "{}", {}),
        ({"value": True}, "", {"value": True}),
        ({"value": 1}, ' {"value":1.0} ', {"value": 1.0}),
        (
            {"flag": False, "value": {"nested": 1}},
            '{"value":{"nested":1.0},"flag":false}',
            {"value": {"nested": 1.0}, "flag": False},
        ),
    ],
)
async def test_messages_preserve_empty_placeholders_and_equivalent_json_arguments(
    initial: dict[str, object], arguments: str, expected: dict[str, object]
) -> None:
    events = await generate("messages", messages_with_tool_input(initial, arguments))
    assert not any(isinstance(event, ErrorEvent) for event in events)
    calls = [event for event in events if isinstance(event, ToolCallEvent)]
    assert [call.id for call in calls] == ["first", "second"]
    assert calls[0].arguments == {"value": "valid"}
    assert json.dumps(calls[1].arguments, sort_keys=True) == json.dumps(expected, sort_keys=True)
