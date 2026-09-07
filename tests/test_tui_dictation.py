"""Self-contained modal tests; no coding agent, hardware, or network is started."""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING
from typing import ClassVar

import aiohttp
import pytest
from textual.app import App
from textual.binding import Binding
from textual.widgets import Button
from textual.widgets import Static
from textual.widgets import TextArea

from nagents.harness.config import HarnessConfig
from nagents.harness.dictation import DictationError
from nagents.harness.dictation import VoiceDictation
from nagents.tui.dictation import DictationModal
from nagents.tui.widgets import Composer

from .test_dictation import FakeMicrophone
from .test_dictation import FakeNetwork
from .test_dictation import wait_until
from .test_dictation import wav_bytes
from .test_tui import FakeHarness
from .test_tui import idle
from .test_tui import make_app

if TYPE_CHECKING:
    from pathlib import Path

    from textual.app import ComposeResult
    from textual.binding import BindingType


class FakeVoice(VoiceDictation):
    def __init__(self, config: HarnessConfig) -> None:
        super().__init__(config)
        self.capture_calls = 0
        self.stop_calls = 0
        self.uploads: list[bytes] = []
        self.record_release = asyncio.Event()
        self.upload_release = asyncio.Event()
        self.cleanup_release = asyncio.Event()
        self.cleanup_release.set()
        self.cancelled = asyncio.Event()
        self.closed = False
        self.failure: Exception | None = None

    def check_ready(self) -> None:
        pass

    async def capture(self) -> bytes:
        self.capture_calls += 1
        self._capturing.set()
        try:
            await self.record_release.wait()
            if self.failure:
                raise self.failure
            return wav_bytes()
        except asyncio.CancelledError:
            self.cancelled.set()
            await self.cleanup_release.wait()
            raise
        finally:
            self._capturing.clear()

    def stop(self) -> None:
        self.stop_calls += 1
        self.record_release.set()

    async def transcribe(self, audio: bytes) -> str:
        self.uploads.append(audio)
        try:
            await self.upload_release.wait()
            if self.failure:
                raise self.failure
            return "Synthetic dictated draft."
        except asyncio.CancelledError:
            self.cancelled.set()
            await self.cleanup_release.wait()
            raise

    async def close(self) -> None:
        self.closed = True


class DictationApp(App[None]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+q", "quit", priority=True),
        Binding("escape,ctrl+c", "cancel_voice", priority=True),
    ]

    def __init__(self, config: HarnessConfig, voice: VoiceDictation) -> None:
        super().__init__()
        self.modal = DictationModal(config, service=voice)
        self.results: list[str | None] = []

    def compose(self) -> ComposeResult:
        yield TextArea("Existing draft. ", id="composer")

    def on_mount(self) -> None:
        self.push_screen(self.modal, self.received)

    def received(self, text: str | None) -> None:
        self.results.append(text)
        if text is not None:
            composer = self.query_one("#composer", TextArea)
            composer.load_text(composer.text + text)

    def action_cancel_voice(self) -> None:
        # NagentsApp's priority bindings need the same routing at integration.
        if isinstance(self.screen, DictationModal):
            self.screen.run_worker(self.screen.action_cancel_dictation())


def make_modal_app(tmp_path: Path) -> tuple[DictationApp, FakeVoice]:
    config = HarnessConfig(workspace=tmp_path, dictation_enabled=True)
    voice = FakeVoice(config)
    return DictationApp(config, voice), voice


@pytest.mark.parametrize("size", [(100, 35), (60, 20), (40, 16)])
def test_explicit_start_stop_preview_edit_accept(tmp_path: Path, size: tuple[int, int]) -> None:
    async def scenario() -> None:
        app, voice = make_modal_app(tmp_path)
        async with app.run_test(size=size) as pilot:
            modal = app.modal
            assert voice.capture_calls == 0
            assert not voice.uploads
            assert modal.focused is modal.query_one("#dictation-cancel", Button)
            assert voice.endpoint in str(modal.query_one("#dictation-destination", Static).content)
            assert modal.query_one("#dictation-cancel").region.bottom <= size[1]
            assert modal.query_one("#dictation-primary").region.x >= 0
            await pilot.click("#dictation-primary")
            await wait_until(lambda: voice.capturing)
            modal._tick()
            assert "CAPTURING" in str(modal.query_one("#dictation-status", Static).content)
            assert not voice.uploads
            await pilot.pause(0.3)
            await pilot.click("#dictation-primary")
            await wait_until(lambda: bool(voice.uploads))
            assert not voice.capturing
            assert voice.stop_calls == 1
            assert "TRANSCRIBING" in str(modal.query_one("#dictation-status", Static).content)
            assert modal.query_one("#dictation-primary", Button).disabled
            voice.upload_release.set()
            await wait_until(lambda: modal._phase == "preview")
            await pilot.pause()
            preview = modal.query_one("#dictation-preview", TextArea)
            assert preview.text == "Synthetic dictated draft."
            assert app.results == []
            assert not modal._audio
            assert preview.region.height >= 3
            preview.load_text("Edited transcript.")
            await pilot.press("enter")
            assert app.results == []
            preview.load_text("Edited transcript.")
            await pilot.pause(0.3)
            await pilot.click("#dictation-primary")
            await pilot.pause()
            assert app.results == ["Edited transcript."]
            assert app.query_one("#composer", TextArea).text == "Existing draft. Edited transcript."
            assert voice.closed
            assert preview.text == ""

    asyncio.run(scenario())


