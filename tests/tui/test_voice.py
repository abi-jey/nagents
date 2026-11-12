"""Terminal Voice uses the production Harness and native local provider fixture."""

from __future__ import annotations

import asyncio
import base64
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest
from aiohttp import web
from fastapi import HTTPException
from textual.widgets import Static
from textual.widgets import Switch

from nagents import Provider
from nagents.events import TextDoneEvent
from nagents.harness.providers.registry import ProviderProfile
from nagents.harness.providers.registry import ProviderRegistry
from nagents.live.delegation import ClientDelegationRequest
from nagents.tui import NagentsApp
from nagents.tui import voice_runtime
from nagents.tui.voice import VoicePanel
from nagents.tui.voice_audio import BLOCK
from nagents.tui.voice_audio import LocalAudio
from nagents.tui.voice_bridge import TuiVoiceBridge
from nagents.tui.voice_settings import VoiceSettingsModal
from nagents.tui.widgets import Composer
from nagents.tui.widgets import Turn
from nagents.types import Message
from tests.support.hang_guard import HANG_GUARD
from tests.support.providers import setup_harness
from tests.support.tui import FakeHarness
from tests.support.tui import idle
from tests.support.tui import make_app
from tests.support.tui import send

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from collections.abc import Callable
    from pathlib import Path

    from nagents.events import Event
    from nagents.harness import Harness
    from nagents.live.delegation import LiveAppendKind
    from nagents.tui.voice_settings import VoiceEdits
    from tests.support.providers import FakeProvider


async def until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(HANG_GUARD):
        while not predicate():
            await asyncio.sleep(0.005)


