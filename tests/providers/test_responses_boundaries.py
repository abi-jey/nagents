"""Completed Responses calls must retain the identities and arguments seen on the wire."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from aiohttp import web

from nagents.events import ErrorEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.events import ToolResultEvent
from nagents.harness import Harness
from nagents.harness import HarnessConfig
from nagents.provider.openai import OpenAIProvider
from nagents.types import Message
from tests.providers.test_gateway_provider import endpoint
from tests.providers.test_gateway_provider import sse
from tests.providers.test_openai_provider import call_item
from tests.providers.test_openai_provider import completion
from tests.providers.test_openai_provider import credentials
from tests.providers.test_openai_provider import endpoint as codex_endpoint
from tests.providers.test_openai_provider import text_item
from tests.providers.test_tool_stream_boundaries import generate
from tests.support.config import connection

if TYPE_CHECKING:
    from pathlib import Path

    from nagents.events import Event
    from nagents.harness.types import ApprovalRequest


def identity_reset(key: str = "name", empty: object = "") -> list[dict[str, object]]:
    initial = {**call_item(), "arguments": "", "status": "in_progress"}
    final = {**call_item(), "name": "other_tool"} if key == "name" else call_item()
    return [
        {"type": "response.output_item.added", "output_index": 0, "item": initial},
        {"type": "response.output_item.done", "output_index": 0, "item": {**initial, key: empty}},
        completion([final]),
    ]


async def response_events(monkeypatch: pytest.MonkeyPatch, route: str, wire: list[dict[str, object]]) -> list[Event]:
    if route == "responses":
        return await generate(route, [*wire])

    async def handle(request: web.Request) -> web.Response:
        return web.Response(body=sse([*wire]), content_type="text/event-stream")

    async with codex_endpoint(monkeypatch, handle), OpenAIProvider(credentials) as provider:
        return [event async for event in provider.generate([Message(role="user", content="Run work")])]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["responses", "codex"])
@pytest.mark.parametrize(
    "fault", ["reclassified", "changed", "omitted", "identity", "completed-text", "late-delta", "empty-id"]
)
async def test_malformed_text_in_a_completed_turn_blocks_all_tool_release(
    monkeypatch: pytest.MonkeyPatch, route: str, fault: str
) -> None:
    delta: dict[str, object] = {
        "type": "response.output_text.delta",
        "output_index": 0,
        "content_index": 0,
        "item_id": "message-a",
        "delta": "streamed",
    }
    message = text_item("streamed")
    wire = [delta]
    if fault == "changed":
        message = text_item("replacement")
    elif fault == "omitted":
        message["content"] = []
    elif fault == "identity":
        message["id"] = "another-message"
    elif fault == "completed-text":
        message = text_item("replacement")
        wire.append(
            {
                "type": "response.output_text.done",
                "output_index": 0,
                "content_index": 0,
                "item_id": "message-a",
                "text": "replacement",
            }
        )
    elif fault == "late-delta":
        message = text_item("streamed more")
        wire.extend(
            [
                {
                    "type": "response.output_text.done",
                    "output_index": 0,
                    "content_index": 0,
                    "text": "streamed",
                },
                {**delta, "delta": " more"},
            ]
        )
    elif fault == "empty-id":
        delta["item_id"] = ""
    output = [{**call_item(), "id": "message-a"}] if fault == "reclassified" else [message, call_item()]
    wire.append(completion(output))
    events = await response_events(monkeypatch, route, wire)
    assert any(isinstance(event, ErrorEvent) for event in events)
    assert not any(isinstance(event, ToolCallEvent | TextDoneEvent) for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["responses", "codex"])
@pytest.mark.parametrize("status", ["in_progress", "incomplete"])
async def test_explicit_unfinished_message_status_blocks_other_completed_tools(
    monkeypatch: pytest.MonkeyPatch, route: str, status: str
) -> None:
    events = await response_events(monkeypatch, route, [completion([call_item(), {**text_item(), "status": status}])])
    assert any(isinstance(event, ErrorEvent) and "unfinished message" in event.message for event in events)
    assert not any(isinstance(event, ToolCallEvent | TextDoneEvent) for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["responses", "codex"])
@pytest.mark.parametrize("mode", ["terminal", "streamed", "item-done"])
@pytest.mark.parametrize("text", ["", "ready"])
async def test_valid_text_and_tools_preserve_optional_metadata_and_empty_terminal_output(
    monkeypatch: pytest.MonkeyPatch, route: str, mode: str, text: str
) -> None:
    message = text_item(text)
    wire: list[dict[str, object]] = []
    if mode == "terminal":
        message.pop("status")
    elif mode == "streamed":
        message["status"] = None
        for fragment in (text[:2], text[2:]):
            wire.append(
                {
                    "type": "response.output_text.delta",
                    "output_index": 0,
                    "content_index": 0,
                    "item_id": "message-a",
                    "delta": fragment,
                }
            )
        wire.append({"type": "response.output_text.done", "output_index": 0, "content_index": 0, "text": text})
    else:
        wire.extend(
            [
                {"type": "response.output_item.done", "output_index": 0, "item": message},
                {"type": "response.output_item.done", "output_index": 1, "item": call_item()},
            ]
        )
    wire.append(completion([] if mode == "item-done" else [message, call_item()]))
    events = await response_events(monkeypatch, route, wire)
    assert not any(isinstance(event, ErrorEvent) for event in events)
    calls = [event for event in events if isinstance(event, ToolCallEvent)]
    assert len(calls) == 1 and calls[0].arguments == {"value": "hello"}
    assert [event.text for event in events if isinstance(event, TextDoneEvent)] == [text]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["responses", "codex"])
@pytest.mark.parametrize("source", ["delta", "arguments-done", "item-done", "item-added"])
@pytest.mark.parametrize(
    ("original", "replacement"),
    [
        ('{"value":true}', '{"value":1}'),
        ('{"value":false}', '{"value":0}'),
        ('{"value":[{"flag":true}]}', '{"value":[{"flag":1}]}'),
        ('{"value":{"flag":false}}', '{"value":{"flag":0}}'),
    ],
)
async def test_responses_argument_consistency_preserves_nested_json_types(
    monkeypatch: pytest.MonkeyPatch, route: str, source: str, original: str, replacement: str
) -> None:
    wire: list[dict[str, object]] = [
        {"type": "response.output_item.added", "output_index": 0, "item": {**call_item(""), "status": "in_progress"}}
    ]
    if source == "item-added":
        wire = [
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {**call_item(original), "status": "in_progress"},
            }
        ]
    elif source == "item-done":
        wire.append({"type": "response.output_item.done", "output_index": 0, "item": call_item(original)})
    else:
        done = source == "arguments-done"
        wire.append(
            {
                "type": "response.function_call_arguments.done" if done else "response.function_call_arguments.delta",
                "output_index": 0,
                "item_id": "item-a",
                "arguments" if done else "delta": original,
            }
        )
    wire.append(completion([call_item(replacement)]))
    events = await response_events(monkeypatch, route, wire)
    assert any(isinstance(event, ErrorEvent) for event in events)
    assert not any(isinstance(event, ToolCallEvent | TextDoneEvent) for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["responses", "codex"])
@pytest.mark.parametrize("kind", ["message", "reasoning"])
@pytest.mark.parametrize("orphan_index", [0, 1])
async def test_completed_arguments_require_a_function_call_before_any_other_tool_is_released(
    monkeypatch: pytest.MonkeyPatch, route: str, kind: str, orphan_index: int
) -> None:
    orphan: dict[str, object] = (
        text_item() if kind == "message" else {"type": "reasoning", "id": "reasoning-a", "summary": []}
    )
    output = [orphan, call_item()] if orphan_index == 0 else [call_item(), orphan]
    events = await response_events(
        monkeypatch,
        route,
        [
            {
                "type": "response.function_call_arguments.done",
                "output_index": orphan_index,
                "item_id": orphan["id"],
                "arguments": "{}",
            },
            completion(output),
        ],
    )
    assert any(isinstance(event, ErrorEvent) and "without a" in event.message for event in events)
    assert not any(isinstance(event, ToolCallEvent | TextDoneEvent) for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("item_id", ["", None, False, 0])
@pytest.mark.parametrize("done", [False, True])
async def test_responses_explicit_argument_item_ids_cannot_be_empty_or_nonstring(item_id: object, done: bool) -> None:
    events = await generate(
        "responses",
        [
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {**call_item(""), "status": "in_progress"},
            },
            {
                "type": "response.function_call_arguments.done" if done else "response.function_call_arguments.delta",
                "output_index": 0,
                "item_id": item_id,
                "arguments" if done else "delta": '{"value":"hello"}',
            },
            completion([call_item()]),
        ],
    )
    assert any(isinstance(event, ErrorEvent) and event.code == "PROVIDER_PROTOCOL_ERROR" for event in events)
    assert not any(isinstance(event, ToolCallEvent | TextDoneEvent) for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("item_id", ["", None, False, 0])
@pytest.mark.parametrize(
    "kind",
    ["response.output_text.delta", "response.output_text.done", "response.refusal.delta", "response.refusal.done"],
)
async def test_responses_explicit_text_item_ids_are_validated_before_tool_release(item_id: object, kind: str) -> None:
    final = text_item()
    if "refusal" in kind:
        final["content"] = [{"type": "refusal", "refusal": "Hello"}]
    value_key = "delta" if kind.endswith("delta") else "refusal" if "refusal" in kind else "text"
    events = await generate(
        "responses",
        [
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {**final, "status": "in_progress", "content": []},
            },
            {"type": kind, "output_index": 0, "content_index": 0, "item_id": item_id, value_key: "Hello"},
            completion([final, call_item()]),
        ],
    )
    assert any(isinstance(event, ErrorEvent) and event.code == "PROVIDER_PROTOCOL_ERROR" for event in events)
    assert not any(isinstance(event, ToolCallEvent | TextDoneEvent) for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["responses", "codex"])
@pytest.mark.parametrize("terminal_output", [False, True])
@pytest.mark.parametrize(
    ("initial", "final"),
    [("{}", "{}"), ('{"value":[true,1],"flag":false}', ' {"flag":false,"value":[true,1.0]} ')],
)
async def test_complete_initial_arguments_and_omitted_event_ids_preserve_equivalent_calls(
    monkeypatch: pytest.MonkeyPatch, route: str, terminal_output: bool, initial: str, final: str
) -> None:
    events = await response_events(
        monkeypatch,
        route,
        [
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {**call_item(initial), "status": "in_progress"},
            },
            {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": initial[:5]},
            {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": initial[5:]},
            {"type": "response.function_call_arguments.done", "output_index": 0, "arguments": final},
            {"type": "response.output_item.done", "output_index": 0, "item": call_item(final)},
            completion([call_item(final)] if terminal_output else []),
        ],
    )
    assert not any(isinstance(event, ErrorEvent) for event in events)
    calls = [event for event in events if isinstance(event, ToolCallEvent)]
    assert len(calls) == 1 and calls[0].id == "call-a"
    assert calls[0].arguments == ({} if initial == "{}" else {"flag": False, "value": [True, 1.0]})


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["responses", "codex"])
@pytest.mark.parametrize("original", ["{}", '{"value":"original"}'])
@pytest.mark.parametrize("cleared", ["", {}, None, False, 0])
async def test_completed_arguments_cannot_be_cleared_before_terminal_replacement(
    monkeypatch: pytest.MonkeyPatch, route: str, original: str, cleared: object
) -> None:
    wire = [
        {"type": "response.output_item.added", "output_index": 0, "item": {**call_item(""), "status": "in_progress"}},
        {
            "type": "response.function_call_arguments.done",
            "output_index": 0,
            "item_id": "item-a",
            "arguments": original,
        },
        {"type": "response.output_item.done", "output_index": 0, "item": {**call_item(), "arguments": cleared}},
        completion([call_item('{"value":"replacement"}')]),
    ]
    events = await response_events(monkeypatch, route, wire)
    assert any(isinstance(event, ErrorEvent) for event in events)
    assert not any(isinstance(event, ToolCallEvent | TextDoneEvent) for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["responses", "codex"])
@pytest.mark.parametrize("completion_kind", ["response.function_call_arguments.done", "response.output_item.done"])
async def test_empty_arguments_are_only_an_initial_placeholder(
    monkeypatch: pytest.MonkeyPatch, route: str, completion_kind: str
) -> None:
    done: dict[str, object] = {"type": completion_kind, "output_index": 0}
    if completion_kind == "response.function_call_arguments.done":
        done.update(item_id="item-a", arguments="")
    else:
        done["item"] = call_item("")
    events = await response_events(monkeypatch, route, [done, completion([call_item()])])
    assert any(isinstance(event, ErrorEvent) for event in events)
    assert not any(isinstance(event, ToolCallEvent | TextDoneEvent) for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["responses", "codex"])
async def test_equivalent_arguments_preserve_whitespace_key_order_and_json_number_semantics(
    monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    original = '{"b": [true, false, null], "a": {"x": 1}}'
    final = '{"a":{"x":1.0},"b":[true,false,null]}'
    wire: list[dict[str, object]] = [
        {"type": "response.output_item.added", "output_index": 0, "item": {**call_item(""), "status": "in_progress"}},
        {"type": "response.function_call_arguments.delta", "output_index": 0, "item_id": "item-a", "delta": original},
        {
            "type": "response.function_call_arguments.done",
            "output_index": 0,
            "item_id": "item-a",
            "arguments": original,
        },
    ]
    # Repeated equivalent item completions must not accumulate every snapshot.
    wire.extend(
        {"type": "response.output_item.done", "output_index": 0, "item": call_item(final if index % 2 else original)}
        for index in range(1025)
    )
    wire.append(completion([call_item(final)]))
    events = await response_events(monkeypatch, route, wire)
    assert not any(isinstance(event, ErrorEvent) for event in events)
    calls = [event for event in events if isinstance(event, ToolCallEvent)]
    assert len(calls) == 1 and calls[0].arguments == {"a": {"x": 1.0}, "b": [True, False, None]}


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["id", "call_id", "name", "type"])
@pytest.mark.parametrize("empty", ["", None, False, 0])
async def test_responses_cannot_clear_then_replace_a_known_output_identity(key: str, empty: object) -> None:
    events = await generate("responses", [*identity_reset(key, empty)])
    assert any(isinstance(event, ErrorEvent) and "identities" in event.message for event in events)
    assert not any(isinstance(event, ToolCallEvent | TextDoneEvent) for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    ["item-arguments", "done-arguments", "repeated-item-arguments", "cancelled", "early-delta-id", "early-done-id"],
)
async def test_codex_rejects_conflicting_completed_calls_before_releasing_tools(
    monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    original = call_item('{"value":"original"}')
    final = call_item('{"value":"replacement"}')
    if fault == "item-arguments":
        wire = [{"type": "response.output_item.done", "output_index": 0, "item": original}]
    elif fault == "repeated-item-arguments":
        wire = [
            {"type": "response.output_item.done", "output_index": 0, "item": original},
            {"type": "response.output_item.done", "output_index": 0, "item": final},
        ]
    elif fault == "done-arguments":
        wire = [
            {
                "type": "response.function_call_arguments.done",
                "output_index": 0,
                "item_id": original["id"],
                "arguments": original["arguments"],
            }
        ]
    elif fault == "cancelled":
        wire = [{"type": "response.cancelled", "response": {"status": "cancelled"}}]
    else:
        done = fault == "early-done-id"
        wire = [
            {
                "type": "response.function_call_arguments.done" if done else "response.function_call_arguments.delta",
                "output_index": 0,
                "item_id": "another-item",
                "arguments" if done else "delta": final["arguments"],
            }
        ]
    wire.append(completion([final]))

    async def handle(request: web.Request) -> web.Response:
        return web.Response(body=sse([*wire]), content_type="text/event-stream")

    async with codex_endpoint(monkeypatch, handle), OpenAIProvider(credentials) as provider:
        events = [event async for event in provider.generate([Message(role="user", content="Run work")])]
    assert any(isinstance(event, ErrorEvent) and event.code == "CODEX_STREAM_INVALID" for event in events)
    assert not any(isinstance(event, ToolCallEvent | TextDoneEvent) for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_output", [False, True])
@pytest.mark.parametrize("arguments", ["{}", '{"value": "hello"}'])
async def test_codex_preserves_matching_early_ids_and_completed_arguments(
    monkeypatch: pytest.MonkeyPatch, terminal_output: bool, arguments: str
) -> None:
    final = call_item(arguments)
    wire: list[dict[str, object] | str] = [
        {
            "type": "response.function_call_arguments.delta",
            "output_index": 0,
            "item_id": final["id"],
            "delta": arguments[:1],
        },
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {**final, "arguments": "", "status": "in_progress"},
        },
        {
            "type": "response.function_call_arguments.delta",
            "output_index": 0,
            "item_id": final["id"],
            "delta": arguments[1:],
        },
        {
            "type": "response.function_call_arguments.done",
            "output_index": 0,
            "item_id": final["id"],
            "arguments": arguments,
        },
        {"type": "response.output_item.done", "output_index": 0, "item": final},
        completion([final] if terminal_output else []),
    ]

    async def handle(request: web.Request) -> web.Response:
        return web.Response(body=sse(wire), content_type="text/event-stream")

    async with codex_endpoint(monkeypatch, handle), OpenAIProvider(credentials) as provider:
        events = [event async for event in provider.generate([Message(role="user", content="Run work")])]
    assert not any(isinstance(event, ErrorEvent) for event in events)
    calls = [event for event in events if isinstance(event, ToolCallEvent)]
    assert len(calls) == 1 and calls[0].id == "call-a" and calls[0].name == "work"
    assert calls[0].arguments == ({} if arguments == "{}" else {"value": "hello"})


@pytest.mark.requires_posix
@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["", "identity", "arguments"])
async def test_real_harness_releases_only_validated_calls_even_when_tool_approval_is_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    monkeypatch.setenv("NGN_BOUNDARY_TEST_KEY", "fixture")
    target = tmp_path / "marker.txt"
    requests = 0
    approvals: list[str] = []
    final = {**call_item(), "name": "other_tool"}
    wire = identity_reset() if fault == "identity" else [completion([final])]
    if fault == "arguments":
        wire = [
            {
                "type": "response.function_call_arguments.done",
                "output_index": 0,
                "item_id": "item-a",
                "arguments": '{"value":"original"}',
            },
            {"type": "response.output_item.done", "output_index": 0, "item": {**final, "arguments": ""}},
            completion([final]),
        ]

    async def handle(request: web.Request) -> web.Response:
        nonlocal requests
        assert request.path == "/responses" and request.method == "POST"
        requests += 1
        return web.Response(body=sse([*wire] if requests == 1 else [completion([])]), content_type="text/event-stream")

    def other_tool(value: str) -> str:
        """Write a harmless marker in the temporary test workspace."""
        target.write_text(value)
        return value

    async def approve(request: ApprovalRequest) -> bool:
        approvals.append(request.tool)
        return True

    async with endpoint(handle) as url:
        harness = Harness(
            HarnessConfig(
                workspace=tmp_path,
                data_dir=tmp_path / "state",
                model="fixture",
                providers=connection(base_url=url, api="responses", api_key_env="NGN_BOUNDARY_TEST_KEY"),
            )
        )
        harness.agent.compactor = None
        harness.agent.register_tool(other_tool)
        harness.approval_handler = approve
        try:
            events = [event async for event in harness.run("Run the fixture work")]
            if fault:
                assert not target.exists() and approvals == [] and requests == 1
                assert not any(isinstance(event, ToolCallEvent | ToolResultEvent) for event in events)
                assert any(
                    isinstance(event, ErrorEvent) and event.code == "PROVIDER_PROTOCOL_ERROR" for event in events
                )
                assert not any(message.tool_calls or message.role == "tool" for message in await harness.history())
            else:
                assert target.read_text() == "hello" and approvals == ["other_tool"] and requests == 2
                assert any(isinstance(event, ToolCallEvent) for event in events)
                assert any(isinstance(event, ToolResultEvent) and not event.error for event in events)
                assert not any(isinstance(event, ErrorEvent) for event in events)
        finally:
            await harness.close()
