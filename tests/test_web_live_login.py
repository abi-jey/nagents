"""GPT-login Live follows the observed WebRTC and sideband contract."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from aiohttp import web

from nagents.provider.openai import CodexCredentials
from nagents.web import live_login
from nagents.web.live_login import ChatGPTLiveConnection
from nagents.web.live_login import LoginVoiceConfig
from nagents.web.live_login import LoginVoiceError
from nagents.web.live_login import normalize_event
from nagents.web.live_runtime import LiveService

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from collections.abc import Awaitable
    from collections.abc import Callable

    from nagents import Agent
    from nagents.web.live_handoff import LoginHandoff
    from nagents.web.live_login import Payload

OFFER = "v=0\r\ns=fixture-offer\r\n"
ANSWER = "v=0\r\ns=fixture-answer\r\n"
SECRET = "fixture-login-token"


async def credentials() -> CodexCredentials:
    return CodexCredentials(SECRET, "fixture-account")


async def answer(request: LoginHandoff) -> str:
    return "Four."


def config(handler: Callable[[LoginHandoff], Awaitable[str]] = answer) -> LoginVoiceConfig:
    return LoginVoiceConfig(credentials, "gpt-live-1-codex", "cove", "Use the selected assistant.", handler)


@asynccontextmanager
async def upstream(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
) -> AsyncIterator[None]:
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handler)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    base = f"http://127.0.0.1:{runner.addresses[0][1]}"
    monkeypatch.setattr(live_login, "CALLS_URL", base + "/calls?intent=quicksilver&architecture=avas")
    monkeypatch.setattr(live_login, "SIDEBAND_URL", base.replace("http:", "ws:") + "/v1/live/")
    try:
        yield
    finally:
        await runner.cleanup()


@pytest.mark.parametrize("seeded", [False, True])
def test_login_voice_provisions_delegates_with_transcripts_and_finalizes(
    monkeypatch: pytest.MonkeyPatch, seeded: bool
) -> None:
    async def scenario() -> None:
        requests: list[str] = []
        commands: list[Payload] = []
        handoffs: list[LoginHandoff] = []
        replied = asyncio.Event()
        history: tuple[Payload, ...] = (
            ({"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Old saved request"}]},)
            if seeded
            else ()
        )

        async def backend(handoff: LoginHandoff) -> str:
            handoffs.append(handoff)
            return "Four."

        async def handle(request: web.Request) -> web.StreamResponse:
            assert request.headers["Authorization"] == "Bearer " + SECRET
            assert request.headers["ChatGPT-Account-Id"] == "fixture-account"
            assert request.headers["OpenAI-Alpha"] == "quicksilver=v2"
            requests.append(request.path)
            if request.method == "POST":
                body = await request.json()
                assert body == {
                    "sdp": OFFER,
                    "session": {
                        "model": "gpt-live-1-codex",
                        "instructions": "Use the selected assistant.",
                        "audio": {"output": {"voice": "cove"}},
                        "delegation": {"type": "client"},
                        "initial_items": list(history),
                    },
                }
                return web.Response(status=201, text=ANSWER, headers={"Location": "/calls/rtc_fixture"})
            assert request.path == "/v1/live/rtc_fixture"
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            assert not handoffs, "startup history must not create an assistant request"
            await socket.send_json(
                {"type": "output_transcript.added", "item": {"text": "Go ahead."}, "start_ms": 0, "end_ms": 20}
            )
            await socket.send_json(
                {"type": "input_transcript.added", "item": {"text": "Two plus two?"}, "start_ms": 30, "end_ms": 50}
            )
            notice = {
                "type": "delegation.created",
                "offset_ms": 60,
                "item": {
                    "id": "delegation-1",
                    "type": "delegation",
                    "target": "client",
                    "content": [{"type": "input_text", "text": "Calculate two plus two."}],
                },
            }
            await socket.send_json(notice)
            await socket.send_json(notice)
            async for message in socket:
                event = message.json()
                commands.append(event)
                if event["type"] == "delegation.context.append":
                    replied.set()
                elif event["type"] == "session.close":
                    await socket.send_json(
                        {"type": "session.closed", "reason": "client_request", "usage": {"audio_duration_ms": 1000}}
                    )
                    break
            return socket

        async with upstream(monkeypatch, handle):
            connection = ChatGPTLiveConnection(replace(config(backend), history=history))
            assert await connection.provision(OFFER) == ANSWER
            events: list[Payload] = []

            async def read() -> None:
                async for event in connection.events():
                    events.append(event)

            reader = asyncio.create_task(read())
            try:
                async with asyncio.timeout(5):
                    await replied.wait()
                    await connection.close()
                    await reader
            finally:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
                await connection.aclose()
            assert requests == ["/calls", "/v1/live/rtc_fixture"]
            assert connection.finalized
            assert events[0] == {"type": "connection.attached", "session": {"id": "rtc_fixture"}}
            assert len(handoffs) == 1 and handoffs[0].text == "Calculate two plus two."
            assert handoffs[0].identifier == "delegation-1" and handoffs[0].offset_ms == 60
            assert [part["speaker"] for part in json.loads(handoffs[0].transcript)] == ["assistant", "user"]
            assert commands == [
                {
                    "type": "delegation.context.append",
                    "delegation_item_id": "delegation-1",
                    "content": [{"type": "input_text", "text": "Four."}],
                },
                {"type": "session.close"},
            ]
            assert events[-1]["usage"] == {"audio_duration_ms": 1000, "seconds": 1.0}

    asyncio.run(scenario())


def test_login_results_keep_each_requesting_delegation_item_id_across_chunks_and_duplicate_notices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        expected = {"item-first": "First result. " * 90, "item-second": "Second result."}
        results = {identifier: "" for identifier in expected}
        order: list[str] = []
        calls: list[LoginHandoff] = []
        replied = asyncio.Event()

        async def backend(handoff: LoginHandoff) -> str:
            calls.append(handoff)
            return expected[handoff.identifier]

        async def handle(request: web.Request) -> web.StreamResponse:
            if request.method == "POST":
                return web.Response(status=201, text=ANSWER, headers={"Location": "/calls/rtc_fixture"})
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            await socket.send_json(
                {
                    "type": "input_transcript.added",
                    "item": {"text": "Current caller request"},
                    "start_ms": 10,
                    "end_ms": 20,
                }
            )
            for identifier in ("item-first", "item-first", "item-second"):
                await socket.send_json(
                    {
                        "type": "delegation.created",
                        "offset_ms": 20,
                        "item": {
                            "id": identifier,
                            "type": "delegation",
                            "target": "client",
                            "handoff_id": "different-handoff-id",
                            "content": [{"type": "input_text", "text": "Current caller request"}],
                        },
                    }
                )
            async for message in socket:
                event = message.json()
                if event["type"] == "session.close":
                    await socket.send_json({"type": "session.closed"})
                    break
                assert event["type"] == "delegation.context.append"
                assert "channel" not in event and "delegation_id" not in event
                identifier = event["delegation_item_id"]
                assert identifier in expected and identifier != "different-handoff-id"
                assert len(event["content"]) == 1 and event["content"][0]["type"] == "input_text"
                results[identifier] += event["content"][0]["text"]
                order.append(identifier)
                if results == expected:
                    replied.set()
            return socket

        async with upstream(monkeypatch, handle):
            connection = ChatGPTLiveConnection(config(backend))
            await connection.provision(OFFER)

            async def read() -> None:
                async for _ in connection.events():
                    pass

            reader = asyncio.create_task(read())
            try:
                async with asyncio.timeout(5):
                    await replied.wait()
                    await connection.close()
                    await reader
            finally:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
                await connection.aclose()
            assert len(calls) == 2 and all(request.text == "Current caller request" for request in calls)
            assert order.count("item-first") > 1 and order[-1] == "item-second"
            assert results == expected and connection.finalized

    asyncio.run(scenario())


@pytest.mark.parametrize("identifier", [None, True, "", " ", "x" * 257])
def test_login_result_without_a_valid_provider_delegation_id_never_uses_unbound_speech(identifier: object) -> None:
    async def scenario() -> None:
        connection = ChatGPTLiveConnection(config())
        with pytest.raises(LoginVoiceError, match="invalid delegation event"):
            normalize_event({"type": "delegation.created", "item": {"id": identifier, "target": "client"}})
        with pytest.raises(LoginVoiceError, match="invalid delegation result"):
            await connection._result({"delegation_id": identifier, "content": "An answer"})

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [301, 401, 403, 429, 500])
def test_login_provision_failures_are_safe_and_never_redirect(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    async def scenario() -> None:
        requests: list[str] = []

        async def handle(request: web.Request) -> web.StreamResponse:
            requests.append(request.path)
            return web.Response(status=status, text=SECRET, headers={"Location": "/wrong"})

        async with upstream(monkeypatch, handle):
            connection = ChatGPTLiveConnection(config())
            with pytest.raises(LoginVoiceError) as failure:
                await connection.provision(OFFER)
            assert SECRET not in str(failure.value)
            await connection.aclose()
            assert requests == ["/calls"] and not connection.identifier

    asyncio.run(scenario())


def test_provisioned_call_is_finalized_when_creation_is_abandoned(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        commands: list[str] = []

        async def handle(request: web.Request) -> web.StreamResponse:
            if request.method == "POST":
                return web.Response(status=201, text=ANSWER, headers={"Location": "/calls/rtc_abandoned"})
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            async for message in socket:
                event = message.json()
                commands.append(event["type"])
                await socket.send_json({"type": "session.closed", "reason": "client_request"})
                break
            return socket

        async with upstream(monkeypatch, handle):
            connection = ChatGPTLiveConnection(config())
            await connection.provision(OFFER)
            await connection.aclose()
            assert commands == ["session.close"] and connection.finalized

    asyncio.run(scenario())


@pytest.mark.parametrize("confirmed", [True, False])
def test_login_supervisor_waits_for_final_event_and_does_not_treat_eof_as_confirmation(
    monkeypatch: pytest.MonkeyPatch, confirmed: bool
) -> None:
    async def scenario() -> None:
        backend_started = asyncio.Event()
        backend_stopped = asyncio.Event()
        close_received = asyncio.Event()
        finish_close = asyncio.Event()
        commands: list[str] = []
        attachments = 0

        async def pending_backend(handoff: LoginHandoff) -> str:
            backend_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                backend_stopped.set()
            return "This pending result must not write after session.close"

        async def handle(request: web.Request) -> web.StreamResponse:
            nonlocal attachments
            if request.method == "POST":
                return web.Response(status=201, text=ANSWER, headers={"Location": "/calls/rtc_supervised"})
            attachments += 1
            if attachments > 1:
                return web.Response(status=404)  # A finalized/lost call cannot be reattached.
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            await socket.send_json(
                {"type": "input_transcript.added", "item": {"text": "Long task"}, "start_ms": 0, "end_ms": 10}
            )
            await socket.send_json(
                {
                    "type": "delegation.created",
                    "item": {
                        "id": "long",
                        "type": "delegation",
                        "target": "client",
                        "content": [{"type": "input_text", "text": "Long task"}],
                    },
                }
            )
            async for message in socket:
                kind = str(message.json()["type"])
                commands.append(kind)
                if kind == "session.close":
                    close_received.set()
                    await finish_close.wait()
                    if confirmed:
                        await socket.send_json(
                            {"type": "session.closed", "reason": "client_request", "usage": {"audio_duration_ms": 1234}}
                        )
                    break
            await socket.close()
            return socket

        def no_public_agent(voice: str) -> Agent:
            raise AssertionError("Login calls must not construct a public-API voice agent")

        async with upstream(monkeypatch, handle):
            service = LiveService(no_public_agent, login_factory=lambda voice: config(pending_backend))
            try:
                async with asyncio.timeout(5):
                    created = await service.create(OFFER)
                    identifier = str(created["session_id"])
                    assert identifier != "rtc_supervised" and created["sdp"] == ANSWER
                    assert service.active_session_id == identifier
                    assert (await service.snapshot(identifier))["status"] == "connected"
                    await backend_started.wait()
                    closing = asyncio.create_task(service.close(identifier))
                    await close_received.wait()
                    await backend_stopped.wait()
                    assert service.active_session_id == identifier and not closing.done()
                    assert (await service.snapshot(identifier))["status"] == "closing"
                    finish_close.set()
                    result = await closing
                assert commands == ["session.close"]
                assert not service.active_session_id
                assert result["status"] == ("closed" if confirmed else "error")
                assert ("Finalization confirmed." if confirmed else "Finalization is unconfirmed.") in str(
                    result["message"]
                )
                assert attachments == (1 if confirmed else 2)
            finally:
                finish_close.set()
                await service.shutdown()

    asyncio.run(scenario())


def test_fragment_normalization_keeps_exact_text_and_timing() -> None:
    assert normalize_event(
        {"type": "input_transcript.added", "start_ms": 123, "end_ms": 456, "item": {"text": " repeated repeated "}}
    ) == {
        "type": "session.input_transcript.delta",
        "delta": " repeated repeated ",
        "start_ms": 123,
        "end_ms": 456,
    }
    assert normalize_event({"type": "delegation.created", "item": {"id": "ignored", "target": "responses"}}) == {}
    with pytest.raises(LoginVoiceError):
        normalize_event({"type": "input_transcript.added", "item": {"text": 5}})
    with pytest.raises(LoginVoiceError):
        normalize_event({"type": "delegation.created", "item": {"target": "client"}})


@pytest.mark.parametrize("start,end", [(True, 2), (0, "2"), (2, 1), (-1, 2), (0, float("nan"))])
def test_invalid_transcript_timestamps_never_reach_the_assistant(start: object, end: object) -> None:
    with pytest.raises(LoginVoiceError):
        normalize_event({"type": "input_transcript.added", "start_ms": start, "end_ms": end, "item": {"text": "hi"}})
