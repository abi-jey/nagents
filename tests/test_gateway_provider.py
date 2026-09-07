"""API-key gateway integration tests. All HTTP stays on a local fixture server."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from aiohttp import web

from nagents import Agent
from nagents import SessionManager
from nagents.adapters._validation import list_data
from nagents.adapters._validation import object_data
from nagents.adapters.responses import ResponseAccumulator
from nagents.events import DoneEvent
from nagents.events import ErrorEvent
from nagents.events import FinishReason
from nagents.events import RateLimitEvent
from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.events import ToolResultEvent
from nagents.harness.config import HarnessConfig
from nagents.harness.provider import HarnessProvider
from nagents.http import HTTPLogger
from nagents.provider import Provider
from nagents.provider import ProviderType
from nagents.provider import gateway
from nagents.types import AudioContent
from nagents.types import DocumentContent
from nagents.types import GenerationConfig
from nagents.types import ImageContent
from nagents.types import Message
from nagents.types import RetryConfig
from nagents.types import TextContent
from nagents.types import ToolCall
from nagents.types import ToolDefinition

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from collections.abc import Awaitable
    from collections.abc import Callable
    from pathlib import Path

    from nagents.events import Event


KEY = "fake-gateway-key-not-a-credential"
LEAK = "fake-upstream-secret-must-not-escape"
APIS = ["chat_completions", "responses", "messages", "completions"]
TOOL = ToolDefinition(
    "work", "Uppercase text", {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]}
)


@asynccontextmanager
async def endpoint(handler: Callable[[web.Request], Awaitable[web.StreamResponse]]) -> AsyncIterator[str]:
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handler)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        yield f"http://127.0.0.1:{runner.addresses[0][1]}"
    finally:
        await runner.cleanup()


def response_item(call: ToolCall) -> dict[str, object]:
    return {
        "type": "function_call",
        "id": f"item-{call.id}",
        "call_id": call.id,
        "name": call.name,
        "arguments": json.dumps(call.arguments),
        "status": "completed",
    }


def text_item(text: str) -> dict[str, object]:
    return {
        "id": "msg-1",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text}],
    }


def payload(api: str, text: str = "Hello", calls: tuple[ToolCall, ...] = ()) -> dict[str, object]:
    if api == "responses":
        return {
            "id": "resp-1",
            "status": "completed",
            "output": [text_item(text), *(response_item(call) for call in calls)],
            "usage": {
                "input_tokens": 12,
                "output_tokens": 4,
                "input_tokens_details": {"cached_tokens": 3},
                "output_tokens_details": {"reasoning_tokens": 2},
            },
        }
    if api == "messages":
        return {
            "id": "msg-1",
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "text", "text": text},
                *({"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments} for call in calls),
            ],
            "stop_reason": "tool_use" if calls else "end_turn",
            "usage": {"input_tokens": 9, "cache_read_input_tokens": 3, "output_tokens": 4},
        }
    choice: dict[str, object] = {"index": 0, "finish_reason": "tool_calls" if calls else "stop"}
    if api == "completions":
        choice["text"] = text
    else:
        choice["message"] = {
            "role": "assistant",
            "content": text,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
                }
                for call in calls
            ],
        }
    return {
        "id": "chat-1",
        "choices": [choice],
        "usage": {
            "prompt_tokens": 12,
            "completion_tokens": 4,
            "prompt_tokens_details": {"cached_tokens": 3},
            "completion_tokens_details": {"reasoning_tokens": 2},
        },
    }


def frames(api: str, result: dict[str, object]) -> list[dict[str, object] | str]:
    if api == "responses":
        events: list[dict[str, object] | str] = [
            {"type": "response.created", "response": {"id": "resp-1", "status": "in_progress"}}
        ]
        for index, raw in enumerate(list_data(result["output"])):
            item = object_data(raw)
            initial = {**item, "status": "in_progress"}
            if item["type"] == "function_call":
                initial["arguments"] = ""
            else:
                initial["content"] = []
            events.append({"type": "response.output_item.added", "output_index": index, "item": initial})
            if item["type"] == "function_call":
                args = str(item["arguments"])
                for delta in (args[:5], args[5:]):
                    events.append(
                        {
                            "type": "response.function_call_arguments.delta",
                            "output_index": index,
                            "item_id": item["id"],
                            "delta": delta,
                        }
                    )
                events.append(
                    {
                        "type": "response.function_call_arguments.done",
                        "output_index": index,
                        "item_id": item["id"],
                        "arguments": args,
                    }
                )
            else:
                text = str(object_data(list_data(item["content"])[0])["text"])
                for delta in (text[:2], text[2:]):
                    events.append(
                        {
                            "type": "response.output_text.delta",
                            "output_index": index,
                            "content_index": 0,
                            "item_id": item["id"],
                            "delta": delta,
                        }
                    )
                events.append(
                    {"type": "response.output_text.done", "output_index": index, "content_index": 0, "text": text}
                )
            events.append({"type": "response.output_item.done", "output_index": index, "item": item})
        events.append({"type": "response.completed", "response": result})
        return events
    if api == "messages":
        events = [
            {
                "type": "message_start",
                "message": {
                    "id": "msg-1",
                    "usage": {"input_tokens": 9, "cache_read_input_tokens": 3, "output_tokens": 0},
                },
            }
        ]
        for index, raw in enumerate(list_data(result["content"])):
            block = object_data(raw)
            events.append(
                {"type": "content_block_start", "index": index, "content_block": {**block, "input": {}, "text": ""}}
            )
            if block["type"] == "tool_use":
                args = json.dumps(block["input"])
                for delta in (args[:5], args[5:]):
                    events.append(
                        {
                            "type": "content_block_delta",
                            "index": index,
                            "delta": {"type": "input_json_delta", "partial_json": delta},
                        }
                    )
            else:
                events.append(
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "text_delta", "text": block["text"]},
                    }
                )
            events.append({"type": "content_block_stop", "index": index})
        events.extend(
            [
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": result["stop_reason"]},
                    "usage": {"output_tokens": 4},
                },
                {"type": "message_stop"},
            ]
        )
        return events
    choice = object_data(list_data(result["choices"])[0])
    if api == "completions":
        events = [{"choices": [{"index": 0, "text": choice["text"], "finish_reason": None}]}]
    else:
        message = object_data(choice["message"])
        events = [{"choices": [{"index": 0, "delta": {"content": message["content"]}, "finish_reason": None}]}]
        # Interleave argument fragments for concurrent calls.
        for half in (0, 1):
            for index, raw in enumerate(list_data(message["tool_calls"])):
                call = object_data(raw)
                function = object_data(call["function"])
                args = str(function["arguments"])
                call_delta: dict[str, object] = {
                    "index": index,
                    "function": {"arguments": args[:5] if half == 0 else args[5:]},
                }
                if half == 0:
                    call_delta.update(id=call["id"], function={"name": function["name"], "arguments": args[:5]})
                events.append({"choices": [{"index": 0, "delta": {"tool_calls": [call_delta]}, "finish_reason": None}]})
    terminal = {
        "index": 0,
        "finish_reason": choice["finish_reason"],
        "text" if api == "completions" else "delta": "" if api == "completions" else {"content": ""},
    }
    # OpenRouter repeats the terminal finish_reason on its usage-only chunk.
    events.extend([{"choices": [terminal]}, {"choices": [terminal], "usage": result["usage"]}, "[DONE]"])
    return events


def sse(events: list[dict[str, object] | str]) -> bytes:
    output = ": OPENROUTER PROCESSING\r\n\r\n"
    for event in events:
        if isinstance(event, str):
            output += f"data:{event}\r\n\r\n"
        else:
            # Multi-line data frames and UTF-8 split over arbitrary network chunks.
            data = json.dumps(event, ensure_ascii=False, indent=2)
            output += (
                "event: ignored-by-data-parser\r\n"
                + "\r\n".join(f"data: {line}" for line in data.splitlines())
                + "\r\n\r\n"
            )
    return output.encode()


async def reply(request: web.Request, api: str, result: dict[str, object], stream: bool) -> web.StreamResponse:
    if not stream:
        return web.json_response(result)
    response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
    await response.prepare(request)
    data = sse(frames(api, result))
    for offset in range(0, len(data), 17):
        await response.write(data[offset : offset + 17])
    await response.write_eof()
    return response


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("prefix", ["", "/v1/", "/proxy/v1/"])
def test_litellm_routes_real_http(api: str, stream: bool, prefix: str) -> None:
    async def scenario() -> None:
        seen: list[dict[str, object]] = []

        async def handle(request: web.Request) -> web.StreamResponse:
            route = "chat/completions" if api == "chat_completions" else api
            expected = prefix.rstrip("/") + ("/v1" if api == "messages" and not prefix else "") + "/" + route
            assert request.path == expected and "/v1/v1" not in request.path
            assert request.headers["Authorization"] == f"Bearer {KEY}"
            assert not {"x-api-key", "ChatGPT-Account-Id", "originator"} & request.headers.keys()
            if api == "messages":
                assert request.headers["anthropic-version"] == "2023-06-01"
            body = object_data(await request.json())
            seen.append(body)
            assert body["stream"] is stream and body["model"] == "fixture-model"
            if stream and api == "chat_completions":
                assert body["stream_options"] == {"include_usage": True}
            if api == "completions":
                assert "stream_options" not in body
            assert body["temperature"] == 0.2 and body["top_p"] == 0.9
            token_key = {"chat_completions": "max_completion_tokens", "responses": "max_output_tokens"}.get(
                api, "max_tokens"
            )
            assert body[token_key] == 40
            return await reply(request, api, payload(api, "H\u00e9llo"), stream)

        async with endpoint(handle) as url:
            async with Provider(ProviderType.LITELLM, KEY, "fixture-model", base_url=url + prefix, api=api) as provider:
                events = [
                    event
                    async for event in provider.generate(
                        [Message(role="user", content="Hello")],
                        config=GenerationConfig(temperature=0.2, top_p=0.9, max_tokens=40),
                        stream=stream,
                    )
                ]
            assert len(seen) == 1 and not any(isinstance(event, ErrorEvent) for event in events)
            done = events[-1]
            assert (
                isinstance(done, TextDoneEvent)
                and done.text == "H\u00e9llo"
                and done.finish_reason == FinishReason.STOP
            )
            assert (
                done.usage.prompt_tokens,
                done.usage.completion_tokens,
                done.usage.total_tokens,
                done.usage.cached_tokens,
            ) == (12, 4, 16, 3)
            assert bool([event for event in events if isinstance(event, TextChunkEvent)]) is stream
            body = seen[0]
            if api == "completions":
                assert body["prompt"] == "Hello" and "messages" not in body and "tools" not in body
            elif api == "responses":
                assert body["store"] is False and "previous_response_id" not in body
                assert object_data(list_data(body["input"])[0])["role"] == "user"
            else:
                assert object_data(list_data(body["messages"])[0])["role"] == "user"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "provider_type", [ProviderType.LITELLM, ProviderType.OPENAI_COMPATIBLE, ProviderType.OPENROUTER]
)
@pytest.mark.parametrize("stream", [False, True])
def test_legacy_usage_opt_in_excludes_only_litellm(provider_type: ProviderType, stream: bool) -> None:
    """LiteLLM 1.100.0 raises MockValSer on an upstream legacy usage-only chunk.

    Do not request that optional chunk from LiteLLM. Other providers retain the
    opt-in, and absence of optional usage must not prevent a completed response.
    Error envelopes and missing terminals remain errors in the stream tests.
    """

    async def scenario() -> None:
        requests: list[dict[str, object]] = []

        async def handle(request: web.Request) -> web.StreamResponse:
            assert request.path == "/v1/completions"
            body = object_data(await request.json())
            requests.append(body)
            result = payload("completions")
            if stream and provider_type == ProviderType.LITELLM:
                chunks = [
                    chunk for chunk in frames("completions", result) if isinstance(chunk, str) or "usage" not in chunk
                ]
                return web.Response(body=sse(chunks), content_type="text/event-stream")
            return await reply(request, "completions", result, stream)

        async with endpoint(handle) as url:
            async with Provider(provider_type, KEY, "fixture", base_url=url + "/v1", api="completions") as provider:
                events = [
                    event async for event in provider.generate([Message(role="user", content="Hello")], stream=stream)
                ]
            assert len(requests) == 1
            if stream and provider_type != ProviderType.LITELLM:
                assert requests[0]["stream_options"] == {"include_usage": True}
            else:
                assert "stream_options" not in requests[0]
            assert not any(isinstance(event, ErrorEvent) for event in events)
            done = events[-1]
            assert isinstance(done, TextDoneEvent) and done.text == "Hello" and done.finish_reason == FinishReason.STOP
            assert done.usage.total_tokens == (0 if stream and provider_type == ProviderType.LITELLM else 16)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "api,provider_type",
    [
        ("chat_completions", ProviderType.LITELLM),
        ("responses", ProviderType.LITELLM),
        ("messages", ProviderType.LITELLM),
        ("auto", ProviderType.OPENROUTER),
    ],
)
@pytest.mark.parametrize("stream", [False, True])
def test_agent_tool_loop_and_persisted_continuation(
    tmp_path: Path, api: str, provider_type: ProviderType, stream: bool
) -> None:
    async def scenario() -> None:
        route = "chat_completions" if api == "auto" else api
        requests: list[dict[str, object]] = []
        calls = (
            ToolCall("call-a", "work", {"value": 'h\u00e9llo\n"quoted"'}),
            ToolCall("call-b", "work", {"value": "second"}),
        )

        async def handle(request: web.Request) -> web.StreamResponse:
            if request.method == "GET":
                assert request.path == "/v1/models"
                return web.json_response({"data": [{"id": "fixture-model"}]})
            body = object_data(await request.json())
            requests.append(body)
            if provider_type == ProviderType.OPENROUTER:
                assert request.headers["HTTP-Referer"] == "https://github.com/nagents"
            assert body["tools"]
            if len(requests) == 1:
                result = payload(route, "Calling work", calls)
            else:
                assert "H\\u00c9LLO" in json.dumps(body)
                result = payload(route, "Finished")
            return await reply(request, route, result, stream)

        async def work(value: str) -> str:
            """Uppercase a local string."""
            return value.upper()

        async with endpoint(handle) as url:
            provider = Provider(provider_type, KEY, "fixture-model", base_url=url + "/v1", api=api)
            agent = Agent(
                provider, SessionManager(tmp_path / "fixture.db"), tools=[work], compactor=None, streaming=stream
            )
            try:
                events = [event async for event in agent.run("Use work", session_id="fixture")]
                assert not any(isinstance(event, ErrorEvent) for event in events)
                assert len(requests) == 2
                assert sum(isinstance(event, ToolResultEvent) for event in events) == 2
                assert isinstance(events[-1], DoneEvent) and events[-1].final_text == "Finished"
                history = await agent.session.get_history("fixture")
                assistant = next(message for message in history if message.tool_calls)
                assert assistant.content == "Calling work" and assistant.tool_calls == list(calls)
                # Replay the persisted messages through a fresh Provider, no in-memory response ID state.
                async with Provider(provider_type, KEY, "fixture-model", base_url=url + "/v1", api=api) as resumed:
                    continuation = [
                        event
                        async for event in resumed.generate(
                            [*history, Message(role="user", content="Continue")], [TOOL], stream=stream
                        )
                    ]
                assert isinstance(continuation[-1], TextDoneEvent)
                assert KEY not in repr(history) and KEY not in repr(events)
                second = requests[1]
                if route == "responses":
                    inputs = [object_data(item) for item in list_data(second["input"])]
                    assert [item["call_id"] for item in inputs if item.get("type") == "function_call_output"] == [
                        "call-a",
                        "call-b",
                    ]
                    assert [
                        json.loads(str(item["arguments"])) for item in inputs if item.get("type") == "function_call"
                    ] == [call.arguments for call in calls]
                elif route == "messages":
                    messages = [object_data(item) for item in list_data(second["messages"])]
                    results = list_data(messages[-1]["content"])
                    assert [object_data(item)["tool_use_id"] for item in results] == ["call-a", "call-b"]
                else:
                    messages = [object_data(item) for item in list_data(second["messages"])]
                    assert [item["tool_call_id"] for item in messages if item["role"] == "tool"] == ["call-a", "call-b"]
            finally:
                await agent.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("status", [401, 429, 503])
def test_http_errors_never_expose_body_headers_or_credentials(
    caplog: pytest.LogCaptureFixture, api: str, stream: bool, status: int
) -> None:
    async def scenario() -> None:
        async def handle(request: web.Request) -> web.Response:
            return web.Response(
                status=status, reason=LEAK, text=LEAK, headers={"X-Upstream-Key": LEAK, "X-Ratelimit-Secret": LEAK}
            )

        async with (
            endpoint(handle) as url,
            Provider(
                ProviderType.LITELLM, KEY, "fixture", base_url=url, api=api, retry_config=RetryConfig(max_retries=0)
            ) as provider,
        ):
            http_logger = Mock(spec=HTTPLogger)
            provider.set_http_logger(http_logger)
            events = [
                event async for event in provider.generate([Message(role="user", content="Hello")], stream=stream)
            ]
            assert len(events) == 1 and isinstance(events[0], ErrorEvent) and events[0].code == str(status)
            assert LEAK not in repr(events) + caplog.text and KEY not in repr(events) + caplog.text
            assert not http_logger.mock_calls

    asyncio.run(scenario())


@pytest.mark.parametrize("api", APIS)
@pytest.mark.parametrize("failure", ["truncated", "malformed", "error", "bad-frame", "wrong-content-type"])
def test_stream_failures_release_no_tools(api: str, failure: str) -> None:
    async def scenario() -> None:
        calls = () if api == "completions" else (ToolCall("call-a", "work", {"value": "hello"}),)
        events = frames(api, payload(api, "partial", calls))
        data = sse(events[:-1])
        if failure == "malformed":
            data += b"data: {not-json}\n\n"
        elif failure == "error":
            data += sse([{"type": "error", "error": {"message": LEAK, "headers": {"Authorization": KEY}}}])
        elif failure == "bad-frame":
            data += b'data: {"type": "unfinished"}'

        async def handle(request: web.Request) -> web.Response:
            return web.Response(
                body=data, content_type="application/json" if failure == "wrong-content-type" else "text/event-stream"
            )

        async with endpoint(handle) as url:
            async with Provider(ProviderType.LITELLM, KEY, "fixture", base_url=url, api=api) as provider:
                output = [
                    event
                    async for event in provider.generate(
                        [Message(role="user", content="Hello")], [TOOL] if calls else None
                    )
                ]
            assert any(isinstance(event, ErrorEvent) for event in output)
            assert not any(isinstance(event, (ToolCallEvent, TextDoneEvent)) for event in output)
            assert LEAK not in repr(output) and KEY not in repr(output)

    asyncio.run(scenario())


@pytest.mark.parametrize("api", ["chat_completions", "responses", "messages"])
@pytest.mark.parametrize("stream", [False, True])
def test_invalid_second_tool_never_releases_first(api: str, stream: bool) -> None:
    async def scenario() -> None:
        result = payload(
            api, "", (ToolCall("a", "work", {"value": "valid"}), ToolCall("b", "work", {"value": "invalid"}))
        )
        if api == "responses":
            object_data(list_data(result["output"])[-1])["arguments"] = '{"partial":'
        elif api == "messages":
            object_data(list_data(result["content"])[-1])["input"] = [LEAK]
        else:
            message = object_data(object_data(list_data(result["choices"])[0])["message"])
            object_data(object_data(list_data(message["tool_calls"])[-1])["function"])["arguments"] = '{"partial":'

        async def handle(request: web.Request) -> web.StreamResponse:
            return await reply(request, api, result, stream)

        async with endpoint(handle) as url:
            async with Provider(ProviderType.LITELLM, KEY, "fixture", base_url=url, api=api) as provider:
                output = [
                    event
                    async for event in provider.generate([Message(role="user", content="Hello")], [TOOL], stream=stream)
                ]
            assert any(isinstance(event, ErrorEvent) for event in output)
            assert not any(isinstance(event, ToolCallEvent) for event in output)
            assert LEAK not in repr(output)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "messages,tools",
    [
        ([Message(role="user", content="Hello")], [TOOL]),
        ([Message(role="system", content="Instructions"), Message(role="user", content="Hello")], []),
        (
            [
                Message(role="user", content="One"),
                Message(role="assistant", content="Two"),
                Message(role="user", content="Three"),
            ],
            [],
        ),
        ([Message(role="assistant", content="Prefix")], []),
        ([Message(role="user", content=[ImageContent("fake", "image/png")])], []),
        ([Message(role="user", content=[AudioContent("fake")])], []),
        ([Message(role="user", content=[DocumentContent("fake")])], []),
        ([Message(role="user", content="Hello", tool_calls=[ToolCall("a", "work", {})])], []),
    ],
)
def test_legacy_rejects_unsupported_input_without_http(messages: list[Message], tools: list[ToolDefinition]) -> None:
    async def scenario() -> None:
        async with Provider(
            ProviderType.LITELLM, KEY, "fixture", base_url="http://127.0.0.1:1", api="completions"
        ) as provider:
            http = Mock(spec=gateway.GatewayHTTPClient)
            provider._http = http
            events = [event async for event in provider.generate(messages, tools)]
            assert len(events) == 1 and isinstance(events[0], ErrorEvent)
            assert "completions API" in events[0].message
            http.post_json.assert_not_called()
            http.post_stream.assert_not_called()

    asyncio.run(scenario())


def test_constructor_and_harness_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="explicit base_url"):
        Provider(ProviderType.LITELLM, KEY, "fixture")
    for api in ["unknown", "RESPONSES"]:
        with pytest.raises(ValueError, match="api must"):
            Provider(ProviderType.OPENAI_COMPATIBLE, KEY, "fixture", api=api)
    for url in [
        "https://user:password@example.test/v1",
        "https://example.test/v1?key=secret",
        "https://example.test/v1/responses",
        "file:///tmp/api",
    ]:
        with pytest.raises(ValueError, match="base_url"):
            Provider(ProviderType.LITELLM, KEY, "fixture", base_url=url)
    for provider_type in [ProviderType.ANTHROPIC, ProviderType.GEMINI_NATIVE]:
        with pytest.raises(ValueError, match="HTTP API"):
            Provider(provider_type, KEY, "fixture", api="responses")
    config = HarnessConfig(
        workspace=tmp_path,
        provider="litellm",
        base_url="http://127.0.0.1:1",
        api="responses",
        api_key_env="LITELLM_API_KEY",
    )
    provider = HarnessProvider(config)
    assert provider.api == "responses" and provider.provider_type == ProviderType.LITELLM
    monkeypatch.setenv("LITELLM_API_KEY", KEY)
    provider.credentials()
    assert provider.api_key == KEY
    config.api = "completions"
    with pytest.raises(ValueError, match=r"coding harness.*text-only"):
        HarnessProvider(config)
    assert Provider(ProviderType.LITELLM, KEY, "fixture", base_url="http://127.0.0.1:1").api == "chat_completions"
    assert Provider(ProviderType.OPENROUTER, KEY, "fixture").base_url == "https://openrouter.ai/api/v1"


@pytest.mark.parametrize("api", APIS)
def test_media_capabilities_match_wire_contract(api: str) -> None:
    provider = Provider(ProviderType.LITELLM, KEY, "gpt-4o-audio-preview", base_url="http://127.0.0.1:1", api=api)
    capabilities = provider.supported_media_formats
    assert bool(capabilities.image_formats) is (api != "completions")
    assert bool(capabilities.document_formats) is (api == "messages")
    assert bool(capabilities.audio_formats) is (api == "chat_completions")


@pytest.mark.parametrize("stream", [False, True])
def test_direct_anthropic_keeps_api_key_auth(stream: bool) -> None:
    async def scenario() -> None:
        async def handle(request: web.Request) -> web.StreamResponse:
            assert request.path == "/v1/messages"
            assert request.headers["x-api-key"] == KEY and "Authorization" not in request.headers
            return await reply(request, "messages", payload("messages"), stream)

        async with endpoint(handle) as url:
            async with Provider(
                ProviderType.ANTHROPIC, KEY, "fixture", base_url=url + "/v1", api="messages"
            ) as provider:
                events = [
                    event async for event in provider.generate([Message(role="user", content="Hello")], stream=stream)
                ]
            assert isinstance(events[-1], TextDoneEvent)

    asyncio.run(scenario())


def test_retry_and_model_verification_stay_on_gateway(caplog: pytest.LogCaptureFixture) -> None:
    async def scenario() -> None:
        attempts = 0

        async def handle(request: web.Request) -> web.Response:
            nonlocal attempts
            assert request.headers["Authorization"] == f"Bearer {KEY}"
            assert not request.query
            if request.method == "GET":
                assert request.path == "/v1/models"
                return web.json_response({"data": [{"id": "fixture"}]})
            attempts += 1
            if attempts == 1:
                return web.Response(status=429, text=LEAK, headers={"Retry-After": "0", "X-Ratelimit-Secret": LEAK})
            return web.json_response(payload("chat_completions"))

        async with endpoint(handle) as url:
            async with Provider(
                ProviderType.LITELLM,
                KEY,
                "fixture",
                base_url=url + "/v1",
                retry_config=RetryConfig(max_retries=1, base_delay=0),
            ) as provider:
                events = [
                    event
                    async for event in provider.generate(
                        [Message(role="user", content="Hello")], stream=False, verify_model=True
                    )
                ]
            assert attempts == 2 and isinstance(events[0], RateLimitEvent) and isinstance(events[-1], TextDoneEvent)
            assert events[0].rate_limit_info == {} and LEAK not in repr(events) + caplog.text

    asyncio.run(scenario())


@pytest.mark.parametrize("stream", [False, True])
def test_redirects_are_not_followed(stream: bool) -> None:
    async def scenario() -> None:
        seen: list[str] = []

        async def handle(request: web.Request) -> web.Response:
            seen.append(request.path)
            return web.Response(status=307, headers={"Location": "/credential-sink"})

        async with endpoint(handle) as url:
            async with Provider(ProviderType.LITELLM, KEY, "fixture", base_url=url, api="responses") as provider:
                events = [
                    event async for event in provider.generate([Message(role="user", content="Hello")], stream=stream)
                ]
            assert seen == ["/responses"] and isinstance(events[-1], ErrorEvent) and events[-1].code == "307"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "fault",
    [
        "identity",
        "arguments",
        "missing-output",
        "failed",
        "incomplete-tool",
        "duplicate-call",
        "non-object",
        "non-finite",
        "duplicate-json-key",
        "unfinished",
        "orphan-args",
        "text-mismatch",
    ],
)
def test_responses_terminal_validation(fault: str) -> None:
    accumulator = ResponseAccumulator()
    call = response_item(ToolCall("a", "work", {"value": "hello"}))
    result: dict[str, object] = {"status": "completed", "output": [call]}
    accumulator.add(
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {**call, "arguments": "", "status": "in_progress"},
        }
    )
    if fault == "identity":
        result["output"] = [{**call, "call_id": "different"}]
    elif fault == "arguments":
        accumulator.add(
            {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": '{"value":"different"}'}
        )
    elif fault == "missing-output":
        result["output"] = []
    elif fault == "failed":
        result["status"] = "failed"
        result["error"] = {"message": LEAK}
    elif fault == "incomplete-tool":
        result.update(status="incomplete", incomplete_details={"reason": "max_output_tokens"})
    elif fault == "duplicate-call":
        result["output"] = [call, call]
    elif fault == "non-object":
        call["arguments"] = "[]"
    elif fault == "non-finite":
        call["arguments"] = '{"value":NaN}'
    elif fault == "duplicate-json-key":
        call["arguments"] = '{"value":1,"value":2}'
    elif fault == "unfinished":
        call["status"] = "in_progress"
    elif fault == "orphan-args":
        accumulator.add({"type": "response.function_call_arguments.delta", "output_index": 1, "delta": "{}"})
    else:
        accumulator.add(
            {"type": "response.output_text.delta", "output_index": 1, "content_index": 0, "delta": "partial"}
        )
        result["output"] = [call, text_item("different")]
    with pytest.raises(ValueError) as error:
        accumulator.finish(result)
    assert LEAK not in str(error.value)


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("reason", ["max_output_tokens", "content_filter"])
def test_responses_incomplete_text_is_not_a_tool_success(stream: bool, reason: str) -> None:
    async def scenario() -> None:
        result = payload("responses", "Partial")
        result.update(status="incomplete", incomplete_details={"reason": reason})

        async def handle(request: web.Request) -> web.Response:
            if stream:
                return web.Response(
                    body=sse([{"type": "response.incomplete", "response": result}]), content_type="text/event-stream"
                )
            return web.json_response(result)

        async with endpoint(handle) as url:
            async with Provider(ProviderType.LITELLM, KEY, "fixture", base_url=url, api="responses") as provider:
                events = [
                    event async for event in provider.generate([Message(role="user", content="Hello")], stream=stream)
                ]
            assert isinstance(events[-1], TextDoneEvent) and events[-1].text == "Partial"
            assert events[-1].finish_reason == (
                FinishReason.LENGTH if reason == "max_output_tokens" else FinishReason.CONTENT_FILTER
            )

    asyncio.run(scenario())


def test_responses_reasoning_metadata_and_role_image_history() -> None:
    from nagents.adapters.responses import format_request

    reasoning = {
        "type": "reasoning",
        "id": "rs-1",
        "summary": [{"type": "summary_text", "text": "Brief summary"}],
        "encrypted_content": "fake-encrypted-state",
    }
    result = payload("responses", "Using tool", (ToolCall("a", "work", {"value": "hello"}),))
    result["output"] = [reasoning, *list_data(result["output"])]
    events = ResponseAccumulator().finish(result)
    event = next(event for event in events if isinstance(event, ToolCallEvent))
    call = ToolCall(event.id, event.name, event.arguments, event.metadata)
    messages = [
        Message(role="system", content="System"),
        Message(role="developer", content="Developer"),
        Message(role="user", content=[TextContent("Look"), ImageContent("ZmFrZQ==", "image/png")]),
        Message(role="assistant", content="Using tool", tool_calls=[call]),
        Message(role="tool", tool_call_id="a", content="HELLO"),
    ]
    # JSON round trip models the string-valued metadata stored by SessionManager.
    call.metadata = json.loads(json.dumps(call.metadata))
    body = format_request("fixture", messages, [TOOL], None, False)
    inputs = [object_data(item) for item in list_data(body["input"])]
    assert [item.get("role") for item in inputs[:3]] == ["system", "developer", "user"]
    assert object_data(list_data(inputs[2]["content"])[1])["type"] == "input_image"
    assert inputs[4] == reasoning and inputs[5]["call_id"] == "a" and inputs[6]["output"] == "HELLO"


@pytest.mark.parametrize("stream", [False, True])
def test_response_size_limits(monkeypatch: pytest.MonkeyPatch, stream: bool) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(gateway, "MAX_EVENT_BYTES", 64)
        monkeypatch.setattr(gateway, "MAX_RESPONSE_BYTES", 128)

        async def handle(request: web.Request) -> web.Response:
            return web.Response(
                body=b"data: " + b"x" * 256, content_type="text/event-stream" if stream else "application/json"
            )

        async with endpoint(handle) as url:
            async with Provider(ProviderType.LITELLM, KEY, "fixture", base_url=url, api="responses") as provider:
                events = [
                    event async for event in provider.generate([Message(role="user", content="Hello")], stream=stream)
                ]
            assert isinstance(events[-1], ErrorEvent) and "size limit" in events[-1].message

    asyncio.run(scenario())


@pytest.mark.parametrize("stream", [False, True])
def test_openrouter_reasoning_details_survive_tool_continuation(stream: bool) -> None:
    async def scenario() -> None:
        requests: list[dict[str, object]] = []
        details = [
            {
                "type": "reasoning.encrypted",
                "id": "rs-a",
                "index": 0,
                "format": "anthropic-claude-v1",
                "data": "opaque-state",
            }
        ]

        async def handle(request: web.Request) -> web.Response:
            body = object_data(await request.json())
            requests.append(body)
            assert body["reasoning"] == {"enabled": True}
            result = payload("chat_completions", "Working", (ToolCall("a", "work", {"value": "hello"}),))
            if stream:
                chunks = frames("chat_completions", result)
                chunks.insert(
                    0,
                    {
                        "choices": [
                            {
                                "delta": {
                                    "reasoning": "Summary",
                                    "reasoning_details": [{**details[0], "data": "opaque-"}],
                                }
                            }
                        ]
                    },
                )
                chunks.insert(1, {"choices": [{"delta": {"reasoning_details": [{**details[0], "data": "state"}]}}]})
                return web.Response(body=sse(chunks), content_type="text/event-stream")
            object_data(object_data(list_data(result["choices"])[0])["message"])["reasoning_details"] = details
            return web.json_response(result)

        async with endpoint(handle) as url:
            async with Provider(ProviderType.OPENROUTER, KEY, "fixture", base_url=url + "/v1") as provider:
                messages = [Message(role="user", content="Use work")]
                config = GenerationConfig(reasoning={"enabled": True})
                first = [event async for event in provider.generate(messages, [TOOL], config, stream=stream)]
                call = next(event for event in first if isinstance(event, ToolCallEvent))
                assert json.loads(call.metadata["reasoning_details"]) == details
                messages.extend(
                    [
                        Message(
                            role="assistant", tool_calls=[ToolCall(call.id, call.name, call.arguments, call.metadata)]
                        ),
                        Message(role="tool", tool_call_id=call.id, content="HELLO"),
                    ]
                )
                second = [event async for event in provider.generate(messages, [TOOL], config, stream=stream)]
            assert not any(isinstance(event, ErrorEvent) for event in second)
            assert object_data(list_data(requests[-1]["messages"])[1])["reasoning_details"] == details

    asyncio.run(scenario())


@pytest.mark.parametrize("api", ["chat_completions", "completions"])
def test_chat_rejects_generation_after_finish(api: str) -> None:
    async def scenario() -> None:
        chunks = frames(api, payload(api))
        chunks.insert(-1, {"choices": [{"finish_reason": "length"}]})

        async def handle(request: web.Request) -> web.Response:
            return web.Response(body=sse(chunks), content_type="text/event-stream")

        async with endpoint(handle) as url:
            async with Provider(ProviderType.LITELLM, KEY, "fixture", base_url=url, api=api) as provider:
                events = [event async for event in provider.generate([Message(role="user", content="Hello")])]
            assert isinstance(events[-1], ErrorEvent)
            assert not any(isinstance(event, (TextDoneEvent, ToolCallEvent)) for event in events)

    asyncio.run(scenario())


@pytest.mark.parametrize("stream", [False, True])
def test_timeout_is_sanitized_without_replay(stream: bool) -> None:
    async def scenario() -> None:
        release = asyncio.Event()
        requests = 0

        async def handle(request: web.Request) -> web.StreamResponse:
            nonlocal requests
            requests += 1
            if stream:
                response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
                await response.prepare(request)
                await response.write(
                    sse(
                        [
                            {
                                "type": "response.output_text.delta",
                                "output_index": 0,
                                "content_index": 0,
                                "delta": "partial",
                            }
                        ]
                    )
                )
                await release.wait()
                return response
            await release.wait()
            return web.json_response(payload("responses"))

        async with (
            endpoint(handle) as url,
            # Allow connection establishment under instrumented CI load; the
            # handler deliberately blocks until cleanup to exercise the timeout.
            Provider(ProviderType.LITELLM, KEY, "fixture", base_url=url, api="responses", timeout=2) as provider,
        ):
            try:
                events = [
                    event async for event in provider.generate([Message(role="user", content="Hello")], stream=stream)
                ]
                assert isinstance(events[-1], ErrorEvent) and "timed out" in events[-1].message
                assert requests == 1 and not any(isinstance(event, ToolCallEvent) for event in events)
            finally:
                release.set()

    asyncio.run(scenario())


@pytest.mark.parametrize("api", APIS)
def test_nonstream_error_envelope_with_http_200(api: str) -> None:
    async def scenario() -> None:
        async def handle(request: web.Request) -> web.Response:
            return web.json_response({"error": {"message": LEAK, "code": KEY}})

        async with endpoint(handle) as url:
            async with Provider(ProviderType.LITELLM, KEY, "fixture", base_url=url, api=api) as provider:
                events = [
                    event async for event in provider.generate([Message(role="user", content="Hello")], stream=False)
                ]
            assert len(events) == 1 and isinstance(events[0], ErrorEvent)
            assert LEAK not in repr(events) and KEY not in repr(events)

    asyncio.run(scenario())


@pytest.mark.parametrize("api", ["chat_completions", "messages", "responses"])
def test_empty_response_still_reports_usage(api: str) -> None:
    async def scenario() -> None:
        async def handle(request: web.Request) -> web.StreamResponse:
            body = object_data(await request.json())
            return await reply(request, api, payload(api, ""), bool(body["stream"]))

        async with (
            endpoint(handle) as url,
            Provider(ProviderType.LITELLM, KEY, "fixture", base_url=url, api=api) as provider,
        ):
            for stream in (False, True):
                events = [
                    event async for event in provider.generate([Message(role="user", content="Hello")], stream=stream)
                ]
                assert isinstance(events[-1], TextDoneEvent) and events[-1].text == ""
                assert events[-1].usage.total_tokens == 16

    asyncio.run(scenario())


def test_responses_completed_items_with_empty_terminal_output() -> None:
    result = payload("responses", "Hello", (ToolCall("a", "work", {"value": "hello"}),))
    accumulator = ResponseAccumulator()
    chunks = frames("responses", result)
    result["output"] = []
    events: list[Event] = []
    for chunk in chunks:
        events.extend(accumulator.add(object_data(chunk)))
    assert accumulator.completed
    assert sum(isinstance(event, ToolCallEvent) for event in events) == 1
    done = next(event for event in events if isinstance(event, TextDoneEvent))
    assert done.text == "Hello" and done.usage.total_tokens == 16
