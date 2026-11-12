"""Voice setup cancellation, reconnect, and ordinary approval boundaries."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from typing import cast

import pytest

from nagents.events import TextDoneEvent
from nagents.harness.types import ApprovalRequest
from nagents.live.delegation import ClientDelegationRequest
from nagents.tui import NagentsApp
from nagents.tui import voice_runtime
from nagents.tui.screens import ApprovalModal
from nagents.tui.voice_bridge import TuiVoiceBridge
from nagents.web.live import runtime
from nagents.web.live.runtime import LiveService
from tests.support.hang_guard import HANG_GUARD
from tests.support.providers import setup_harness
from tests.support.tui import idle
from tests.tui.test_voice import FakeLocalAudio
from tests.tui.test_voice import configure
from tests.tui.test_voice import until
from tests.tui.test_voice_service import NativeAgent

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents import Agent
    from nagents.events import Event
    from nagents.live.delegation import LiveAppendKind
    from nagents.types import Message
    from tests.support.providers import FakeProvider


@pytest.mark.usefixtures("workspace_discovery_only")
@pytest.mark.parametrize("phase", ["settings", "devices", "attach"])
def test_end_during_setup_joins_resources_updates_panel_and_allows_reconnect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    async def scenario() -> None:
        entered, release = asyncio.Event(), asyncio.Event()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            yield TextDoneEvent(text="unused")

        class Audio(FakeLocalAudio):
            async def open(self) -> None:
                if phase == "devices" and not release.is_set():
                    entered.set()
                    await release.wait()

        monkeypatch.setattr(voice_runtime, "LocalAudio", Audio)
        monkeypatch.setattr(runtime, "FINALIZE_SECONDS", 0.01)
        harness, _ = setup_harness(tmp_path, monkeypatch, script)
        app = NagentsApp(harness)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            configure(harness, monkeypatch)
            voice = app._ensure_voice()
            agents: list[NativeAgent] = []

            def factory(name: str) -> Agent:
                agent = NativeAgent("marin")
                if phase == "attach" and not agents:
                    agent.attach.clear()
                    entered.set()
                agents.append(agent)
                return cast("Agent", agent)

            voice.service = LiveService(factory)
            original_load = voice.settings.load

            async def load() -> None:
                if phase == "settings" and not release.is_set():
                    entered.set()
                    await release.wait()
                await original_load()

            monkeypatch.setattr(voice.settings, "load", load)
            starting = asyncio.create_task(voice.start())
            await asyncio.wait_for(entered.wait(), HANG_GUARD)
            await voice.stop()
            with pytest.raises(asyncio.CancelledError):
                await starting
            assert not voice.active and not voice.settings._busy
            assert voice.payload["status"] in {"closed", "error"}
            if voice.audio is not None:
                assert voice.audio.input.closed and voice.audio.output.closed
            release.set()
            await voice.start()
            assert voice.active and voice.payload["status"] == "connected"
            await voice.stop()
            assert not voice.active and all(agent.closed == 1 for agent in agents)

    asyncio.run(scenario())


@pytest.mark.usefixtures("workspace_discovery_only")
@pytest.mark.parametrize("delayed", [False, True])
def test_spoken_yes_does_not_approve_terminal_permission_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, delayed: bool
) -> None:
    async def scenario() -> None:
        decisions: list[bool] = []
        approval_update = asyncio.Event()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if len(provider.requests) == 1:
                allowed = await harness.approval_handler(
                    ApprovalRequest("guarded", "example", "Review this action", {}, mandatory=True)
                )
                decisions.append(allowed)
                yield TextDoneEvent(text="The action was denied." if not allowed else "Approved")
            else:
                assert messages[-1].content == "yes"
                yield TextDoneEvent(text="That was another message, not an approval.")

        harness, _ = setup_harness(tmp_path, monkeypatch, script)
        app = NagentsApp(harness)
        sent: list[tuple[str, str, str]] = []

        async def sink(kind: LiveAppendKind, text: str, identifier: str) -> str:
            if delayed and "Speech does not approve" in text:
                await approval_update.wait()
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
            await bridge.handle_request(ClientDelegationRequest("action", "Request an action", "[]"))
            await until(lambda: isinstance(app.screen, ApprovalModal))
            # Mounting the dialog and delivering the queued voice notice have
            # independent owners. Do not assume a scheduler/timer ordering.
            approval_update.set()
            await until(lambda: any("Speech does not approve" in text for _, text, _ in sent))
            await bridge.handle_request(ClientDelegationRequest("yes", "yes", "[]"))
            assert not decisions and isinstance(app.screen, ApprovalModal)
            assert any("Speech does not approve" in text for _, text, _ in sent)
            await pilot.press("escape")
            await idle(app, pilot)
            assert decisions == [False]
            assert any(message.content == "yes" for message in await harness.history())

    asyncio.run(scenario())


@pytest.mark.usefixtures("workspace_discovery_only")
def test_accepted_request_keeps_its_reporter_after_many_voice_reconnects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        blocked, release = asyncio.Event(), asyncio.Event()
        reports: list[dict[str, object]] = []

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if len(provider.requests) == 1:
                blocked.set()
                await release.wait()
            yield TextDoneEvent(text="Verified answer")

        async def sink(kind: LiveAppendKind, text: str, identifier: str) -> str:
            return "session." + kind + ".append"

        harness, _ = setup_harness(tmp_path, monkeypatch, script)
        app = NagentsApp(harness)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            voice = app._ensure_voice()
            old = TuiVoiceBridge(
                harness.session_id,
                "original-call",
                app._accept_voice,
                reports.append,
                agent="assistant",
                provider="text",
                model="normal",
            )
            voice.bridges.append(old)
            old.attach(sink)
            app._launch(lambda: app._send("first"), "Working")
            await asyncio.wait_for(blocked.wait(), HANG_GUARD)
            await old.handle_request(ClientDelegationRequest("accepted", "Continue this request", "[]"))
            await old.close()
            for index in range(8):
                voice.bridges.append(
                    TuiVoiceBridge(
                        harness.session_id,
                        f"reconnected-{index}",
                        app._accept_voice,
                        lambda report: None,
                        agent="assistant",
                        provider="text",
                        model="normal",
                    )
                )
            assert old not in voice.bridges
            release.set()
            await idle(app, pilot)
            assert [report["status"] for report in reports] == ["queued", "working", "completed"]
            assert all(report["voice_session_id"] == "original-call" for report in reports)
            assert any(message.content == "Continue this request" for message in await harness.history())
            assert not app._voice_run_bridges

    asyncio.run(scenario())
