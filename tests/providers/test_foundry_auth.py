"""Offline Foundry auth boundaries, token lifecycle, and real HTTP/WS transports."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Literal

import pytest
from aiohttp import web

from nagents import Agent
from nagents import FoundryProvider
from nagents import OpenAIProvider
from nagents import Provider
from nagents import ProviderType
from nagents import SessionManager
from nagents.audio import AudioDuplex
from nagents.audio import NullAudioOutput
from nagents.events import ErrorEvent
from nagents.events import TextDoneEvent
from nagents.live import LiveAPI
from nagents.live import LiveConfig
from nagents.live import _LiveUpdates
from nagents.live.runtime import converse
from nagents.types import Message
from nagents.types import RetryConfig
from tests.providers.test_live import BlockingMic
from tests.support.hang_guard import HANG_GUARD

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from collections.abc import Awaitable
    from collections.abc import Callable
    from pathlib import Path

    from nagents.provider import AsyncTokenCredential
    from nagents.provider import TokenCredential


@asynccontextmanager
async def server(handler: Callable[[web.Request], Awaitable[web.StreamResponse]]) -> AsyncIterator[str]:
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handler)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    try:
        yield f"http://127.0.0.1:{runner.addresses[0][1]}/openai/v1"
    finally:
        await runner.cleanup()


@dataclass(frozen=True)
class Token:
    token: str


class Tokens:
    def __init__(self) -> None:
        self.calls = 0
        self.closed = False
        self.scopes: list[tuple[str, ...]] = []

    async def get_token(self, *scopes: str) -> Token:
        self.scopes.append(scopes)
        return Token(await self())

    async def close(self) -> None:
        self.closed = True

    async def __call__(self) -> str:
        assert not self.closed
        self.calls += 1
        return f"fake-token-{self.calls}"


@pytest.mark.parametrize("api", ["chat_completions", "responses"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("foundry", [False, True])
def test_fresh_auth_on_retry_and_catalog(api: str, stream: bool, foundry: bool) -> None:
    async def scenario() -> None:
        tokens = Tokens()
        seen: list[str] = []

        async def handle(request: web.Request) -> web.Response:
            seen.append(request.headers["Authorization"])
            assert "api-key" not in request.headers
            if request.path.endswith("/models"):
                return web.json_response({"data": [{"id": "deployment"}]})
            body = await request.json()
            assert body["model"] == "deployment"
            if len(seen) == 1:
                return web.Response(status=503)
            if api == "responses":
                assert request.path == "/openai/v1/responses"
                result = {
                    "status": "completed",
                    "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
                }
                if stream:
                    return web.Response(
                        text="data: " + json.dumps({"type": "response.completed", "response": result}) + "\n\n",
                        content_type="text/event-stream",
                    )
                return web.json_response(result)
            assert request.path == "/openai/v1/chat/completions"
            if stream:
                return web.Response(
                    text='data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n',
                    content_type="text/event-stream",
                )
            return web.json_response({"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})

        async with server(handle) as prefix:
            provider: Provider = (
                FoundryProvider(
                    base_url=prefix,
                    model="deployment",
                    api=api,
                    credential=tokens,
                    scope="custom-audience/.default",
                    retry_config=RetryConfig(base_delay=0, max_retries=1),
                )
                if foundry
                else OpenAIProvider(
                    base_url=prefix,
                    model="deployment",
                    api=api,
                    bearer_token_provider=tokens,
                    retry_config=RetryConfig(base_delay=0, max_retries=1),
                )
            )
            async with provider:
                events = [
                    event async for event in provider.generate([Message(role="user", content="hi")], stream=stream)
                ]
                assert any(isinstance(event, TextDoneEvent) and event.text == "ok" for event in events)
                assert not any(isinstance(event, ErrorEvent) for event in events)
                if not foundry:
                    assert await provider.get_model_list() == ["deployment"]
                assert provider.api_key == ""
            assert not tokens.closed
            expected = 2 if foundry else 3
            assert tokens.calls == expected
            assert seen == [f"Bearer fake-token-{i}" for i in range(1, expected + 1)]
            if foundry:
                assert tokens.scopes == [("custom-audience/.default",)] * 2
            await tokens.close()
            assert tokens.closed

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "url",
    [
        "https://user:pass@example.test/v1",
        "https://example.test/v1?key=x",
        "http://example.test/v1",
        "https://example.test:bad/v1",
        "https://example.test/v1/live/sessions",
    ],
)
def test_invalid_endpoint_before_auth(url: str) -> None:
    tokens = Tokens()
    with pytest.raises(ValueError):
        OpenAIProvider(base_url=url, bearer_token_provider=tokens, live_config=LiveConfig())
    with pytest.raises(ValueError):
        FoundryProvider(base_url=url, model="m", credential=tokens)
    assert tokens.calls == 0


def test_auth_selection_and_unsupported_paths(tmp_path: Path) -> None:
    tokens = Tokens()
    with pytest.raises(ValueError, match="auth sources"):
        OpenAIProvider(api_key="key", bearer_token_provider=tokens)
    with pytest.raises(ValueError, match="auth sources"):
        OpenAIProvider(credentials=lambda: None, bearer_token_provider=tokens)  # type: ignore[arg-type,return-value]
    with pytest.raises(ValueError, match="not both"):
        Provider(ProviderType.OPENAI_COMPATIBLE, "key", "m", bearer_token_provider=tokens)
    for kind in (ProviderType.ANTHROPIC, ProviderType.GEMINI_NATIVE, ProviderType.AZURE_OPENAI_COMPATIBLE):
        with pytest.raises(ValueError, match="v1 provider"):
            Provider(kind, model="m", bearer_token_provider=tokens)
    with pytest.raises(ValueError, match="text chat/responses"):
        OpenAIProvider(api="messages", bearer_token_provider=tokens)
    from nagents.realtime import RealtimeConfig

    with pytest.raises(ValueError, match="not Realtime"):
        OpenAIProvider(bearer_token_provider=tokens, realtime_config=RealtimeConfig())
    provider = OpenAIProvider(home=tmp_path / "missing", bearer_token_provider=tokens)
    assert not provider.uses_chatgpt_auth and provider.api_key == ""
    agent = Agent(provider=provider, session_manager=SessionManager(tmp_path / "realtime.db"))
    with pytest.raises(ValueError, match="Realtime does not support"):
        agent.realtime_session()
    with pytest.raises(ValueError, match="Batch mode does not support"):
        Agent(provider=provider, session_manager=SessionManager(tmp_path / "s.db"), batch=True)
    assert tokens.calls == 0


@pytest.mark.parametrize("auth", ["key", "sync", "async", "openai-key"])
def test_live_rest_ws_fresh_auth_and_routing(auth: str) -> None:
    async def scenario() -> None:
        tokens = Tokens()
        seen: list[tuple[str, dict[str, str]]] = []
        key_auth = auth in {"key", "openai-key"}

        class SyncCredential:
            def get_token(self, *scopes: str) -> Token:
                assert not tokens.closed
                tokens.calls += 1
                tokens.scopes.append(scopes)
                return Token(f"fake-token-{tokens.calls}")

        async def handle(request: web.Request) -> web.StreamResponse:
            assert not request.query_string
            seen.append(
                (
                    request.path,
                    {name: request.headers[name] for name in ("Authorization", "api-key") if name in request.headers},
                )
            )
            if request.headers.get("Upgrade") == "websocket":
                ws = web.WebSocketResponse()
                await ws.prepare(request)
                if not request.path.endswith("/attach"):
                    event = await ws.receive_json()
                    assert event["type"] == "session.start"
                    await ws.send_json({"type": "session.started", "session": {"id": "s"}})
                await ws.send_json({"type": "session.closed", "usage": {}, "reason": "completed"})
                await ws.close()
                return ws
            if request.path.endswith("/content"):
                return web.Response(body=b"audio")
            return web.json_response({"session": {"id": "s"}, "transport": {"sdp": "answer"}})

        async def discard(event: dict[str, object]) -> None:
            pass

        async def backend(context: str, identifier: str) -> str:
            return "ok"

        async with server(handle) as prefix:
            provider: Provider = (
                OpenAIProvider(
                    base_url=prefix,
                    model="live-deployment",
                    api_key="key",
                    live_config=LiveConfig(),
                )
                if auth == "openai-key"
                else FoundryProvider(
                    base_url=prefix,
                    model="live-deployment",
                    api_key="key" if key_auth else "",
                    credential=None if key_auth else SyncCredential() if auth == "sync" else tokens,
                    live_config=LiveConfig(),
                )
            )
            async with provider:
                api = LiveAPI(provider)
                assert (await api.create_webrtc("offer", {}))["transport"] == {"sdp": "answer"}
                assert b"".join([chunk async for chunk in api.recording("s")]) == b"audio"
                for options in (LiveConfig(), LiveConfig(attach_to="s"), LiveConfig(fork_from="s")):
                    await asyncio.wait_for(
                        converse(
                            options.session("live-deployment", "brief"),
                            "",
                            backend,
                            AudioDuplex(input=BlockingMic([])),
                            NullAudioOutput(),
                            discard,
                            _LiveUpdates(),
                            options,
                            provider=provider,
                        ),
                        HANG_GUARD,
                    )
            assert not tokens.closed
            if not key_auth:
                assert tokens.scopes == [("https://ai.azure.com/.default",)] * 5
        assert [path for path, _ in seen] == [
            "/openai/v1/live/sessions",
            "/openai/v1/live/sessions/s/content",
            "/openai/v1/live/sessions",
            "/openai/v1/live/sessions/s/attach",
            "/openai/v1/live/sessions/s/fork",
        ]
        expected = (
            [{"Authorization": "Bearer key"}] * 2 + [{"api-key": "key"}] * 3
            if auth == "key"
            else [{"Authorization": "Bearer key"}] * 5
            if auth == "openai-key"
            else [{"Authorization": f"Bearer fake-token-{i}"} for i in range(1, 6)]
        )
        assert [headers for _, headers in seen] == expected

    asyncio.run(scenario())


@pytest.mark.parametrize("transport", ["text", "rest", "ws"])
@pytest.mark.parametrize("foundry_key", [False, True])
def test_redirects_never_forward_credentials(transport: str, foundry_key: bool) -> None:
    async def scenario() -> None:
        paths: list[str] = []
        tokens = Tokens()

        async def handle(request: web.Request) -> web.Response:
            paths.append(request.path)
            assert not request.query_string
            if foundry_key and transport == "ws":
                assert request.headers["api-key"] == "key" and "Authorization" not in request.headers
            else:
                assert request.headers["Authorization"] == ("Bearer key" if foundry_key else "Bearer fake-token-1")
                assert "api-key" not in request.headers
            return web.Response(status=307, headers={"Location": "/unexpected"})

        async def discard(event: dict[str, object]) -> None:
            pass

        async def backend(context: str, identifier: str) -> str:
            return ""

        async with (
            server(handle) as prefix,
            (
                FoundryProvider(base_url=prefix, model="m", api_key="key", live_config=LiveConfig())
                if foundry_key
                else OpenAIProvider(base_url=prefix, bearer_token_provider=tokens, live_config=LiveConfig())
            ) as provider,
        ):
            if transport == "text":
                events = [e async for e in provider.generate([Message(role="user", content="hi")], stream=False)]
                assert isinstance(events[-1], ErrorEvent)
            elif transport == "rest":
                with pytest.raises(RuntimeError, match="307"):
                    await LiveAPI(provider).create_webrtc("offer", {})
            else:
                with pytest.raises(ValueError, match="redirects"):
                    await converse(
                        {},
                        "",
                        backend,
                        AudioDuplex(),
                        NullAudioOutput(),
                        discard,
                        _LiveUpdates(),
                        provider=provider,
                    )
        assert len(paths) == 1 and paths[0] != "/unexpected"
        assert tokens.calls == (0 if foundry_key else 1)

    asyncio.run(scenario())


def test_cancellation_and_failed_callback_do_not_leak_or_close_credentials() -> None:
    async def scenario() -> None:
        entered = asyncio.Event()

        async def pending() -> str:
            entered.set()
            await asyncio.Event().wait()
            return "never"

        async with OpenAIProvider(bearer_token_provider=pending) as provider:
            task = asyncio.create_task(provider.auth_headers(provider.base_url))
            await asyncio.wait_for(entered.wait(), HANG_GUARD)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert provider.api_key == ""

        async def fail() -> str:
            raise RuntimeError("secret-token")

        async with OpenAIProvider(bearer_token_provider=fail) as provider:
            with pytest.raises(ValueError, match="Bearer token provider failed") as error:
                await provider.auth_headers(provider.base_url)
            assert "secret-token" not in str(error.value)

    asyncio.run(scenario())


@pytest.mark.parametrize("key_auth", [False, True])
@pytest.mark.parametrize("query", ["api-key=secret", "Authorization=Bearer%20secret"])
def test_foundry_websocket_query_credentials_rejected_before_acquisition(key_auth: bool, query: str) -> None:
    async def scenario() -> None:
        tokens = Tokens()
        async with FoundryProvider(
            base_url="https://resource.openai.azure.com/openai/v1",
            model="m",
            api_key="key" if key_auth else "",
            credential=None if key_auth else tokens,
        ) as provider:
            with pytest.raises(ValueError, match="Invalid authentication endpoint"):
                await provider.auth_headers(provider.live_endpoint(websocket=True) + "?" + query)
        assert tokens.calls == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("token", ["", "bad\r\nheader", "two words", "non-ascii-\u00e9"])
def test_invalid_token_rejected(token: str) -> None:
    async def callback() -> str:
        return token

    async def scenario() -> None:
        async with OpenAIProvider(bearer_token_provider=callback) as provider:
            with pytest.raises(ValueError, match="invalid token"):
                await provider.auth_headers(provider.base_url)

    asyncio.run(scenario())


def test_azure_v1_prefix_and_mutated_endpoint_validation() -> None:
    tokens = Tokens()
    provider = Provider(
        ProviderType.AZURE_OPENAI_COMPATIBLE_V1,
        model="deployment",
        base_url="https://resource.openai.azure.com/openai",
        bearer_token_provider=tokens,
        live_config=LiveConfig(),
        api="responses",
    )
    assert provider.live_endpoint() == "https://resource.openai.azure.com/openai/v1/live/sessions"
    assert provider.live_endpoint(websocket=True) == "wss://resource.openai.azure.com/openai/v1/live/sessions"
    provider.base_url = "https://resource.openai.azure.com/openai/v1?secret=x"
    with pytest.raises(ValueError):
        asyncio.run(provider.auth_headers("https://resource.openai.azure.com/openai/v1/responses"))
    with pytest.raises(ValueError):
        LiveAPI(provider)
    assert tokens.calls == 0


def test_official_async_azure_token_adapter_caches_and_caller_closes() -> None:
    identity = pytest.importorskip("azure.identity.aio")
    from time import time

    from azure.core.credentials import AccessToken

    class Credential:
        calls = 0
        closed = False

        async def get_token(self, *scopes: str, **kwargs: object) -> AccessToken:
            assert scopes == ("https://ai.azure.com/.default",)
            assert not self.closed
            self.calls += 1
            return AccessToken("fake-sdk-token", int(time()) + 3600)

        async def close(self) -> None:
            self.closed = True

    async def scenario() -> None:
        credential = Credential()
        callback = identity.get_bearer_token_provider(credential, "https://ai.azure.com/.default")
        async with (
            OpenAIProvider(bearer_token_provider=callback) as first,
            OpenAIProvider(bearer_token_provider=callback) as second,
        ):
            assert await first.auth_headers(first.base_url) == {"Authorization": "Bearer fake-sdk-token"}
            assert await second.auth_headers(second.base_url) == {"Authorization": "Bearer fake-sdk-token"}
        assert credential.calls == 1 and not credential.closed
        await credential.close()
        assert credential.closed

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["client", "responses"])
def test_agent_live_uses_provider_auth_and_deployments(tmp_path: Path, mode: Literal["client", "responses"]) -> None:
    async def scenario() -> None:
        tokens = Tokens()
        seen: list[dict[str, object]] = []

        async def handle(request: web.Request) -> web.WebSocketResponse:
            assert request.path == "/openai/v1/live/sessions"
            assert request.headers["Authorization"] == "Bearer fake-token-1"
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            start = await ws.receive_json()
            seen.append(start["session"])
            await ws.send_json({"type": "session.started", "session": {"id": "s"}})
            await ws.send_json({"type": "session.closed", "usage": {}, "reason": "completed"})
            await ws.close()
            return ws

        async with server(handle) as prefix:
            sessions = SessionManager(tmp_path / "agents.db")
            backend = Agent(
                provider=FoundryProvider(base_url=prefix, model="backend-deployment", credential=tokens),
                session_manager=sessions,
            )
            voice = Agent(
                provider=FoundryProvider(
                    base_url=prefix,
                    model="voice-deployment",
                    credential=tokens,
                    live_config=LiveConfig(delegation=mode, backend_model="backend-deployment"),
                ),
                session_manager=sessions,
                delegation_agent=backend if mode == "client" else None,
                audio=AudioDuplex(input=BlockingMic([])),
            )
            try:
                async with asyncio.timeout(HANG_GUARD):
                    events = [event async for event in voice.run()]
                assert not any(isinstance(event, ErrorEvent) for event in events)
            finally:
                await voice.close()
                await backend.close()
            assert tokens.calls == 1 and not tokens.closed
        assert seen[0]["model"] == "voice-deployment"
        delegation = seen[0]["delegation"]
        assert isinstance(delegation, dict) and delegation["type"] == mode
        if mode == "responses":
            assert delegation["responses"]["model"] == "backend-deployment"

    asyncio.run(scenario())


def test_foundry_credential_selection() -> None:
    prefix = "https://resource.openai.azure.com/openai/v1"
    tokens = Tokens()
    with pytest.raises(ValueError, match="not both"):
        FoundryProvider(base_url=prefix, model="m", credential=tokens, api_key="key")
    with pytest.raises(ValueError, match="requires credential"):
        FoundryProvider(base_url=prefix, model="m")
    with pytest.raises(ValueError, match="scope"):
        FoundryProvider(base_url=prefix, model="m", credential=tokens, scope="")
    provider = FoundryProvider(base_url=prefix, model="m", credential=tokens)
    assert provider.api_key == ""
    assert asyncio.run(provider.auth_headers(prefix)) == {"Authorization": "Bearer fake-token-1"}
    assert tokens.scopes == [("https://ai.azure.com/.default",)]
    asyncio.run(provider.close())
    assert not tokens.closed


def test_foundry_import_and_key_auth_without_azure_sdk() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import builtins
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == 'azure' or name.startswith('azure.'):
        raise AssertionError('Azure SDK must not be imported')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
import asyncio
from nagents import FoundryProvider
async def main():
    async with FoundryProvider(base_url='https://resource.openai.azure.com/openai/v1', model='m', api_key='key') as p:
        assert await p.auth_headers(p.base_url) == {'Authorization': 'Bearer key'}
asyncio.run(main())
""",
        ],
        capture_output=True,
        text=True,
        timeout=HANG_GUARD,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("cancel", [False, True])
