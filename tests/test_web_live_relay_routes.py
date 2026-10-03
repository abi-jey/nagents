"""Real HTTP admission, native login sideband and local provider media fixture."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from aiohttp import web
from aiortc import AudioStreamTrack
from aiortc import RTCConfiguration
from aiortc import RTCPeerConnection
from aiortc import RTCSessionDescription

from nagents.types import Message
from nagents.web import live_login
from tests.support.web import client_app
from tests.support.web import no_guarded_workspace_io as no_guarded_workspace_io
from tests.test_web_live import SECRET
from tests.test_web_live import chatgpt_login as chatgpt_login
from tests.test_web_live import configuration
from tests.test_web_live import configure
from tests.test_web_live import environment as environment

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from nagents.web.live_runtime import LiveService


def test_login_relay_routes_survive_reload_without_browser_sdp_or_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        peers: list[RTCPeerConnection] = []
        sessions: list[dict[str, object]] = []
        commands: list[str] = []

        async def provision(request: web.Request) -> web.Response:
            assert request.headers["Authorization"] == f"Bearer {SECRET}"
            assert request.headers["ChatGPT-Account-Id"] == "fixture-account"
            assert request.headers["OpenAI-Alpha"] == "quicksilver=v2"
            body = await request.json()
            sessions.append(body["session"])
            peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
            peers.append(peer)
            peer.addTrack(AudioStreamTrack())
            await peer.setRemoteDescription(RTCSessionDescription(sdp=body["sdp"], type="offer"))
            await peer.setLocalDescription(await peer.createAnswer())
            assert peer.localDescription is not None
            return web.Response(
                status=201,
                text=peer.localDescription.sdp,
                headers={"Location": f"/calls/rtc_fixture_{len(peers)}"},
            )

        async def sideband(request: web.Request) -> web.WebSocketResponse:
            assert request.headers["Authorization"] == f"Bearer {SECRET}"
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            await socket.send_json(
                {
                    "type": "input_transcript.added",
                    "item": {"id": "input1", "text": "Hello"},
                    "start_ms": 0,
                    "end_ms": 100,
                }
            )
            await socket.send_json(
                {
                    "type": "output_transcript.added",
                    "item": {"id": "output1", "text": "Hi"},
                    "start_ms": 100,
                    "end_ms": 200,
                }
            )
            async for message in socket:
                event = message.json()
                commands.append(event["type"])
                assert event["type"] == "session.close"
                # The primary media peer must still be alive when the owned
                # sideband receives End, including app shutdown without HTTP End.
                assert peers[-1].connectionState == "connected"
                await socket.send_json({"type": "session.closed"})
                break
            return socket

        upstream = web.Application()
        upstream.router.add_post("/calls", provision)
        upstream.router.add_get("/live/{id}", sideband)
        runner = web.AppRunner(upstream, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        origin = f"http://127.0.0.1:{runner.addresses[0][1]}"
        monkeypatch.setattr(live_login, "CALLS_URL", origin + "/calls")
        monkeypatch.setattr(live_login, "SIDEBAND_URL", origin + "/live/")
        ids: list[str] = []
        try:
            for cycle, restart in enumerate((False, True), start=1):
                phase = "startup"
                caption_count = 0
                watchdog = asyncio.timeout(15)
                try:
                    # Each complete lifecycle has its own watchdog; time spent
                    # in the first app must not consume the restarted app's guard.
                    async with watchdog:
                        async with client_app(tmp_path, config=configuration(tmp_path)) as (
                            app,
                            client,
                            headers,
                            harnesses,
                        ):
                            phase = "configure"
                            await harnesses[0].agent.session.add_message(
                                harnesses[0].session_id, Message(role="user", content="A saved startup detail")
                            )
                            await configure(client, headers)
                            preferences = (
                                await client.get("/api/live/settings?scope=workspace", headers=headers)
                            ).json()
                            configured = await client.post(
                                "/api/live/settings",
                                headers=headers,
                                json={
                                    "scope": "workspace",
                                    "revision": preferences["revision"],
                                    "overrides": {**preferences["overrides"], "instructions": "Use brief replies."},
                                },
                            )
                            assert configured.status_code == 200
                            revision = configured.json()["revision"]
                            phase = "provision"
                            response = await client.post(
                                "/api/live/sessions",
                                headers=headers,
                                json={
                                    "revision": revision,
                                    "session_id": harnesses[0].session_id,
                                },
                            )
                            assert response.status_code == 201, response.text
                            created = response.json()
                            assert set(created) == {"session_id", "model", "voice", "context"}
                            assert created["model"] == "gpt-live-1-codex" and created["voice"] == "sol"
                            assert SECRET not in response.text and "rtc_fixture" not in response.text
                            identifier = created["session_id"]
                            context_response = await client.get(
                                f"/api/live/sessions/{identifier}/context", headers=headers
                            )
                            assert context_response.status_code == 200
                            context = context_response.json()
                            assert context["available"] and context["chat_session_id"] == harnesses[0].session_id
                            assert context["instructions"]["text"] == sessions[-1]["instructions"]
                            assert "Use brief replies." in context["instructions"]["text"]
                            assert context["history"] == sessions[-1]["initial_items"]
                            assert "A saved startup detail" in str(context["history"])
                            assert "instructions" not in created["context"] and "history" not in created["context"]
                            ids.append(identifier)
                            phase = "snapshot"
                            snapshot = (await client.get(f"/api/live/sessions/{identifier}", headers=headers)).json()
                            assert snapshot["status"] == "connected"
                            # Media readiness does not wait for every sideband caption
                            # to be persisted. Windows can observe the first caption
                            # while the second is still behind that asynchronous write.
                            phase = "captions"
                            async with asyncio.timeout(3):
                                while True:
                                    captions = [
                                        event["text"] for event in snapshot["events"] if event["type"] == "transcript"
                                    ]
                                    caption_count = len(captions)
                                    if caption_count >= 2:
                                        break
                                    await asyncio.sleep(0.01)
                                    snapshot = (
                                        await client.get(f"/api/live/sessions/{identifier}", headers=headers)
                                    ).json()
                                    assert snapshot["status"] == "connected"
                            assert captions == ["Hello", "Hi"]
                            if not restart:
                                phase = "explicit_end"
                                ended = await client.post(
                                    f"/api/live/sessions/{identifier}/close", headers=headers, json={}
                                )
                                assert ended.status_code == 200 and ended.json()["status"] == "closed"
                            service: LiveService = app.state.live
                            phase = "shutdown"
                        assert service._closed and not service.active_session_id
                except TimeoutError as error:
                    error.add_note(
                        f"Relay fixture cycle={cycle} phase={phase} captions={caption_count} "
                        f"cycle_watchdog_expired={watchdog.expired()}"
                    )
                    raise
            assert len(set(ids)) == 2
            assert commands == ["session.close", "session.close"]
            assert all(session["delegation"] == {"type": "client"} for session in sessions)
            assert not [task for task in asyncio.all_tasks() if task.get_name().startswith("web-chatgpt-")]
        finally:
            for peer in peers:
                await peer.close()
            await runner.cleanup()

    asyncio.run(scenario())