def test_time_cap_waits_for_explicit_upload(tmp_path: Path) -> None:
    async def scenario() -> None:
        app, voice = make_modal_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.click("#dictation-primary")
            voice.record_release.set()
            await wait_until(lambda: app.modal._phase == "recorded")
            assert not voice.uploads
            assert not voice.capturing
            assert app.modal._audio
            await pilot.pause(0.3)
            await pilot.click("#dictation-primary")
            await wait_until(lambda: bool(voice.uploads))
            assert voice.capture_calls == 1
            assert len(voice.uploads) == 1
            await pilot.press("escape")
            assert not app.modal._audio
            assert app.results == [None]

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["idle", "capturing", "transcribing", "preview", "recorded"])
@pytest.mark.parametrize("cancel", ["escape", "ctrl+c", "button", "dismiss", "quit"])
def test_cancellation_and_shutdown_never_return_drafts(tmp_path: Path, phase: str, cancel: str) -> None:
    async def scenario() -> None:
        app, voice = make_modal_app(tmp_path)
        async with app.run_test() as pilot:
            if phase != "idle":
                await pilot.click("#dictation-primary")
                await wait_until(lambda: voice.capturing)
            if phase == "recorded":
                voice.record_release.set()
                await wait_until(lambda: app.modal._phase == "recorded")
            if phase in {"transcribing", "preview"}:
                await pilot.pause(0.3)
                await pilot.click("#dictation-primary")
                await wait_until(lambda: bool(voice.uploads))
            if phase == "preview":
                voice.upload_release.set()
                await wait_until(lambda: app.modal._phase == "preview")
            if cancel == "button":
                await pilot.click("#dictation-cancel")
            elif cancel == "dismiss":
                app.modal.dismiss()
                await pilot.pause()
            else:
                await pilot.press("ctrl+q" if cancel == "quit" else cancel)
            if cancel != "quit":
                assert app.results == [None]
                assert app.query_one("#composer", TextArea).text == "Existing draft. "
        assert voice.closed
        assert not voice.capturing
        assert not app.modal._audio
        assert not app.modal._preview.text
        assert all(value is None for value in app.results)
        if phase in {"capturing", "transcribing"}:
            assert voice.cancelled.is_set()

    asyncio.run(scenario())


def test_cleanup_without_screen_stack_clears_preview_and_closes_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        app, voice = make_modal_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            app.modal._preview.load_text("Synthetic private transcript")

            def forbidden() -> None:
                pytest.fail("Cleanup must not touch selection after the screen stack is removed")

            with monkeypatch.context() as patch:
                patch.setattr(type(app), "screen_stack", property(lambda self: []))
                patch.setattr(app, "clear_selection", forbidden)
                await app.modal.close()
            assert voice.closed
            assert not app.modal._preview.text
            assert not app.modal._audio

    asyncio.run(scenario())


def test_real_service_modal_flow_uses_only_fake_io(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    microphone = FakeMicrophone()
    network = FakeNetwork()
    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(RawInputStream=microphone))
    monkeypatch.setitem(sys.modules, "numpy", None)
    monkeypatch.setattr(aiohttp, "ClientSession", network)
    monkeypatch.setenv("VOICE_TEST_KEY", "synthetic-voice-key")

    async def scenario() -> None:
        config = HarnessConfig(workspace=tmp_path, dictation_enabled=True, dictation_api_key_env="VOICE_TEST_KEY")
        service = VoiceDictation(config)
        app = DictationApp(config, service)
        async with app.run_test() as pilot:
            assert not microphone.calls
            assert not network.requests
            await pilot.click("#dictation-primary")
            await wait_until(lambda: service.capturing)
            await pilot.pause(0.3)
            await pilot.click("#dictation-primary")
            await wait_until(lambda: app.modal._phase == "preview")
            assert microphone.closed.is_set()
            assert len(network.requests) == 1
            assert network.closed
            assert app.results == []
            await pilot.pause(0.3)
            await pilot.click("#dictation-primary")
            await pilot.pause()
            assert app.results == ["A synthetic draft."]

    asyncio.run(scenario())