def configure(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_VOICE_KEY", "fixture-voice-only-key")
    store = harness.provider_store
    registry = store.global_store.load()
    store.global_store.save(
        ProviderRegistry(
            active="voice",
            providers={"voice": ProviderProfile(kind="openai", auth="api-key", api_key_env="TEST_VOICE_KEY")},
        ),
        expected=registry.revision,
    )
    document = store.agent_models.load("assistant")
    store.agent_models.patch_live(
        "assistant",
        {"enabled": True, "model": {"provider": "voice", "id": "gpt-live-1"}},
        expected=document.revision,
    )


class FakeLocalAudio(LocalAudio):
    async def open(self) -> None:
        return None


@pytest.mark.usefixtures("workspace_discovery_only")
def test_native_pcm_captions_handoff_and_end_keep_assistant_work_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        blocked, release, heard, spoken = (asyncio.Event() for _ in range(4))
        commands: list[dict[str, object]] = []
        sessions: list[dict[str, object]] = []
        input_audio = b"\x01\x00" * BLOCK
        output_audio = b"\x02\x00" * BLOCK

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            blocked.set()
            await release.wait()
            yield TextDoneEvent(text="Verified assistant answer")

        async def provider_socket(request: web.Request) -> web.WebSocketResponse:
            assert request.headers["Authorization"] == "Bearer fixture-voice-only-key"
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            first = await socket.receive_json()
            sessions.append(first["session"])
            await socket.send_json({"type": "session.started", "session": {"id": "native-tui"}})
            async for message in socket:
                event = message.json()
                commands.append(event)
                if event["type"] == "session.input_audio.append" and not heard.is_set():
                    assert base64.b64decode(event["audio"]) == input_audio
                    heard.set()
                    await socket.send_json(
                        {"type": "session.output_audio.delta", "delta": base64.b64encode(output_audio).decode()}
                    )
                    await socket.send_json(
                        {"type": "session.input_transcript.delta", "delta": "Hello", "start_ms": 0, "end_ms": 100}
                    )
                    await socket.send_json(
                        {"type": "session.delegation.created", "delegation": {"id": "request", "target": "client"}}
                    )
                elif event["type"] == "session.commentary.append":
                    spoken.set()
                elif event["type"] == "session.close":
                    await socket.send_json({"type": "session.closed", "reason": "client_close"})
                    break
            return socket

        upstream = web.Application()
        upstream.router.add_get("/sessions", provider_socket)
        runner = web.AppRunner(upstream, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        url = f"http://127.0.0.1:{runner.addresses[0][1]}/sessions"
        monkeypatch.setattr(Provider, "live_endpoint", lambda self, **kwargs: url)
        monkeypatch.setattr(voice_runtime, "LocalAudio", FakeLocalAudio)
        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        app = NagentsApp(harness)
        try:
            async with app.run_test(size=(90, 30)) as pilot:
                await idle(app, pilot)
                configure(harness, monkeypatch)
                await harness.agent.session.add_message(
                    harness.session_id, Message(role="user", content="Saved context only")
                )
                voice = app._ensure_voice()
                await voice.start()
                assert voice.active and voice.audio is not None
                assert not app.busy and not providers[0].requests
                assert "Saved context only" in str(sessions[0]["input"])
                assert sessions[0]["delegation"] == {"type": "client"}
                composer = app.query_one(Composer)
                composer.text = "unsent draft"
                voice.audio.input.callback(input_audio, BLOCK, None, None)
                await asyncio.wait_for(blocked.wait(), HANG_GUARD)
                await until(lambda: bool(voice.audio and voice.audio.output._buffer))
                assert voice.audio.output._buffer == output_audio
                assert [message.content for message in providers[0].requests[-1] if message.role == "user"][
                    -1
                ] == "Hello"
                await until(lambda: "Hello" in str(app.query_one(VoicePanel).content))
                assert composer.text == "unsent draft"
                active = app._active
                voice.mute()
                assert voice.muted
                voice.mute()
                app.command("/new")
                assert "End Voice" in str(app.query_one("#status", Static).content)
                await voice.stop()
                assert not voice.active and voice.audio.input.closed and voice.audio.output.closed
                assert app._active is active and app.busy
                assert not spoken.is_set()  # No invented success was spoken while the assistant was blocked.
                release.set()
                await idle(app, pilot)
                assert any(message.content == "Verified assistant answer" for message in await harness.history())
                assert composer.text == "unsent draft"
                assert not spoken.is_set()  # Results after End stay in the chat, not a closed voice connection.
                assert sum(command["type"] == "session.close" for command in commands) == 1
                assert "Finalization confirmed" in str(voice.payload["message"])
        finally:
            release.set()
            await runner.cleanup()

    asyncio.run(scenario())


@pytest.mark.usefixtures("workspace_discovery_only")
def test_voice_joins_busy_root_at_response_boundary_and_slash_speech_is_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        blocked, release = asyncio.Event(), asyncio.Event()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if len(provider.requests) == 1:
                blocked.set()
                await release.wait()
                yield TextDoneEvent(text="First verified answer")
            else:
                yield TextDoneEvent(text="Second verified answer")

        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        app = NagentsApp(harness)
        sent: list[tuple[str, str, str]] = []

        async def sink(kind: LiveAppendKind, text: str, identifier: str) -> str:
            sent.append((kind, text, identifier))
            return "session." + kind + ".append"

        async with app.run_test() as pilot:
            await idle(app, pilot)
            voice = app._ensure_voice()
            bridge = TuiVoiceBridge(
                harness.session_id,
                "call",
                app._accept_voice,
                lambda report: None,
                agent="assistant",
                provider="text",
                model="normal",
            )
            voice.bridges.append(bridge)
            bridge.attach(sink)
            await send(app, pilot, "first")
            await asyncio.wait_for(blocked.wait(), HANG_GUARD)
            active = app._active
            composer = app.query_one(Composer)
            composer.text = "draft remains"
            await bridge.handle_request(ClientDelegationRequest("spoken-command", "/quit", "[]"))
            assert app._active is active and app._voice_inputs and not app._queued_prompts
            release.set()
            await idle(app, pilot)
            await until(lambda: sum(kind == "commentary" for kind, _, _ in sent) == 2)
            assert [item for item in sent if item[0] == "commentary"] == [
                ("commentary", "First verified answer", ""),
                ("commentary", "Second verified answer", "spoken-command"),
            ]
            assert len(providers[0].requests) == 2
            assert any(message.content == "/quit" for message in await harness.history())
            assert not app._closing_requested and not app._voice_inputs
            assert composer.text == "draft remains"
            body = list(app.query(".user").results(Turn))[-1].body
            assert isinstance(body, Static) and str(body.content) == "/quit"

    asyncio.run(scenario())


@pytest.mark.usefixtures("workspace_discovery_only")
@pytest.mark.parametrize("size", [(100, 32), (48, 18)])
def test_voice_settings_use_shared_cas_and_preserve_inheritance_and_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size: tuple[int, int]
) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            yield TextDoneEvent(text="unused")

        harness, _ = setup_harness(tmp_path, monkeypatch, script)
        app = NagentsApp(harness)
        async with app.run_test(size=size) as pilot:
            await idle(app, pilot)
            configure(harness, monkeypatch)
            document = harness.provider_store.agent_models.load("assistant")
            harness.provider_store.agent_models.patch_live(
                "assistant", {"prompt": "Keep this exact Voice prompt"}, expected=document.revision
            )
            voice = app._ensure_voice()
            before = await voice.snapshot()
            edits: list[VoiceEdits | None] = []
            modal = VoiceSettingsModal(before)
            await app.push_screen(modal, edits.append)
            await pilot.pause()
            assert modal.query_one("#voice-save").region.bottom <= size[1]
            enabled = modal.query_one("#voice-enabled", Switch)
            viewport = modal.query_one("#voice-settings-content").region
            assert viewport.y <= enabled.region.y and enabled.region.bottom <= viewport.bottom
            assert modal.focused is enabled
            modal.query_one("#voice-enabled", Switch).value = False
            assert await pilot.click("#voice-save")
            await pilot.pause()
            edit = edits[0]
            assert edit is not None and edit.changes["enabled"] is False
            assert "prompt" not in edit.changes and "voice" not in edit.changes
            await voice.save(edit.snapshot, edit.changes)
            with pytest.raises(HTTPException) as stale:
                await voice.save(edit.snapshot, edit.changes)
            assert stale.value.status_code == 409
            saved = harness.provider_store.agent_models.load("assistant")
            live = saved.preferences["live"]
            assert isinstance(live, dict) and live["prompt"] == "Keep this exact Voice prompt"
            current = await voice.snapshot()
            await voice.save(current, {}, reset=True)
            restored = harness.provider_store.agent_models.load("assistant")
            assert restored.preferences["live"] == {"prompt": "Keep this exact Voice prompt"}

    asyncio.run(scenario())


def test_demo_voice_is_explicitly_unavailable_without_accessing_devices(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        app = make_app(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            app.command("/voice start")
            assert "unavailable in offline demo" in str(app.query_one("#status", Static).content)
            assert app._voice is None and not app.busy
            assert any(command.name == "voice" for command in app._completion_commands())

    asyncio.run(scenario())


def test_normal_tui_import_does_not_load_optional_voice_dependencies() -> None:
    script = """
import sys
from nagents.tui import NagentsApp
blocked = ('sounddevice', 'fastapi', 'aiortc', 'av', 'nagents.tui.voice_runtime')
assert not any(name == item or name.startswith(item + '.') for name in sys.modules for item in blocked)
"""
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=HANG_GUARD, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