def test_sync_credential_does_not_block_voice_loop_and_remains_caller_owned(cancel: bool) -> None:
    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        loop_thread = threading.get_ident()
        entered, finished = asyncio.Event(), asyncio.Event()
        release = threading.Event()

        class Credential:
            closed = False
            calls = 0

            def get_token(self, *scopes: str) -> Token:
                assert threading.get_ident() != loop_thread
                assert scopes == ("https://ai.azure.com/.default",)
                assert not self.closed
                self.calls += 1
                loop.call_soon_threadsafe(entered.set)
                try:
                    assert release.wait(HANG_GUARD)
                    assert not self.closed
                    return Token(f"sync-{self.calls}")
                finally:
                    loop.call_soon_threadsafe(finished.set)

            def close(self) -> None:
                self.closed = True

        credential = Credential()
        async with FoundryProvider(
            base_url="https://resource.openai.azure.com/openai/v1", model="m", credential=credential
        ) as provider:
            task = asyncio.create_task(provider.auth_headers(provider.base_url))
            try:
                await asyncio.wait_for(entered.wait(), HANG_GUARD)
                # This coroutine is advancing while synchronous acquisition waits.
                assert not task.done()
                if cancel:
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                    assert not credential.closed
                release.set()
                await asyncio.wait_for(finished.wait(), HANG_GUARD)
                if not cancel:
                    assert await task == {"Authorization": "Bearer sync-1"}
                    assert await provider.auth_headers(provider.base_url) == {"Authorization": "Bearer sync-2"}
                assert provider.api_key == ""
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
        assert not credential.closed
        credential.close()
        assert credential.closed

    asyncio.run(scenario())


