"""Codex Responses transport tests with fake OAuth credentials and local SSE."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import pytest
from aiohttp import web

from nagents import Agent
from nagents import SessionManager
from nagents.events import DoneEvent
from nagents.events import ErrorEvent
from nagents.events import ReasoningChunkEvent
from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.events import ToolResultEvent
from nagents.provider import codex
from nagents.provider.codex import DEFAULT_CODEX_MODEL
from nagents.provider.codex import CodexCredentials
from nagents.provider.codex import CodexProvider
from nagents.types import AudioContent
from nagents.types import DocumentContent
from nagents.types import GenerationConfig
from nagents.types import ImageContent
from nagents.types import Message
from nagents.types import TextContent
from nagents.types import ToolCall
from nagents.types import ToolDefinition

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from collections.abc import Awaitable
    from collections.abc import Callable
    from pathlib import Path

    from nagents.types import ContentPart


ACCESS = "fake-codex-access-secret"


async def credentials() -> CodexCredentials:
    return CodexCredentials(ACCESS, "account-123", "eu")


def call_item(arguments: str = '{"value":"hello"}') -> dict[str, object]:
    return {
        "id": "item-a",
        "type": "function_call",
        "call_id": "call-a",
        "name": "work",
        "arguments": arguments,
        "status": "completed",
    }


def completion(output: list[dict[str, object]]) -> dict[str, object]:
    return {
        "type": "response.completed",
        "response": {
            "status": "completed",
            "output": output,
            "usage": {
                "input_tokens": 20,
                "output_tokens": 10,
                "total_tokens": 30,
                "input_tokens_details": {"cached_tokens": 5},
                "output_tokens_details": {"reasoning_tokens": 3},
            },
        },
    }


def text_item(text: str = "Hello") -> dict[str, object]:
    return {
        "id": "message-a",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text}],
    }


def sse(events: list[dict[str, object]]) -> str:
    return ": heartbeat\r\n\r\n" + "".join(
        f"event: {event['type']}\r\ndata: {json.dumps(event)}\r\n\r\n" for event in events
    )


@asynccontextmanager
async def endpoint(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    *,
    omit_content_type: bool = False,
) -> AsyncIterator[str]:
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handler)
    if omit_content_type:

        async def prepare(request: web.Request, response: web.StreamResponse) -> None:
            response.headers.pop("Content-Type", None)

        app.on_response_prepare.append(prepare)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{runner.addresses[0][1]}/backend-api/codex/responses"
    monkeypatch.setattr(codex, "CODEX_ENDPOINT", url)
    try:
        yield url
    finally:
        await runner.cleanup()


@pytest.fixture(autouse=True)
def isolate_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


@pytest.mark.parametrize("streaming", [False, True])
def test_responses_conversion_headers_and_text_usage(monkeypatch: pytest.MonkeyPatch, streaming: bool) -> None:
    async def scenario() -> None:
        requests: list[dict[str, object]] = []

        async def handle(request: web.Request) -> web.Response:
            assert request.path == "/backend-api/codex/responses" and request.method == "POST"
            assert request.headers["Authorization"] == f"Bearer {ACCESS}"
            assert request.headers["ChatGPT-Account-Id"] == "account-123"
            assert request.headers["x-openai-internal-codex-residency"] == "eu"
            assert request.headers["originator"] == "ngn"
            assert request.headers["User-Agent"].startswith("ngn/")
            body = await request.json()
            requests.append(body)
            return web.Response(
                text=sse(
                    [
                        {"type": "response.reasoning_summary_text.delta", "delta": "Brief reasoning"},
                        {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "Hel"},
                        {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "lo"},
                        {"type": "response.output_text.done", "output_index": 0, "content_index": 0, "text": "Hello"},
                        completion([text_item()]),
                    ]
                ),
                content_type="text/event-stream",
            )

        messages = [
            Message(role="system", content="System instructions"),
            Message(role="developer", content=[TextContent("Developer instructions")]),
            Message(role="compaction_summary", content="Earlier facts"),
            Message(role="user", content=[TextContent("See image"), ImageContent("aW1hZ2U=", "image/png")]),
            Message(
                role="assistant", content="Using a tool", tool_calls=[ToolCall("previous", "work", {"value": "old"})]
            ),
            Message(role="tool", tool_call_id="previous", name="work", content="old result"),
        ]
        tool = ToolDefinition("work", "Return value", {"type": "object", "properties": {"value": {"type": "string"}}})
        async with endpoint(monkeypatch, handle):
            provider = CodexProvider(credentials)
            provider.base_url = "http://127.0.0.1:1/never-send-oauth-here"
            assert provider.model == DEFAULT_CODEX_MODEL
            assert await provider.verify_model() is True and requests == []
            events = [
                event
                async for event in provider.generate(
                    messages,
                    [tool],
                    GenerationConfig(temperature=0.5, max_tokens=50, top_p=0.9, reasoning={"enabled": True}),
                    stream=streaming,
                )
            ]
            assert len(requests) == 1
            body = requests[0]
            assert body["store"] is False and body["stream"] is True
            assert body["instructions"] == "System instructions\n\nDeveloper instructions"
            assert not {"temperature", "max_tokens", "max_output_tokens", "top_p"} & body.keys()
            assert body["reasoning"] == {"summary": "auto"}
            assert body["tools"] == [
                {
                    "type": "function",
                    "name": "work",
                    "description": "Return value",
                    "parameters": tool.parameters,
                    "strict": False,
                }
            ]
            inputs = body["input"]
            assert isinstance(inputs, list)
            assert "Earlier facts" in json.dumps(inputs[0])
            assert inputs[1]["content"][1] == {
                "type": "input_image",
                "image_url": "data:image/png;base64,aW1hZ2U=",
                "detail": "auto",
            }
            assert inputs[2]["content"] == [{"type": "output_text", "text": "Using a tool"}]
            assert inputs[3] == {
                "type": "function_call",
                "call_id": "previous",
                "name": "work",
                "arguments": '{"value": "old"}',
            }
            assert inputs[4] == {"type": "function_call_output", "call_id": "previous", "output": "old result"}
            assert not any(isinstance(event, ErrorEvent) for event in events)
            done = next(event for event in events if isinstance(event, TextDoneEvent))
            assert done.text == "Hello"
            assert (done.usage.prompt_tokens, done.usage.completion_tokens, done.usage.total_tokens) == (20, 10, 30)
            assert done.usage.cached_tokens == 5 and done.usage.reasoning_tokens == 3
            assert [event.chunk for event in events if isinstance(event, TextChunkEvent)] == (
                ["Hel", "lo"] if streaming else []
            )
            assert [event.chunk for event in events if isinstance(event, ReasoningChunkEvent)] == (
                ["Brief reasoning"] if streaming else []
            )
            assert ACCESS not in repr(events) and ACCESS not in json.dumps(body)
            await provider.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("valid_stream", [False, True])
def test_missing_content_type_still_requires_valid_sse(monkeypatch: pytest.MonkeyPatch, valid_stream: bool) -> None:
    async def scenario() -> None:
        async def handle(request: web.Request) -> web.Response:
            body = sse([completion([text_item()])]) if valid_stream else '{"message":"not an event stream"}'
            return web.Response(text=body)

        async with endpoint(monkeypatch, handle, omit_content_type=True):
            provider = CodexProvider(credentials)
            try:
                events = [event async for event in provider.generate([Message(role="user", content="hello")])]
                assert len(events) == 1
                assert isinstance(events[0], TextDoneEvent if valid_stream else ErrorEvent)
                assert not any(isinstance(event, ToolCallEvent) for event in events)
            finally:
                await provider.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("terminal_only", [False, True])
def test_fragmented_tool_arguments_are_deduplicated(monkeypatch: pytest.MonkeyPatch, terminal_only: bool) -> None:
    async def scenario() -> None:
        item = call_item()
        events = (
            []
            if terminal_only
            else [
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {**item, "arguments": "", "status": "in_progress"},
                },
                {
                    "type": "response.function_call_arguments.delta",
                    "output_index": 0,
                    "item_id": "item-a",
                    "delta": '{"value":',
                },
                {
                    "type": "response.function_call_arguments.delta",
                    "output_index": 0,
                    "item_id": "item-a",
                    "delta": '"hello"}',
                },
                {"type": "response.function_call_arguments.done", "output_index": 0, "arguments": '{"value":"hello"}'},
                {"type": "response.output_item.done", "output_index": 0, "item": item},
            ]
        )
        events.append(completion([item]))

        async def handle(request: web.Request) -> web.StreamResponse:
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            data = sse(events).encode()
            for start in range(0, len(data), 7):
                await response.write(data[start : start + 7])
            await response.write_eof()
            return response

        async with endpoint(monkeypatch, handle):
            provider = CodexProvider(credentials)
            results = [event async for event in provider.generate([Message(role="user", content="work")])]
            calls = [event for event in results if isinstance(event, ToolCallEvent)]
            assert len(calls) == 1
            assert calls[0].id == "call-a" and calls[0].name == "work"
            assert calls[0].arguments == {"value": "hello"} and calls[0].usage.total_tokens == 30
            assert not any(isinstance(event, ErrorEvent) for event in results)
            await provider.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "failure",
    [
        "incomplete",
        "failed",
        "error",
        "eof",
        "done_marker",
        "json",
        "unterminated",
        "arguments",
        "unfinished",
        "usage",
        "identity",
    ],
)
def test_failed_or_malformed_streams_never_release_tools(monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    async def scenario() -> None:
        item = call_item()
        beginning = sse([{"type": "response.output_item.done", "output_index": 0, "item": item}])
        endings = {
            "incomplete": sse([{"type": "response.incomplete", "response": {"error": ACCESS}}]),
            "failed": sse([{"type": "response.failed", "response": {"error": ACCESS}}]),
            "error": sse([{"type": "error", "message": ACCESS}]),
            "eof": "",
            "done_marker": "data: [DONE]\n\n",
            "json": "data: " + ACCESS + "\n\n",
            "unterminated": "data: " + json.dumps(completion([item])),
            "arguments": sse([completion([call_item(ACCESS)])]),
            "unfinished": sse([completion([{**item, "status": "in_progress"}])]),
            "usage": sse(
                [{"type": "response.completed", "response": {"output": [item], "usage": {"input_tokens": ACCESS}}}]
            ),
            "identity": sse([completion([{**item, "call_id": "different-id"}])]),
        }

        async def handle(request: web.Request) -> web.Response:
            return web.Response(text=beginning + endings[failure], content_type="text/event-stream")

        async with endpoint(monkeypatch, handle):
            provider = CodexProvider(credentials)
            events = [event async for event in provider.generate([Message(role="user", content="work")])]
            assert len(events) == 1 and isinstance(events[0], ErrorEvent)
            assert ACCESS not in repr(events)
            await provider.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [401, 403, 429, 500, 307])
def test_http_errors_safe_and_redirects_disabled(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    async def scenario() -> None:
        paths: list[str] = []

        async def handle(request: web.Request) -> web.Response:
            paths.append(request.path)
            return web.Response(status=status, text=ACCESS, headers={"Location": codex.CODEX_ENDPOINT + "/leak"})

        async with endpoint(monkeypatch, handle):
            provider = CodexProvider(credentials)
            events = [event async for event in provider.generate([Message(role="user", content="hello")])]
            assert len(events) == 1 and isinstance(events[0], ErrorEvent)
            assert events[0].code == f"CODEX_HTTP_{status}" and not events[0].recoverable
            assert ACCESS not in repr(events)
            assert paths == ["/backend-api/codex/responses"]
            await provider.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["orphan", "partial", "wrong_item", "nonfinite", "nonobject", "duplicate"])
def test_invalid_tool_state_is_rejected_before_any_call(monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    async def scenario() -> None:
        item = call_item()
        frames: list[dict[str, object]] = []
        output = [item]
        if failure == "orphan":
            frames.append({"type": "response.function_call_arguments.delta", "output_index": 1, "delta": "{}"})
        elif failure == "partial":
            frames.append({"type": "response.output_item.added", "output_index": 0, "item": item})
            output = []
        elif failure == "wrong_item":
            frames.extend(
                [
                    {"type": "response.output_item.added", "output_index": 0, "item": item},
                    {
                        "type": "response.function_call_arguments.delta",
                        "output_index": 0,
                        "item_id": "different",
                        "delta": "{}",
                    },
                ]
            )
        elif failure == "nonfinite":
            output = [call_item('{"value":NaN}')]
        elif failure == "nonobject":
            output = [call_item("[]")]
        else:
            output = [item, {**item, "id": "different-item"}]
        frames.append(completion(output))

        async def handle(request: web.Request) -> web.Response:
            return web.Response(text=sse(frames), content_type="text/event-stream")

        async with endpoint(monkeypatch, handle):
            provider = CodexProvider(credentials)
            events = [event async for event in provider.generate([Message(role="user", content="work")])]
            assert len(events) == 1 and isinstance(events[0], ErrorEvent)
            await provider.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("tool_call", [False, True])
def test_completed_stream_items_with_empty_terminal_output(monkeypatch: pytest.MonkeyPatch, tool_call: bool) -> None:
    async def scenario() -> None:
        item = call_item() if tool_call else text_item()

        async def handle(request: web.Request) -> web.Response:
            return web.Response(
                text=sse(
                    [
                        {
                            "type": "response.output_item.added",
                            "output_index": 0,
                            "item": {**item, "status": "in_progress"},
                        },
                        {"type": "response.output_item.done", "output_index": 0, "item": item},
                        completion([]),
                    ]
                )
            )

        async with endpoint(monkeypatch, handle, omit_content_type=True):
            provider = CodexProvider(credentials)
            try:
                events = [event async for event in provider.generate([Message(role="user", content="hello")])]
                assert not any(isinstance(event, ErrorEvent) for event in events)
                assert sum(isinstance(event, ToolCallEvent) for event in events) == int(tool_call)
                done = next(event for event in events if isinstance(event, TextDoneEvent))
                assert done.text == ("" if tool_call else "Hello")
            finally:
                await provider.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("part", [AudioContent("fake"), DocumentContent("fake"), ImageContent("fake", "image/tiff")])
def test_unsupported_media_rejected_before_credentials(part: ContentPart) -> None:
    async def scenario() -> None:
        async def forbidden() -> CodexCredentials:
            raise AssertionError("Credentials must not be requested for unsupported media")

        provider = CodexProvider(forbidden)
        events = [event async for event in provider.generate([Message(role="user", content=[part])])]
        assert len(events) == 1 and isinstance(events[0], ErrorEvent)
        assert events[0].code == "CODEX_REQUEST_INVALID"
        await provider.close()

    asyncio.run(scenario())


def test_credentials_exception_is_not_exposed() -> None:
    async def scenario() -> None:
        async def broken() -> CodexCredentials:
            raise RuntimeError(ACCESS)

        provider = CodexProvider(broken)
        events = [event async for event in provider.generate([Message(role="user", content="hello")])]
        assert len(events) == 1 and isinstance(events[0], ErrorEvent)
        assert "/login" in events[0].message and ACCESS not in repr(events)
        await provider.close()

    asyncio.run(scenario())


def test_missing_optional_headers_and_no_constraint(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async def basic() -> CodexCredentials:
            return CodexCredentials(ACCESS, residency="no_constraint")

        async def handle(request: web.Request) -> web.Response:
            assert "ChatGPT-Account-Id" not in request.headers
            assert "x-openai-internal-codex-residency" not in request.headers
            body = await request.json()
            assert body["instructions"] == ""
            return web.Response(text=sse([completion([text_item()])]), content_type="text/event-stream")

        async with endpoint(monkeypatch, handle):
            provider = CodexProvider(basic)
            events = [event async for event in provider.generate([Message(role="user", content="hello")])]
            assert isinstance(events[-1], TextDoneEvent)
            await provider.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel", [False, True])
def test_interruption_closes_stream(monkeypatch: pytest.MonkeyPatch, cancel: bool) -> None:
    async def scenario() -> None:
        release = asyncio.Event()
        disconnected = asyncio.Event()

        async def handle(request: web.Request) -> web.StreamResponse:
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(sse([{"type": "response.output_text.delta", "delta": "partial"}]).encode())
            try:
                while not release.is_set():
                    if request.transport is None or request.transport.is_closing():
                        disconnected.set()
                        break
                    await asyncio.sleep(0.01)
            finally:
                release.set()
            return response

        async with endpoint(monkeypatch, handle):
            provider = CodexProvider(credentials)
            stream = provider.generate([Message(role="user", content="hello")])
            first = await anext(stream)
            assert isinstance(first, TextChunkEvent)
            try:
                if cancel:
                    task = asyncio.create_task(anext(stream))
                    await asyncio.sleep(0)
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    await stream.aclose()
                await asyncio.wait_for(disconnected.wait(), 1)
            finally:
                release.set()
                await stream.aclose()
                await provider.close()

    asyncio.run(scenario())


def test_timeout_and_stream_size_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async def handle(request: web.Request) -> web.Response:
            return web.Response(text="data: " + "x" * 256, content_type="text/event-stream")

        monkeypatch.setattr(codex, "_MAX_EVENT_BYTES", 128)
        async with endpoint(monkeypatch, handle):
            provider = CodexProvider(credentials)
            events = [event async for event in provider.generate([Message(role="user", content="hello")])]
            assert len(events) == 1 and isinstance(events[0], ErrorEvent)
            assert "size limit" in events[0].message
            await provider.close()

        release = asyncio.Event()

        async def wait(request: web.Request) -> web.Response:
            await release.wait()
            return web.Response(text="late")

        async with endpoint(monkeypatch, wait):
            provider = CodexProvider(credentials, timeout=0.03)
            try:
                events = [event async for event in provider.generate([Message(role="user", content="hello")])]
                assert len(events) == 1 and isinstance(events[0], ErrorEvent)
                assert "timed out" in events[0].message
            finally:
                release.set()
                await provider.close()

    asyncio.run(scenario())


def test_agent_tool_loop_and_history_never_store_oauth(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def scenario() -> None:
        requests = 0

        async def handle(request: web.Request) -> web.Response:
            nonlocal requests
            requests += 1
            body = await request.json()
            if requests == 1:
                output = [text_item("Calling work"), call_item()]
            else:
                assert any(
                    item.get("type") == "function_call_output" and item["output"] == "HELLO" for item in body["input"]
                )
                output = [text_item("Finished")]
            return web.Response(text=sse([completion(output)]), content_type="text/event-stream")

        async def work(value: str) -> str:
            """Uppercase the local value."""
            return value.upper()

        async with endpoint(monkeypatch, handle):
            agent = Agent(
                CodexProvider(credentials), SessionManager(tmp_path / "sessions.db"), tools=[work], compactor=None
            )
            events = [event async for event in agent.run("Use work", session_id="s")]
            assert requests == 2
            assert any(isinstance(event, ToolResultEvent) and event.result == "HELLO" for event in events)
            assert isinstance(events[-1], DoneEvent) and events[-1].final_text == "Finished"
            history = await agent.session.get_history("s")
            assert ACCESS not in repr(history) and ACCESS not in repr(events)
            assert history[1].content == "Calling work" and len(history[1].tool_calls) == 1
            assert ACCESS.encode() not in (tmp_path / "sessions.db").read_bytes()
            await agent.close()

    asyncio.run(scenario())
