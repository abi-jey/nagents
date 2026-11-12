"""Call-scoped voice requests and verified parent updates for the terminal host."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from typing import TYPE_CHECKING

from nagents._async import join_owned
from nagents.events import CompactionDoneEvent
from nagents.events import CompactionStartedEvent
from nagents.events import DoneEvent
from nagents.events import ErrorEvent
from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.harness.types import TaskCompleted
from nagents.harness.types import TaskStarted
from nagents.live.delegation import ClientDelegationRequest
from nagents.live.runtime import result_chunks
from nagents.web.live.bridge import delegation_prompt
from nagents.web.live.bridge import speech_data
from nagents.web.live.bridge import voice_display

from .voice import VoicePrompt

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable

    from nagents.harness.runtime.followups import RunTurn
    from nagents.harness.types import HarnessEvent
    from nagents.live.delegation import LiveAppend
    from nagents.live.delegation import LiveAppendKind


class TuiVoiceBridge:
    def __init__(
        self,
        session_id: str,
        voice_session_id: str,
        submit: Callable[[VoicePrompt], Awaitable[None]],
        report: Callable[[dict[str, object]], None],
        *,
        agent: str,
        provider: str,
        model: str,
    ) -> None:
        self.session_id, self.voice_session_id = session_id, voice_session_id
        self.submit, self.report = submit, report
        self.target = {"agent": agent or "Main assistant", "provider": provider, "model": model}
        self.ready: asyncio.Event | None = None
        self.needs_attention: Callable[[], bool] = lambda: False
        self._sink: LiveAppend | None = None
        self._worker: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[tuple[LiveAppendKind, str, str]] = asyncio.Queue(maxsize=128)
        self._seen: set[str] = set()
        self._submitted: set[str] = set()
        self._handled: set[str] = set()
        self._speech: set[str] = set()
        self._admission = asyncio.Lock()
        self._run_id = ""
        self._requests: dict[str, str] = {}
        self._tasks: dict[str, str] = {}
        self._origin = ""
        self._initial = ""
        self._first_turn = True
        self._texts: set[str] = set()
        self._text = ""
        self._compacting = False
        self._run_failed = False
        self.closed = False
        self.failed = False

    @property
    def updates(self) -> TuiVoiceBridge:
        return self

    def _report(self, identifier: str, status: str, text: str, run_id: str = "", **details: object) -> None:
        self.report(
            {
                "delegation_id": identifier,
                "voice_session_id": self.voice_session_id,
                "chat_session_id": self.session_id,
                "run_id": run_id,
                "status": status,
                "text": text,
                **self.target,
                **details,
            }
        )

    def observe(self, request: ClientDelegationRequest) -> None:
        if self.closed or request.identifier in self._seen:
            return
        if len(self._seen) >= 512:
            raise ValueError("Reconnect voice before sending more requests")
        self._seen.add(request.identifier)
        self._report(request.identifier, "queued", "Waiting for the assistant.", request_transcript=request.transcript)

    def observe_native(self, request: ClientDelegationRequest) -> None:
        self.observe(request)
        if not request.text and not self.closed:
            self._handled.add(request.identifier)
            self._report(request.identifier, "failed", "The native voice handoff had no request. Please repeat it.")

    async def handle_request(self, request: ClientDelegationRequest) -> str:
        async with self._admission:
            if self.closed or request.identifier in self._handled:
                return ""
            self.observe(request)
            self._handled.add(request.identifier)
            try:
                explicit = request
                if not request.text:
                    fragments, _ = speech_data(request.transcript)
                    fresh = [
                        part
                        for part in fragments
                        if part["speaker"] == "user"
                        and json.dumps([part["text"], part.get("start_ms"), part.get("end_ms")], ensure_ascii=False)
                        not in self._speech
                    ]
                    text = "".join(str(part["text"]) for part in fresh).strip()
                    if not text:
                        if any(part["speaker"] == "user" and str(part["text"]).strip() for part in fragments):
                            self._report(
                                request.identifier, "cancelled", "No new caller request; existing work continues."
                            )
                            self.append(
                                "thinking",
                                "This request is already with the assistant; wait for its updates.",
                                request.identifier,
                            )
                        else:
                            self._report(
                                request.identifier,
                                "failed",
                                "Caller speech was not available. Please repeat the request.",
                            )
                            self.append("commentary", "Could you repeat that request?", request.identifier)
                        return ""
                    explicit = ClientDelegationRequest(request.identifier, text, request.transcript, request.offset_ms)
                prompt = delegation_prompt(explicit)
                _, speech = voice_display(explicit.transcript, self._speech)
                await self.submit(VoicePrompt(prompt, request.identifier, self.session_id, self))
                self._submitted.add(request.identifier)
                self._speech = speech
                self.append(
                    "thinking", "The request is with the assistant; wait for its verified updates.", request.identifier
                )
            except Exception:
                self._report(
                    request.identifier,
                    "failed",
                    "The assistant could not accept this request. Check the terminal and repeat it.",
                )
                self.append(
                    "commentary",
                    "The request was not accepted. Please check the terminal before trying again.",
                    request.identifier,
                )
        return ""

    def attach(self, sink: LiveAppend) -> None:
        if self._sink is not None or self.closed:
            raise RuntimeError("A voice bridge attaches only once")
        self._sink = sink
        self._worker = asyncio.create_task(self._send(), name="ngn-tui-voice-updates")

    def append(self, kind: LiveAppendKind, text: str, identifier: str = "") -> None:
        if self.closed or self._sink is None or not text.strip():
            return
        try:
            self._queue.put_nowait((kind, text[:32000], identifier))
        except asyncio.QueueFull:
            self.failed = self.closed = True
            if self._worker is not None:
                self._worker.cancel()

    async def _send(self) -> None:
        assert self._sink is not None
        try:
            if self.ready is not None:
                await self.ready.wait()
            while not self.closed:
                kind, text, identifier = await self._queue.get()
                for part in result_chunks(text):
                    async with asyncio.timeout(15):
                        await self._sink(kind, part, identifier)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.failed = self.closed = True

    def begin(self, run_id: str, prompt: VoicePrompt | None = None) -> None:
        self._run_id = run_id
        self._initial = prompt.identifier if prompt is not None and prompt.bridge is self else ""
        self._first_turn = True
        self._origin = self._initial
        self._texts.clear()
        self._text = ""
        self._run_failed = False
        if self._initial:
            self._admitted(self._initial)

    def _admitted(self, identifier: str) -> None:
        self._requests[identifier] = self._run_id
        self._report(identifier, "working", "Processing…", self._run_id)

    def turn(self, marker: RunTurn) -> None:
        origin = (
            marker.identifier
            if marker.voice_session_id == self.voice_session_id and marker.identifier in self._submitted
            else ""
        )
        if self._first_turn and marker.kind == "human" and not marker.identifier:
            origin = self._initial
        if marker.kind == "notification":
            origins = {self._tasks.get(task, "") for task in marker.session_ids}
            if len(origins) == 1:
                origin = next(iter(origins))
        self._first_turn = False
        self._origin = origin
        self._texts.clear()
        self._text = ""
        if origin and origin not in self._requests:
            self._admitted(origin)

    def event(self, event: HarnessEvent) -> None:
        if isinstance(event, CompactionStartedEvent):
            self._compacting = True
        elif isinstance(event, CompactionDoneEvent):
            self._compacting = False
        elif self._compacting:
            return
        elif isinstance(event, (TextChunkEvent, TextDoneEvent, DoneEvent)):
            if event.extra.get("session_id") and event.extra["session_id"] != self.session_id:
                return
            if isinstance(event, TextChunkEvent):
                self._text = (self._text + event.chunk)[:32000]
            else:
                text = event.text if isinstance(event, TextDoneEvent) else event.final_text or self._text
                if text.strip() and text not in self._texts and (not isinstance(event, DoneEvent) or not self._texts):
                    self._texts.add(text)
                    self.append("commentary", text, self._origin)
        elif isinstance(event, ToolCallEvent):
            self.append(
                "thinking",
                f"The assistant is requesting tool {event.name[:128]}. This does not confirm success.",
                self._origin,
            )
        elif isinstance(event, TaskStarted):
            origin = self._tasks.get(event.parent_session_id, self._origin)
            self._tasks[event.session_id] = origin
            self.append("thinking", f"An assistant background task started: {event.title[:128]}.", origin)
        elif isinstance(event, TaskCompleted):
            self.append(
                "thinking",
                f"An assistant background task {event.status}; wait for the parent's verified result.",
                self._tasks.get(event.session_id, self._origin),
            )
        elif isinstance(event, ErrorEvent):
            self._run_failed |= not event.recoverable
            self.append(
                "thinking", "An assistant request reported an error. Check the terminal before retrying.", self._origin
            )

    def finish(self, run_id: str, *, cancelled: bool = False, failed: bool = False) -> None:
        status = "cancelled" if cancelled else "failed" if failed or self._run_failed else "completed"
        for identifier, owner in tuple(self._requests.items()):
            if owner == run_id:
                self._requests.pop(identifier)
                self._report(
                    identifier,
                    status,
                    "The assistant finished this request."
                    if status == "completed"
                    else "The assistant request was stopped. Check the terminal for actions already completed.",
                    run_id,
                )
        if status != "completed":
            self.append(
                "commentary",
                "The assistant request was stopped. Check the terminal for any actions already completed.",
                self._origin,
            )

    def discard(self, prompt: VoicePrompt) -> None:
        self._report(prompt.identifier, "cancelled", "The queued request was cleared before execution.")

    def approval(self, tool: str) -> None:
        self.append(
            "thinking",
            f"The assistant is waiting for an explicit {tool[:128]} approval in the terminal. Speech does not approve it.",
            self._origin,
        )

    async def close(self) -> None:
        self.closed = True
        if self._worker is not None:
            self._worker.cancel()
            with suppress(asyncio.CancelledError):
                await join_owned(self._worker)
        while not self._queue.empty():
            self._queue.get_nowait()
