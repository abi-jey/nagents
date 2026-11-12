"""Real local WebRTC/sideband fixture for terminal subscription Voice; no account I/O."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from aiohttp import web
from aiortc import AudioStreamTrack
from aiortc import RTCConfiguration
from aiortc import RTCPeerConnection
from aiortc import RTCSessionDescription
from fastapi import HTTPException

from nagents import Provider
from nagents.events import TextDoneEvent
from nagents.harness.providers.registry import ProviderProfile
from nagents.harness.providers.registry import ProviderRegistry
from nagents.provider.codex.openai import CodexCredentials
from nagents.tui import NagentsApp
from nagents.tui import voice_runtime
from nagents.tui.voice import VoicePanel
from nagents.web.live import login
from tests.support.hang_guard import HANG_GUARD
from tests.support.providers import setup_harness
from tests.support.tui import idle
from tests.tui.test_voice import FakeLocalAudio
from tests.tui.test_voice import configure
from tests.tui.test_voice import until

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.types import Message
    from tests.support.providers import FakeProvider


@pytest.mark.usefixtures("workspace_discovery_only")
@pytest.mark.parametrize("status", [201, 403])
def test_chatgpt_voice_uses_same_identity_native_handoff_and_confirmed_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    async def scenario() -> None:
        peers: list[RTCPeerConnection] = []
        sessions: list[dict[str, object]] = []
        commands: list[dict[str, object]] = []
        spoken = asyncio.Event()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            assert messages[-1].content == "Hello"
            yield TextDoneEvent(text="Verified subscription answer")

        async def provision(request: web.Request) -> web.Response:
            assert request.headers["Authorization"] == "Bearer fixture-subscription-token"
            assert request.headers["ChatGPT-Account-Id"] == "selected-voice-account"
            assert request.headers["OpenAI-Alpha"] == "quicksilver=v2"
            if status != 201:
                return web.Response(status=status, text="PRIVATE provider body fixture-subscription-token")
            body = await request.json()
            sessions.append(body["session"])
            peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
            peers.append(peer)
            peer.addTrack(AudioStreamTrack())
            await peer.setRemoteDescription(RTCSessionDescription(sdp=body["sdp"], type="offer"))
            await peer.setLocalDescription(await peer.createAnswer())
            assert peer.localDescription is not None
            return web.Response(status=201, text=peer.localDescription.sdp, headers={"Location": "/calls/rtc_tui"})

        async def sideband(request: web.Request) -> web.WebSocketResponse:
            assert request.headers["Authorization"] == "Bearer fixture-subscription-token"
            assert request.headers["ChatGPT-Account-Id"] == "selected-voice-account"
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            await socket.send_json(
                {
                    "type": "input_transcript.added",
                    "item": {"id": "caption", "text": "Hello"},
                    "start_ms": 0,
                    "end_ms": 100,
                }
            )
            notice = {
                "type": "delegation.created",
                "item": {
                    "type": "delegation",
                    "id": "request",
                    "target": "client",
                    "content": [{"type": "input_text", "text": "Hello"}],
                },
            }
            await socket.send_json(notice)
            await socket.send_json(notice)
            async for message in socket:
                event = message.json()
                commands.append(event)
                if event["type"] == "delegation.context.append" and event["channel"] == "speakable":
                    assert event["delegation_item_id"] == "request"
                    assert event["content"] == [{"type": "input_text", "text": "Verified subscription answer"}]
                    spoken.set()
                elif event["type"] == "session.close":
                    assert peers[0].connectionState == "connected"
                    await socket.send_json({"type": "session.closed", "usage": {"audio_duration_ms": 100}})
                    break
            return socket

        upstream = web.Application()
        upstream.router.add_post("/calls", provision)
        upstream.router.add_get("/live/{id}", sideband)
        runner = web.AppRunner(upstream, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        origin = f"http://127.0.0.1:{runner.addresses[0][1]}"
        monkeypatch.setattr(login, "CALLS_URL", origin + "/calls")
        monkeypatch.setattr(login, "SIDEBAND_URL", origin + "/live/")
        monkeypatch.setattr(voice_runtime, "LocalAudio", FakeLocalAudio)

        def no_api_fallback(provider: Provider, **kwargs: object) -> str:
            raise AssertionError("A subscription call must not use the API-key transport")

        monkeypatch.setattr(Provider, "live_endpoint", no_api_fallback)
        harness, providers = setup_harness(tmp_path, monkeypatch, script)

        async def credentials() -> CodexCredentials:
            return CodexCredentials("fixture-subscription-token", "selected-voice-account")

        monkeypatch.setattr(harness.openai_auth, "logged_in", lambda: True)
        monkeypatch.setattr(harness.openai_auth, "credentials", credentials)
        app = NagentsApp(harness)
        try:
            async with app.run_test() as pilot:
                await idle(app, pilot)
                configure(harness, monkeypatch)
                current = harness.provider_store.global_store.load()
                harness.provider_store.global_store.save(
                    ProviderRegistry(
                        active="voice", providers={"voice": ProviderProfile(kind="openai", auth="chatgpt")}
                    ),
                    expected=current.revision,
                )
                voice = app._ensure_voice()
                if status == 201:
                    async with asyncio.timeout(HANG_GUARD):
                        await voice.start()
                        await spoken.wait()
                        await idle(app, pilot)
                        await until(lambda: "Hello" in str(app.query_one(VoicePanel).content))
                        assert len(providers[0].requests) == 1
                        assert sessions[0]["model"] == "gpt-live-1-codex"
                        assert sessions[0]["audio"] == {"output": {"voice": "sol"}}
                        assert sessions[0]["delegation"] == {"type": "client", "ack_filler": False}
                        await voice.stop()
                    assert voice.payload["status"] == "closed"
                    assert "Finalization confirmed" in str(voice.payload["message"])
                    assert sum(command["type"] == "session.close" for command in commands) == 1
                else:
                    with pytest.raises(HTTPException) as failure:
                        await voice.start()
                    assert "does not have access" in str(failure.value.detail)
                    assert "PRIVATE" not in str(failure.value.detail) and "fixture-subscription-token" not in str(
                        failure.value.detail
                    )
                    assert not voice.active and not providers[0].requests
                assert voice.audio is not None and voice.audio.input.closed and voice.audio.output.closed
        finally:
            await asyncio.gather(*(peer.close() for peer in peers))
            await runner.cleanup()

    asyncio.run(scenario())
