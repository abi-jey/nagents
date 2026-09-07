"""Self-contained dictation modal. Returns edited text or None, never submits it."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING
from typing import ClassVar

from textual import on
from textual.binding import Binding
from textual.containers import Horizontal
from textual.containers import Vertical
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button
from textual.widgets import Static
from textual.widgets import TextArea

from nagents.harness.dictation import DictationError
from nagents.harness.dictation import VoiceDictation

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.binding import BindingType

    from nagents.harness.config import HarnessConfig


class DictationModal(ModalScreen[str | None]):
    """Push with a result callback that only inserts text into the composer.

    The modal owns its service (including an injected one) and closes it on every
    exit. Merely opening it never starts recording. Use ``service=`` for fake I/O
    in tests. The constructor takes a config, not a provider or chat credential.
    Hosts with priority Esc/Ctrl+C bindings must route them to
    ``action_cancel_dictation()``; await ``close()`` before host shutdown.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape,ctrl+c", "cancel_dictation", show=False, priority=True),
    ]
    DEFAULT_CSS = """
    DictationModal { align: center middle; background: $background 65%; }
    DictationModal #dictation-dialog {
        width: 76; max-width: 100%; height: 28; max-height: 100%;
        border: round $accent; background: $surface; padding: 1;
    }
    DictationModal #dictation-title { text-style: bold; color: $accent; height: 1; }
    DictationModal #dictation-details { height: 1fr; }
    DictationModal #dictation-destination { margin-bottom: 1; }
    DictationModal #dictation-status { height: auto; max-height: 5; overflow-y: auto; margin: 1 0; }
    DictationModal #dictation-preview { display: none; height: 1fr; min-height: 3; }
    DictationModal.preview #dictation-preview { display: block; }
    DictationModal.preview #dictation-hint { display: none; }
    DictationModal #dictation-actions { height: 3; }
    DictationModal #dictation-primary { width: 2fr; min-width: 0; }
    DictationModal #dictation-cancel { width: 1fr; min-width: 0; }
    """

    def __init__(self, config: HarnessConfig, *, service: VoiceDictation | None = None) -> None:
        super().__init__()
        self.service = service if service is not None else VoiceDictation(config)
        self._phase = "idle"
        self._audio = b""
        self._upload_requested = False
        self._leaving = False
        self._job: asyncio.Task[None] | None = None
        self._preview = TextArea(id="dictation-preview", soft_wrap=True, show_line_numbers=False)

    def compose(self) -> ComposeResult:
        with Vertical(id="dictation-dialog"):
            yield Static("VOICE DICTATION / DRAFT ONLY", id="dictation-title", markup=False)
            with VerticalScroll(id="dictation-details"):
                warning = (
                    "\nWARNING: HTTP sends audio and API key without TLS."
                    if self.service.endpoint.startswith("http:")
                    else ""
                )
                yield Static(
                    f"Audio destination:\n{self.service.endpoint}{warning}", id="dictation-destination", markup=False
                )
                yield Static(
                    f"Up to {self.service.max_seconds}s. Start explicitly enables your microphone. "
                    "Stop & transcribe uploads once to the destination above using a separate API key. "
                    "API billing applies, not ChatGPT subscription access.\n\n"
                    "Audio stays in memory, never a local file. At the time limit, capture stops without uploading. "
                    "Review and edit the transcript before adding it to your draft. Nothing is auto-sent. "
                    "Cancel discards local audio/text but cannot recall an upload already received by the server.",
                    id="dictation-hint",
                    markup=False,
                )
            yield Static("Ready. Microphone is off.", id="dictation-status", markup=False)
            yield self._preview
            with Horizontal(id="dictation-actions"):
                yield Button("Start recording", id="dictation-primary", variant="primary")
                yield Button("Cancel", id="dictation-cancel")

    def on_mount(self) -> None:
        self.query_one("#dictation-cancel", Button).focus()
        try:
            self.service.check_ready()
        except DictationError as exc:
            self.query_one("#dictation-status", Static).update(str(exc))
        self.set_interval(0.1, self._tick)

    def _tick(self) -> None:
        if self._phase == "capturing" and not self._leaving and self.service.capturing:
            self.query_one("#dictation-status", Static).update(
                f"CAPTURING {self.service.elapsed:.0f}/{self.service.max_seconds}s. Microphone is on."
            )

    @on(TextArea.Changed, "#dictation-preview")
    def preview_changed(self, event: TextArea.Changed) -> None:
        event.stop()
        if self._phase == "preview" and not self._leaving:
            self.query_one("#dictation-primary", Button).disabled = not bool(self._preview.text.strip())

    @on(Button.Pressed)
    async def pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "dictation-cancel":
            await self.action_cancel_dictation()
        elif event.button.id == "dictation-primary" and not self._leaving:
            primary = self.query_one("#dictation-primary", Button)
            if self._phase == "preview":
                text = self._preview.text.strip()
                if text:
                    self._leaving = True
                    await self.service.close()
                    self._preview.load_text("")
                    self.dismiss(text)
            elif self._phase == "capturing":
                self._upload_requested = True
                self._phase = "stopping"
                primary.disabled = True
                self.query_one("#dictation-status", Static).update("Stopping microphone before upload...")
                self.service.stop()
            elif self._phase in {"idle", "recorded"}:
                self._job = asyncio.create_task(self._run())
                # Change phase before yielding so repeated clicks cannot start two jobs.
                self._phase = "transcribing" if self._audio else "capturing"
                primary.label = "Stop & transcribe"
                primary.disabled = bool(self._audio)
                self.query_one("#dictation-status", Static).update(
                    "Starting microphone..." if not self._audio else "TRANSCRIBING. Microphone is off."
                )

    async def _run(self) -> None:
        try:
            if not self._audio:
                self._audio = await self.service.capture()
                if self._leaving:
                    return
                if not self._upload_requested:
                    self._phase = "recorded"
                    self.query_one("#dictation-status", Static).update(
                        "Capture stopped at the limit. Microphone is off. Transcribe or cancel."
                    )
                    primary = self.query_one("#dictation-primary", Button)
                    primary.label = "Transcribe"
                    primary.disabled = False
                    return
            self._phase = "transcribing"
            self.query_one("#dictation-status", Static).update(
                "TRANSCRIBING. Microphone is off. Cancel stops waiting, not server receipt."
            )
            primary = self.query_one("#dictation-primary", Button)
            primary.disabled = True
            text = await self.service.transcribe(self._audio)
            if not self._leaving:
                self._phase = "preview"
                self.add_class("preview")
                self.query_one("#dictation-status", Static).update(
                    "Review and edit. Use text adds to the composer, never sends."
                )
                self._preview.load_text(text)
                self._preview.focus()
                primary.label = "Use text"
                primary.disabled = False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._leaving:
                self._phase = "idle"
                message = (
                    str(exc)
                    if isinstance(exc, DictationError)
                    else "Dictation failed. Check microphone permission and API setup, then start again."
                )
                self.query_one("#dictation-status", Static).update(message)
                primary = self.query_one("#dictation-primary", Button)
                primary.label = "Start recording"
                primary.disabled = False
        finally:
            # Only retain audio while awaiting the explicit upload decision at the cap.
            if self._phase != "recorded" or self._leaving:
                self._audio = b""
            self._upload_requested = False

    async def close(self) -> None:
        """Discard transient data and await I/O cleanup without dismissing the screen."""
        self._leaving = True
        self._audio = b""
        try:
            if self._job is not None:
                self._job.cancel()
                with suppress(asyncio.CancelledError):
                    await self._job
        finally:
            try:
                await self.service.close()
            finally:
                if self.is_mounted and self in self.app.screen_stack:
                    self._preview.load_text("")
                else:
                    # load_text moves the cursor and touches the active screen,
                    # which may already be gone during external dismiss/shutdown.
                    self._preview.history.clear()
                    self._preview.document.replace_range((0, 0), self._preview.document.end, "")

    async def action_cancel_dictation(self) -> None:
        if self._leaving:
            return
        self.query_one("#dictation-status", Static).update("Cancelling; waiting for microphone and network cleanup...")
        self.query_one("#dictation-primary", Button).disabled = True
        self.query_one("#dictation-cancel", Button).disabled = True
        await self.close()
        self.dismiss(None)

    async def on_unmount(self) -> None:
        # Covers external dismiss/pop_screen and application shutdown, not just Cancel.
        await self.close()
