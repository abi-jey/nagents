"""Conversation headers reach generation HTTP without crossing task or catalog boundaries."""

from __future__ import annotations

import asyncio
import gc
import json
from typing import TYPE_CHECKING
from typing import cast
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web
from fastapi import HTTPException

from nagents import Agent
from nagents import SessionManager
from nagents._async import finish_on_cancel
from nagents.events import DoneEvent
from nagents.events import ErrorEvent
from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.harness.config import HarnessConfig
from nagents.harness.providers.auth import OpenAIAuth
from nagents.harness.providers.connection import build_provider
from nagents.harness.providers.factory import HarnessProvider
from nagents.harness.providers.registry import ProviderProfile
from nagents.harness.providers.registry import ProviderRegistry
from nagents.harness.providers.registry import ProviderRegistryStore
from nagents.harness.providers.registry import ScopedProviderRegistryStore
from nagents.provider import FoundryProvider
from nagents.provider import OpenAIProvider
from nagents.provider import Provider
from nagents.provider import ProviderType
from nagents.types import ToolCall
from nagents.web.http.app import _provider_profile
from tests.providers.test_gateway_provider import KEY
from tests.providers.test_gateway_provider import endpoint
from tests.providers.test_gateway_provider import frames
from tests.providers.test_gateway_provider import payload
from tests.providers.test_gateway_provider import reply
from tests.support.web import client_app

if TYPE_CHECKING:
    from pathlib import Path

HEADER = "x-opencode-session"


