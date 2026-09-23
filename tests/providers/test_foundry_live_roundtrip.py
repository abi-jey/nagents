"""Offline Foundry delegation round trips through real Agent/HTTP/WS transports."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest
from aiohttp import web

from nagents import Agent
from nagents import FoundryProvider
from nagents import SessionManager
from nagents.audio import AudioDuplex
from nagents.events import AudioTranscriptDeltaEvent
from nagents.events import DoneEvent
from nagents.events import ErrorEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.events import ToolResultEvent
from nagents.live import LiveConfig
from nagents.live import LiveEvent
from nagents.types import RetryConfig
from tests.providers.test_foundry_auth import Token
from tests.providers.test_foundry_auth import Tokens
from tests.providers.test_foundry_auth import server
from tests.providers.test_live import BlockingMic
from tests.providers.test_live import delegation
from tests.providers.test_live_native import function_item
from tests.support.hang_guard import HANG_GUARD

if TYPE_CHECKING:
    from pathlib import Path


class NamedTokens(Tokens):
    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name

    async def __call__(self) -> str:
        return f"{self.name}-{await super().__call__()}"


@pytest.mark.parametrize("sync_backend", [False, True])
@pytest.mark.parametrize("key_voice", [False, True])
def test_client_delegation_separate_credentials_endpoints_and_bubbled_result(
    tmp_path: Path, sync_backend: bool, key_voice: bool
) -> None:
    async def scenario() -> None:
        voice_tokens, backend_tokens = NamedTokens("voice"), NamedTokens("backend")
        voice_requests: list[tuple[str, str]] = []
        backend_requests: list[tuple[str, str]] = []
        commentaries: list[dict[str, object]] = []
        transcript = "What is the verified delivery date?"
        answer = "The verified delivery date is October 12."

        class SyncBackendCredential:
            def get_token(self, *scopes: str) -> Token:
                assert not backend_tokens.closed
                backend_tokens.scopes.append(scopes)
                backend_tokens.calls += 1
                return Token(f"backend-fake-token-{backend_tokens.calls}")

        async def handle_backend(request: web.Request) -> web.Response:
            backend_requests.append((request.path, request.headers["Authorization"]))
            assert request.method == "POST" and request.path == "/openai/v1/responses"
            assert "api-key" not in request.headers
            body = await request.json()
            assert body["model"] == "delivery-deployment" and body["stream"] is False
            assert transcript in json.dumps(body["input"])
            # Retry the actual backend request: voice auth must remain independent.
            if len(backend_requests) == 1:
                return web.Response(status=503)
            return web.json_response(
                {
                    "id": "backend-response",
                    "status": "completed",
                    "output": [{"type": "message", "content": [{"type": "output_text", "text": answer}]}],
                    "usage": {"input_tokens": 10, "output_tokens": 8, "total_tokens": 18},
                }
            )

        async def handle_voice(request: web.Request) -> web.WebSocketResponse:
            voice_requests.append((request.path, request.headers["api-key" if key_voice else "Authorization"]))
            assert request.method == "GET" and request.path == "/openai/v1/live/sessions"
            assert ("Authorization" if key_voice else "api-key") not in request.headers
            assert not request.query_string
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            start = await asyncio.wait_for(socket.receive_json(), HANG_GUARD)
            assert start["type"] == "session.start"
            assert start["session"]["model"] == "voice-deployment"
            assert start["session"]["delegation"] == {"type": "client"}
            await socket.send_json({"type": "session.started", "session": {"id": "voice-session"}})
            await socket.send_json(
                {"type": "session.input_transcript.delta", "delta": transcript, "start_ms": 0, "end_ms": 50}
            )
            await socket.send_json(delegation("client-task"))
            result = await asyncio.wait_for(socket.receive_json(), HANG_GUARD)
            commentaries.append(result)
            assert result["type"] == "session.commentary.append"
            assert result["delegation_id"] == "client-task" and result["content"] == answer
            await socket.send_json(
                {
                    "type": "session.commentary.appended",
                    "client_event_id": result["event_id"],
                    "start_ms": 60,
                    "end_ms": 90,
                }
            )
            await socket.send_json(
                {"type": "session.output_transcript.delta", "delta": answer, "start_ms": 60, "end_ms": 90}
            )
            await socket.send_json({"type": "session.closed", "usage": {"duration": 1}, "reason": "completed"})
            await socket.close()
            return socket

        async with server(handle_voice) as voice_prefix, server(handle_backend) as backend_prefix:
            assert voice_prefix != backend_prefix
            backend = Agent(
                provider=FoundryProvider(
                    base_url=backend_prefix,
                    model="delivery-deployment",
                    credential=SyncBackendCredential() if sync_backend else backend_tokens,
                    scope="backend-audience/.default",
                    retry_config=RetryConfig(base_delay=0, max_retries=1),
                ),
                session_manager=SessionManager(tmp_path / "backend.db"),
                streaming=False,
                compactor=None,
            )
            voice = Agent(
                provider=FoundryProvider(
                    base_url=voice_prefix,
                    model="voice-deployment",
                    credential=None if key_voice else voice_tokens,
                    api_key="voice-key" if key_voice else "",
                    scope="voice-audience/.default",
                    live_config=LiveConfig(delegation="client"),
                ),
                session_manager=SessionManager(tmp_path / "voice.db"),
                delegation_agent=backend,
                audio=AudioDuplex(input=BlockingMic([])),
                compactor=None,
            )
            try:
                async with asyncio.timeout(HANG_GUARD):
                    events = [event async for event in voice.run()]
                assert voice.live.status.finalized
            finally:
                await voice.close()
                await backend.close()

        assert voice_requests == [
            ("/openai/v1/live/sessions", "voice-key" if key_voice else "Bearer voice-fake-token-1")
        ]
        assert backend_requests == [
            ("/openai/v1/responses", "Bearer backend-fake-token-1"),
            ("/openai/v1/responses", "Bearer backend-fake-token-2"),
        ]
        assert voice_tokens.scopes == ([] if key_voice else [("voice-audience/.default",)])
        assert backend_tokens.scopes == [("backend-audience/.default",)] * 2
        assert len(commentaries) == 1
        assert not any(isinstance(event, ErrorEvent) for event in events)
        text = [event for event in events if isinstance(event, TextDoneEvent)]
        done = [
            event for event in events if isinstance(event, DoneEvent) and event.extra.get("source") == "live_backend"
        ]
        assert len(text) == len(done) == 1
        assert text[0].text == done[0].final_text == answer
        assert text[0].usage.total_tokens == 18
        for event in (text[0], done[0]):
            assert event.extra["source"] == "live_backend" and event.extra["delegation_id"] == "client-task"
        assert any(isinstance(event, AudioTranscriptDeltaEvent) and event.delta == answer for event in events)
        assert any(
            isinstance(event, LiveEvent) and event.event_type == "session.commentary.appended" for event in events
        )
        assert not voice_tokens.closed and not backend_tokens.closed
        await voice_tokens.close()
        await backend_tokens.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("key_auth", [False, True])
def test_hosted_delegation_executes_tool_and_continues_response(tmp_path: Path, key_auth: bool) -> None:
    async def scenario() -> None:
        tokens = NamedTokens("hosted")
        requests: list[tuple[str, str]] = []
        commands: list[dict[str, object]] = []
        executions: list[int] = []
        answer = "The verified result is 42."

        def lookup(value: int) -> str:
            """Look up a verified result for a value."""
            executions.append(value)
            return f"verified-{value}"

        async def handle(request: web.Request) -> web.WebSocketResponse:
            requests.append((request.path, request.headers["api-key" if key_auth else "Authorization"]))
            assert ("Authorization" if key_auth else "api-key") not in request.headers
            assert not request.query_string
            assert request.method == "GET" and request.path == "/openai/v1/live/sessions"
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            start = await asyncio.wait_for(socket.receive_json(), HANG_GUARD)
            assert start["type"] == "session.start"
            assert start["session"]["model"] == "voice-deployment"
            config = start["session"]["delegation"]
            assert config["type"] == "responses" and config["responses"]["model"] == "hosted-deployment"
            assert any(tool["name"] == "lookup" and tool["type"] == "function" for tool in config["responses"]["tools"])
            await socket.send_json({"type": "session.started", "session": {"id": "hosted-session"}})
            await socket.send_json(delegation("d1", target="responses"))

            async def response(event: dict[str, object]) -> None:
                await socket.send_json({"type": "response.event", "delegation_id": "d1", "event": event})

            await response({"type": "response.created", "response": {"id": "r1"}})
            await socket.send_json(function_item("call-1", name="lookup", arguments='{"value": 42}'))
            await response({"type": "response.completed", "response": {"id": "r1", "status": "completed"}})
            output = await asyncio.wait_for(socket.receive_json(), HANG_GUARD)
            commands.append(output)
            assert output["type"] == "response.item.create"
            assert output["item"]["type"] == "function_call_output" and output["item"]["call_id"] == "call-1"
            assert json.loads(output["item"]["output"]) == "verified-42"
            continuation = await asyncio.wait_for(socket.receive_json(), HANG_GUARD)
            commands.append(continuation)
            assert continuation["type"] == "response.create"
            await response({"type": "response.created", "response": {"id": "r2"}})
            await response({"type": "response.output_text.delta", "delta": answer})
            await response({"type": "response.completed", "response": {"id": "r2", "status": "completed"}})
            await socket.send_json(
                {"type": "session.output_transcript.delta", "delta": answer, "start_ms": 100, "end_ms": 200}
            )
            await socket.send_json({"type": "session.closed", "usage": {"duration": 1}, "reason": "completed"})
            await socket.close()
            return socket

        async with server(handle) as prefix:
            voice = Agent(
                provider=FoundryProvider(
                    base_url=prefix,
                    model="voice-deployment",
                    credential=None if key_auth else tokens,
                    api_key="hosted-key" if key_auth else "",
                    live_config=LiveConfig(delegation="responses", backend_model="hosted-deployment"),
                ),
                session_manager=SessionManager(tmp_path / "hosted.db"),
                tools=[lookup],
                audio=AudioDuplex(input=BlockingMic([])),
                compactor=None,
            )
            try:
                async with asyncio.timeout(HANG_GUARD):
                    events = [event async for event in voice.run()]
                assert voice.live.status.finalized
            finally:
                await voice.close()

        # Hosted inference stays on the authenticated Live socket, not local HTTP.
        assert requests == [("/openai/v1/live/sessions", "hosted-key" if key_auth else "Bearer hosted-fake-token-1")]
        assert tokens.calls == (0 if key_auth else 1)
        assert tokens.scopes == ([] if key_auth else [("https://ai.azure.com/.default",)])
        assert executions == [42]
        assert [command["type"] for command in commands] == ["response.item.create", "response.create"]
        assert not any(isinstance(event, ErrorEvent) for event in events)
        calls = [event for event in events if isinstance(event, ToolCallEvent)]
        results = [event for event in events if isinstance(event, ToolResultEvent)]
        assert len(calls) == len(results) == 1
        assert calls[0].id == results[0].id == "call-1"
        assert calls[0].name == results[0].name == "lookup" and calls[0].arguments == {"value": 42}
        assert results[0].result == "verified-42" and results[0].error is None
        for event in (calls[0], results[0]):
            assert event.extra == {"source": "live_backend", "delegation_id": "d1", "response_id": "r1"}
        envelopes = [
            event.payload for event in events if isinstance(event, LiveEvent) and event.event_type == "response.event"
        ]
        assert envelopes[-1] == {
            "type": "response.event",
            "delegation_id": "d1",
            "event": {"type": "response.completed", "response": {"id": "r2", "status": "completed"}},
        }
        assert any(isinstance(event, AudioTranscriptDeltaEvent) and event.delta == answer for event in events)
        assert not tokens.closed
        await tokens.close()

    asyncio.run(scenario())