@pytest.mark.parametrize("wrapped", [False, True])
def test_async_credential_executes_on_original_loop_even_through_wrapper(wrapped: bool) -> None:
    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        loop_thread = threading.get_ident()
        seen: list[tuple[str, ...]] = []

        class AsyncCredential:
            async def get_token(self, *scopes: str) -> Token:
                assert asyncio.get_running_loop() is loop
                assert threading.get_ident() == loop_thread
                await asyncio.sleep(0)
                seen.append(scopes)
                return Token("async-token")

        class Wrapper:
            def get_token(self, *scopes: str) -> Awaitable[Token]:
                assert threading.get_ident() != loop_thread
                return AsyncCredential().get_token(*scopes)

        async with FoundryProvider(
            base_url="https://resource.openai.azure.com/openai/v1",
            model="m",
            credential=Wrapper() if wrapped else AsyncCredential(),
            scope="custom/.default",
        ) as provider:
            assert await provider.auth_headers(provider.base_url) == {"Authorization": "Bearer async-token"}
        assert seen == [("custom/.default",)]

    asyncio.run(scenario())


def test_both_default_azure_credential_classes_satisfy_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("azure.identity")
    from azure.identity import DefaultAzureCredential
    from azure.identity.aio import DefaultAzureCredential as AsyncDefaultAzureCredential

    async def scenario() -> None:
        loop_thread = threading.get_ident()

        def sync_token(self: DefaultAzureCredential, *scopes: str) -> Token:
            assert threading.get_ident() != loop_thread
            assert scopes == ("https://ai.azure.com/.default",)
            return Token("sdk-sync")

        async def async_token(self: AsyncDefaultAzureCredential, *scopes: str) -> Token:
            assert threading.get_ident() == loop_thread
            assert scopes == ("https://ai.azure.com/.default",)
            return Token("sdk-async")

        # Real SDK credential objects, with token acquisition replaced: no Azure IO.
        monkeypatch.setattr(DefaultAzureCredential, "get_token", sync_token)
        monkeypatch.setattr(AsyncDefaultAzureCredential, "get_token", async_token)
        sync_credential = DefaultAzureCredential()
        async_credential = AsyncDefaultAzureCredential()
        credentials: tuple[TokenCredential | AsyncTokenCredential, ...] = (sync_credential, async_credential)
        try:
            for credential, value in zip(credentials, ("sdk-sync", "sdk-async"), strict=True):
                async with FoundryProvider(
                    base_url="https://resource.openai.azure.com/openai/v1", model="m", credential=credential
                ) as provider:
                    assert await provider.auth_headers(provider.base_url) == {"Authorization": f"Bearer {value}"}
        finally:
            sync_credential.close()
            await async_credential.close()

    asyncio.run(scenario())