@pytest.mark.parametrize("api", ["chat_completions", "responses", "messages"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("provider_type", [ProviderType.LITELLM, ProviderType.OPENAI_COMPATIBLE])
def test_generation_header_is_optional_explicit_and_excludes_catalog(
    api: str, stream: bool, provider_type: ProviderType
) -> None:
    async def scenario() -> None:
        seen: list[tuple[str, str]] = []

        async def handle(request: web.Request) -> web.StreamResponse:
            seen.append((request.method, request.headers.get(HEADER, "")))
            if request.method == "GET":
                return web.json_response({"data": [{"id": "fixture"}]})
            return await reply(request, api, payload(api), stream)

        async with (
            endpoint(handle) as url,
            Provider(provider_type, KEY, "fixture", base_url=url, api=api) as provider,
        ):
            # Existing callers may set IDs for logging without enabling an HTTP header.
            provider.set_session_id("logging-only")
            assert isinstance([event async for event in provider.generate([], stream=stream)][-1], TextDoneEvent)
            provider.set_session_id(None)
            provider.session_id_header = HEADER
            assert isinstance([event async for event in provider.generate([], stream=stream)][-1], TextDoneEvent)
            provider.set_session_id("conversation:opaque-123")
            assert await provider.get_model_list() == ["fixture"]
            for _ in range(2):
                events = [event async for event in provider.generate([], stream=stream)]
                assert isinstance(events[-1], TextDoneEvent)
            provider.set_session_id("")
            assert isinstance([event async for event in provider.generate([], stream=stream)][-1], TextDoneEvent)
        assert seen == [
            ("POST", ""),
            ("POST", ""),
            ("GET", ""),
            ("POST", "conversation:opaque-123"),
            ("POST", "conversation:opaque-123"),
            ("POST", ""),
        ]

    asyncio.run(scenario())


@pytest.mark.parametrize("api", ["chat_completions", "responses"])
@pytest.mark.parametrize("stream", [False, True])
def test_shared_provider_interleaves_two_agents_and_keeps_tool_round_identity(
    tmp_path: Path, api: str, stream: bool
) -> None:
    async def scenario() -> None:
        first_arrived = asyncio.Event()
        second_arrived = asyncio.Event()
        seen: list[tuple[str, str]] = []
        counts: dict[str, int] = {}

        async def handle(request: web.Request) -> web.StreamResponse:
            if request.method == "GET":
                assert HEADER not in request.headers
                return web.json_response({"data": [{"id": "fixture", "max_input_tokens": 100000}]})
            body: object = await request.json()
            # The header must match the durable conversation's user input; no prompt-derived ID.
            text = json.dumps(body)
            expected = "chat-a" if "prompt-a" in text else "chat-b" if "prompt-b" in text else ""
            identity = request.headers.get(HEADER, "")
            seen.append((identity, expected))
            assert identity == expected
            counts[identity] = counts.get(identity, 0) + 1
            if expected and counts[identity] == 1:
                if expected == "chat-a":
                    first_arrived.set()
                    await asyncio.wait_for(second_arrived.wait(), 5)
                else:
                    second_arrived.set()
                result = payload(api, "Working", (ToolCall("call-1", "work", {}),))
            else:
                result = payload(api, "Finished")
            return await reply(request, api, result, stream)

        async def work() -> str:
            """A real local tool continuation."""
            await asyncio.sleep(0)
            return "tool result"

        async with endpoint(handle) as url:
            provider = Provider(ProviderType.LITELLM, KEY, "fixture", base_url=url, api=api, session_id_header=HEADER)
            agents = [
                Agent(
                    provider,
                    SessionManager(tmp_path / f"agent-{i}.db"),
                    tools=[work],
                    compaction_strategy=None,
                    streaming=stream,
                )
                for i in range(2)
            ]
            try:

                async def run(index: int, suffix: str) -> None:
                    events = [
                        event async for event in agents[index].run(f"prompt-{suffix}", session_id=f"chat-{suffix}")
                    ]
                    assert not any(isinstance(event, ErrorEvent) for event in events)
                    assert isinstance(events[-1], DoneEvent) and events[-1].final_text == "Finished"

                # The first conversation is paused at the actual HTTP server while the second starts.
                first = asyncio.create_task(run(0, "a"))
                await asyncio.wait_for(first_arrived.wait(), 5)
                await asyncio.gather(first, run(1, "b"))
                assert counts == {"chat-a": 2, "chat-b": 2}
                assert seen[0] == ("chat-a", "chat-a") and seen[1] == ("chat-b", "chat-b")
                # Resuming the persisted conversation keeps its ID across another Agent.run.
                await run(0, "a")
                assert counts["chat-a"] == 3
                # Agent.run restores its context, so a subsequent direct call has no chat header.
                assert isinstance([event async for event in provider.generate([], stream=stream)][-1], TextDoneEvent)
                assert seen[-1] == ("", "")
            finally:
                for agent in agents:
                    await agent.close()

    asyncio.run(scenario())


def test_public_setter_is_task_local_without_an_agent() -> None:
    async def scenario() -> None:
        ready = asyncio.Event()
        changed = asyncio.Event()
        seen: list[str] = []

        async def handle(request: web.Request) -> web.Response:
            seen.append(request.headers.get(HEADER, ""))
            return web.json_response(payload("chat_completions"))

        async with (
            endpoint(handle) as url,
            Provider(
                ProviderType.OPENAI_COMPATIBLE, KEY, "fixture", base_url=url, session_id_header=HEADER
            ) as provider,
        ):

            async def first() -> None:
                provider.set_session_id("first")
                ready.set()
                await changed.wait()
                assert isinstance([event async for event in provider.generate([], stream=False)][-1], TextDoneEvent)

            async def second() -> None:
                await ready.wait()
                provider.set_session_id("second")
                changed.set()
                assert isinstance([event async for event in provider.generate([], stream=False)][-1], TextDoneEvent)

            await asyncio.gather(first(), second())
            assert sorted(seen) == ["first", "second"]
            provider.set_session_id("outer")
            with pytest.raises(RuntimeError), provider.session_context("inner"):
                raise RuntimeError("cancelled work")
            assert isinstance([event async for event in provider.generate([], stream=False)][-1], TextDoneEvent)
            assert seen[-1] == "outer"

    asyncio.run(scenario())


@pytest.mark.parametrize("close", ["same_task", "other_task", "finish_on_cancel", "gc"])
def test_suspended_agent_stream_restores_caller_context_and_can_close_elsewhere(tmp_path: Path, close: str) -> None:
    async def scenario() -> None:
        seen: list[str] = []
        errors: list[dict[str, object]] = []
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()

        def loop_error(loop: asyncio.AbstractEventLoop, context: dict[str, object]) -> None:
            errors.append(context)

        loop.set_exception_handler(loop_error)

        async def handle(request: web.Request) -> web.StreamResponse:
            if request.method == "GET":
                return web.json_response({"data": [{"id": "fixture"}]})
            seen.append(request.headers.get(HEADER, ""))
            body = cast("dict[str, object]", await request.json())
            result = payload("chat_completions")
            if body.get("stream") is True:
                # This route intentionally uses the ordinary HTTPClient. Its
                # SSE parser expects a complete JSON object on each data line;
                # the gateway fixture's multi-line JSON tests a different parser.
                data = "".join(
                    f"data: {json.dumps(frame) if isinstance(frame, dict) else frame}\n\n"
                    for frame in frames("chat_completions", result)
                )
                return web.Response(text=data, content_type="text/event-stream")
            return web.json_response(result)

        async with endpoint(handle) as url:
            provider = Provider(ProviderType.OPENAI_COMPATIBLE, KEY, "fixture", base_url=url, session_id_header=HEADER)
            agent = Agent(
                provider, SessionManager(tmp_path / "early-close.db"), compaction_strategy=None, streaming=True
            )
            streams = [agent.run("Hello", session_id="chat-stream")]
            try:
                provider.set_session_id("caller-context")
                async for event in streams[0]:
                    assert not isinstance(event, ErrorEvent)
                    if isinstance(event, TextChunkEvent):
                        break
                else:
                    raise AssertionError("No streaming model response")
                assert seen == ["chat-stream"]
                # The caller resumes between events. Its independent request must
                # not inherit the suspended conversation, even before closing it.
                assert isinstance([event async for event in provider.generate([], stream=False)][-1], TextDoneEvent)
                assert seen[-1] == "caller-context"
                if close == "same_task":
                    await streams[0].aclose()
                elif close == "other_task":
                    await asyncio.create_task(streams[0].aclose())
                elif close == "finish_on_cancel":
                    await finish_on_cancel(streams[0].aclose())
                else:
                    streams.clear()
                    gc.collect()

                    async def finalized() -> None:
                        while agent._active_runs:
                            await asyncio.sleep(0)

                    await asyncio.wait_for(finalized(), 5)
                assert agent._active_runs == 0
                assert not errors
                provider.set_session_id(None)
                assert isinstance([event async for event in provider.generate([], stream=False)][-1], TextDoneEvent)
                assert seen[-1] == ""
            finally:
                for stream in streams:
                    await stream.aclose()
                await agent.close()
                loop.set_exception_handler(previous_handler)

    asyncio.run(scenario())


def test_cancelling_an_inflight_agent_request_restores_its_task_context(tmp_path: Path) -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        seen: list[str] = []

        async def handle(request: web.Request) -> web.Response:
            if request.method == "GET":
                return web.json_response({"data": [{"id": "fixture"}]})
            identity = request.headers.get(HEADER, "")
            seen.append(identity)
            if identity == "chat-cancel":
                entered.set()
                await release.wait()
            return web.json_response(payload("chat_completions"))

        async with endpoint(handle) as url:
            provider = Provider(ProviderType.OPENAI_COMPATIBLE, KEY, "fixture", base_url=url, session_id_header=HEADER)
            agent = Agent(provider, SessionManager(tmp_path / "cancel.db"), compaction_strategy=None, streaming=False)
            provider.set_session_id("caller-context")

            async def consume() -> None:
                try:
                    async for _ in agent.run("Hello", session_id="chat-cancel"):
                        pass
                except asyncio.CancelledError:
                    # Exercise an actual request in the cancelled consumer's
                    # own context after Agent's cancellation-safe cleanup.
                    assert isinstance([event async for event in provider.generate([], stream=False)][-1], TextDoneEvent)
                    raise

            pending = asyncio.create_task(consume())
            try:
                await asyncio.wait_for(entered.wait(), 5)
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
                assert seen == ["chat-cancel", "caller-context"]
                assert agent._active_runs == 0
            finally:
                release.set()
                if not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                await agent.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "name",
    [
        "Authorization",
        "aUtHoRiZaTiOn",
        "Cookie",
        "Host",
        "Content-Type",
        "Content-Length",
        "Transfer-Encoding",
        "Connection",
        "X-API-Key",
        "api-key",
        "Proxy-Authorization",
        "Sec-WebSocket-Key",
        "X-Forwarded-For",
        "bad:name",
        "bad name",
        "x-\r\ninject",
        "x-ümlaut",
        "x" * 129,
    ],
)
def test_reserved_or_invalid_names_are_rejected_in_public_config_and_web(name: str) -> None:
    with pytest.raises(ValueError, match="session_id_header"):
        Provider(ProviderType.OPENAI_COMPATIBLE, KEY, "fixture", session_id_header=name)
    with pytest.raises(ValueError, match="session_id_header"):
        ProviderProfile(kind="openai", session_id_header=name).validate()
    with pytest.raises(HTTPException) as error:
        _provider_profile({"kind": "openai", "session_id_header": name})
    assert error.value.status_code == 422


@pytest.mark.parametrize("value", ["bad\r\nAuthorization: injected", "\x00", "\x7f", "has space", "ümlaut", "a" * 1025])
def test_invalid_session_value_is_rejected_without_a_wire_request(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async with Provider(ProviderType.OPENAI_COMPATIBLE, KEY, "fixture", session_id_header=HEADER) as provider:
            request = AsyncMock(side_effect=AssertionError("No invalid header may reach HTTP"))
            monkeypatch.setattr(provider._http, "_get_session", request)
            with pytest.raises(ValueError, match="visible ASCII") as rejected:
                provider.set_session_id(value)
            assert value not in str(rejected.value)
            with pytest.raises(ValueError), provider.session_context(value):
                raise AssertionError("Invalid context was entered")
            # Recheck when a formerly logging-only ID becomes a configured wire value.
            provider.session_id_header = ""
            provider.set_session_id(value)
            provider.session_id_header = HEADER
            events = [event async for event in provider.generate([], stream=False)]
            assert isinstance(events[-1], ErrorEvent) and value not in repr(events)
            request.assert_not_called()

    asyncio.run(scenario())


def test_profile_roundtrip_defaults_validation_and_harness_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ProviderRegistryStore(tmp_path / "providers.json")
    original = ProviderProfile(kind="litellm", base_url="https://example.invalid/v1", session_id_header=HEADER)
    saved = store.save(ProviderRegistry(active="gateway", providers={"gateway": original}), expected="0" * 64)
    assert store.load() == saved
    profiles = cast("dict[str, dict[str, object]]", saved.snapshot()["providers"])
    assert profiles["gateway"]["session_id_header"] == HEADER
    assert _provider_profile({"kind": "openai"}).session_id_header == ""
    assert _provider_profile({"kind": "openai", "session_id_header": HEADER}).session_id_header == HEADER
    invalid_headers: list[object] = [None, 1, True, []]
    for value in invalid_headers:
        with pytest.raises(HTTPException):
            _provider_profile({"kind": "openai", "session_id_header": value})
    legacy = json.loads(store.path.read_text())
    del legacy["providers"]["gateway"]["session_id_header"]
    store.path.write_text(json.dumps(legacy))
    assert store.load().providers["gateway"].session_id_header == ""
    config = HarnessConfig(workspace=tmp_path, provider="gateway", providers={"gateway": original})
    assert HarnessProvider(config).session_id_header == HEADER
    monkeypatch.setattr(OpenAIAuth, "logged_in", lambda self: False)
    assert build_provider(original, config, OpenAIAuth(tmp_path / "login.json")).session_id_header == HEADER
    assert OpenAIProvider(api_key=KEY, model="fixture", session_id_header=HEADER).session_id_header == HEADER
    assert (
        FoundryProvider(
            api_key=KEY, model="fixture", base_url="https://example.invalid/v1", session_id_header=HEADER
        ).session_id_header
        == HEADER
    )


def test_web_scoped_connection_serializes_session_header_and_inherits(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (_, client, headers, _):
            before = (await client.get("/api/provider-scopes/global/providers", headers=headers)).json()
            saved = await client.put(
                "/api/provider-scopes/global/providers/gateway",
                headers=headers,
                json={
                    "revision": before["revision"],
                    "profile": {
                        "kind": "litellm",
                        "base_url": "https://example.invalid/v1",
                        "session_id_header": HEADER,
                    },
                },
            )
            assert saved.status_code == 200, saved.text
            assert saved.json()["providers"]["gateway"]["session_id_header"] == HEADER
            workspace = (await client.get("/api/provider-scopes/workspace/providers", headers=headers)).json()
            assert workspace["providers"]["gateway"]["session_id_header"] == HEADER
            assert workspace["origins"]["gateway"] == "global"
            scoped = ScopedProviderRegistryStore(tmp_path)
            assert scoped.load().providers["gateway"].session_id_header == HEADER
            assert not scoped.workspace_store.path.exists()

    asyncio.run(scenario())


def test_bearer_credential_requests_do_not_carry_conversation_header() -> None:
    async def scenario() -> None:
        seen: list[tuple[str, str]] = []

        async def handle(request: web.Request) -> web.Response:
            seen.append((request.path, request.headers.get(HEADER, "")))
            if request.path == "/oauth/token":
                return web.json_response({"access_token": KEY})
            assert request.headers["Authorization"] == f"Bearer {KEY}"
            return web.json_response(payload("responses"))

        async with endpoint(handle) as url:

            async def credentials() -> str:
                async with aiohttp.ClientSession() as client, client.post(url + "/oauth/token") as response:
                    value = cast("dict[str, str]", await response.json())
                    return value["access_token"]

            async with Provider(
                ProviderType.OPENAI_COMPATIBLE,
                model="fixture",
                base_url=url,
                api="responses",
                bearer_token_provider=credentials,
                session_id_header=HEADER,
            ) as provider:
                provider.set_session_id("chat-credential-test")
                events = [event async for event in provider.generate([], stream=False)]
                assert isinstance(events[-1], TextDoneEvent)
        assert seen == [("/oauth/token", ""), ("/responses", "chat-credential-test")]

    asyncio.run(scenario())