@pytest.mark.parametrize("enabled", [False, True])
def test_real_service_readiness_error_never_opens_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    microphone = FakeMicrophone()
    monkeypatch.setitem(sys.modules, "sounddevice", SimpleNamespace(RawInputStream=microphone))
    monkeypatch.delenv("VOICE_TEST_KEY", raising=False)

    async def scenario() -> None:
        config = HarnessConfig(workspace=tmp_path, dictation_enabled=enabled, dictation_api_key_env="VOICE_TEST_KEY")
        app = DictationApp(config, VoiceDictation(config))
        async with app.run_test(size=(40, 16)) as pilot:
            message = str(app.modal.query_one("#dictation-status", Static).content)
            assert "VOICE_TEST_KEY" in message if enabled else "disabled" in message
            assert app.modal.query_one("#dictation-cancel").region.bottom <= 16
            await pilot.click("#dictation-primary")
            await pilot.pause()
            assert not microphone.calls
            await pilot.press("escape")
            assert app.results == [None]

    asyncio.run(scenario())


def test_blank_preview_cannot_be_accepted(tmp_path: Path) -> None:
    async def scenario() -> None:
        app, voice = make_modal_app(tmp_path)
        voice.upload_release.set()
        async with app.run_test() as pilot:
            await pilot.click("#dictation-primary")
            await pilot.pause(0.3)
            await pilot.click("#dictation-primary")
            await wait_until(lambda: app.modal._phase == "preview")
            app.modal._preview.load_text(" \n ")
            await pilot.pause()
            assert app.modal.query_one("#dictation-primary", Button).disabled
            assert not app.results
            await pilot.press("escape")
            assert app.results == [None]

    asyncio.run(scenario())


@pytest.mark.parametrize("finish", ["accept", "escape", "ctrl+c", "quit"])
def test_nagents_host_integration_preserves_draft_and_never_sends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, finish: str
) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        app = make_app(backend)
        voice = FakeVoice(backend.config)
        voice.upload_release.set()
        modal = DictationModal(backend.config, service=voice)
        results: list[str | None] = []

        def receive(text: str | None) -> None:
            results.append(text)
            if text:
                app.query_one(Composer).insert(text)

        def cancel_voice() -> None:
            if isinstance(app.screen, DictationModal):
                app.screen.run_worker(app.screen.action_cancel_dictation())

        # Exercise the integration hooks without changing the host's source file.
        monkeypatch.setattr(app, "action_cancel", cancel_voice)
        monkeypatch.setattr(app, "action_interrupt", cancel_voice)
        async with app.run_test(size=(60, 20)) as pilot:
            await idle(app, pilot)
            composer = app.query_one(Composer)
            composer.load_text("Kept draft. ")
            composer.move_cursor((0, len(composer.text)))
            await app.push_screen(modal, receive)
            assert not voice.capture_calls
            await pilot.click("#dictation-primary")
            await wait_until(lambda: voice.capturing)
            if finish == "accept":
                await pilot.pause(0.3)
                await pilot.click("#dictation-primary")
                await wait_until(lambda: modal._phase == "preview")
                await pilot.press("enter")
                assert not backend.prompts
                await pilot.pause(0.3)
                await pilot.click("#dictation-primary")
                await pilot.pause()
                assert results == ["Synthetic dictated draft."]
                assert composer.text == "Kept draft. Synthetic dictated draft."
            else:
                await pilot.press("ctrl+q" if finish == "quit" else finish)
                await pilot.pause()
                assert composer.text == "Kept draft. "
                assert not any(results)
            assert not backend.prompts
            assert not composer.prompt_history
        assert voice.closed
        assert not voice.capturing
        assert backend.closed

    asyncio.run(scenario())


def test_cancel_keeps_modal_until_cleanup_finishes(tmp_path: Path) -> None:
    async def scenario() -> None:
        app, voice = make_modal_app(tmp_path)
        async with app.run_test() as pilot:
            await pilot.click("#dictation-primary")
            await wait_until(lambda: voice.capturing)
            voice.cleanup_release.clear()
            cancelling = asyncio.create_task(app.modal.action_cancel_dictation())
            await voice.cancelled.wait()
            assert app.screen is app.modal
            assert not voice.closed
            assert app.results == []
            voice.cleanup_release.set()
            await cancelling
            await pilot.pause()
            assert voice.closed
            assert app.results == [None]

    asyncio.run(scenario())


@pytest.mark.parametrize("known", [True, False])
def test_errors_are_actionable_and_no_exception_body_is_rendered(
    tmp_path: Path, known: bool, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario() -> None:
        app, voice = make_modal_app(tmp_path)
        voice.failure = DictationError("Install the voice extra.") if known else RuntimeError("private-response-body")
        voice.record_release.set()
        async with app.run_test() as pilot:
            await pilot.click("#dictation-primary")
            await wait_until(lambda: app.modal._phase == "idle")
            text = str(app.modal.query_one("#dictation-status", Static).content)
            assert "voice extra" in text if known else "API setup" in text
            assert "private-response-body" not in app.export_screenshot() + caplog.text
            assert not app.modal.query_one("#dictation-primary", Button).disabled
            assert not app.modal._audio
            assert not voice.uploads
            await pilot.press("escape")

    asyncio.run(scenario())
