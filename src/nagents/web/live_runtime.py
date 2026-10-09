"""Bounded, server-owned Live calls and browser audio relays.

The server owns delegation, provider media, and finalization. Web voice uses
the shared provider registry and relays PCM over the application's WebSocket.
Only normalized captions and application status survive a completed call.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import deque
from contextlib import aclosing
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from typing import TYPE_CHECKING
from typing import Literal
from typing import cast
from uuid import uuid4

from fastapi import HTTPException
from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from nagents.audio import AudioDuplex
from nagents.audio import AudioFormat
from nagents.events import AudioTranscriptDeltaEvent
from nagents.events import ErrorEvent
from nagents.events import InputTranscriptDeltaEvent
from nagents.live import LiveAPI
from nagents.live import LiveEvent

from ._async import join_owned
from .live_context import LiveSeed
from .live_inspection import MAX_MODEL_BYTES
from .live_inspection import MAX_MODEL_PAYLOAD
from .live_inspection import MAX_MODEL_REQUESTS
from .live_inspection import seed_details
from .live_inspection import text_preview
from .live_login import ChatGPTLiveConnection
from .live_login import LoginVoiceError
from .live_output import BrowserOutput as _BrowserOutput

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from collections.abc import Awaitable
    from collections.abc import Callable

    from nagents import Agent
    from nagents.events import Event

    from .live_login import LoginVoiceConfig
    from .live_relay import ChatGPTMediaRelay

MAX_SDP_BYTES = 65536
MAX_AUDIO_FRAME = 9600  # 100 ms of mono PCM16 at 24 kHz.
SILENCE_FRAME = bytes(960)
MAX_EVENTS = 256
MAX_TEXT_CHARACTERS = 4096
MAX_COMPLETED = 8
MAX_COMPLETED_DELEGATIONS = 32
MAX_DELEGATION_REQUEST = 32768
MAX_DELEGATION_RESULT = 16384
MAX_DELEGATION_TIMELINE = 96
LEASE_SECONDS = 40.0
PROVISION_SECONDS = 25.0
ATTACH_SECONDS = 10.0
FINALIZE_SECONDS = 10.0
CLEANUP_SECONDS = 5.0

Status = Literal["connecting", "connected", "closing", "closed", "error"]
Payload = dict[str, object]
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z")
_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_DELEGATION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_CHAT_ID = re.compile(r"ngn-[A-Za-z0-9-]{1,76}\Z")
_DELEGATION_STATES = {"queued", "working", "completed", "failed", "cancelled"}
_DELEGATION_TERMINAL = {"completed", "failed", "cancelled"}


def _bounded_text(text: str, limit: int) -> Payload:
    return {"text": text[:limit], "truncated": len(text) > limit, "characters": len(text)}


@dataclass
class _DelegationDetails:
    request: dict[str, Payload] = field(default_factory=dict)
    result: Payload = field(default_factory=dict)
    timeline: deque[Payload] = field(default_factory=lambda: deque(maxlen=MAX_DELEGATION_TIMELINE))
    timeline_truncated: bool = False
    model_requests: list[Payload] = field(default_factory=list)
    model_requests_truncated: bool = False
    model_bytes: int = 0

    def capture_model(self, value: object, seq: int, status: str) -> Payload:
        if status != "working" or not isinstance(value, dict):
            return {}
        kind, identifier, round_number = value.get("type"), value.get("model_call_id"), value.get("round")
        payload = value.get("payload")
        if (
            not isinstance(kind, str)
            or kind not in {"model_context", "http_request_body"}
            or not isinstance(identifier, str)
            or not re.fullmatch(r"[0-9a-f]{32}", identifier)
            or type(round_number) is not int
            or not 0 <= round_number <= 1_000_000
        ):
            return {}
        metadata: Payload = {"type": kind, "model_call_id": identifier, "round": round_number}
        if kind == "http_request_body":
            attempt_id = value.get("attempt_id")
            if not isinstance(attempt_id, str) or not re.fullmatch(r"[0-9a-f]{32}", attempt_id):
                return {}
            metadata.update(attempt_id=attempt_id, segmented=True)
        if value.get("capture_limited") is True:
            self.model_requests_truncated = True
            return {**metadata, "capture_limited": True}
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("text"), str)
            or type(payload.get("characters")) is not int
            or payload["characters"] < len(payload["text"])
            or type(payload.get("truncated")) is not bool
        ):
            return {}
        if len(self.model_requests) >= MAX_MODEL_REQUESTS or self.model_bytes >= MAX_MODEL_BYTES:
            self.model_requests_truncated = True
            return {**metadata, "capture_limited": True}
        preview = text_preview(payload["text"], min(MAX_MODEL_PAYLOAD, MAX_MODEL_BYTES - self.model_bytes))
        preview["characters"] = payload["characters"]
        preview["truncated"] = bool(preview["truncated"]) or payload["truncated"]
        if payload.get("characters_complete") is False:
            preview["characters_complete"] = False
        self.model_bytes += len(str(preview["text"]).encode("utf-8"))
        self.model_requests.append({"seq": seq, **metadata, "payload": preview})
        return metadata

    def capture(self, update: Payload, status: str, explanation: str) -> bool:
        changed = False
        for field_name, wire_name in (("transcript", "request_transcript"), ("input", "request_input")):
            value = update.get(wire_name)
            if (
                field_name not in self.request
                and isinstance(value, str)
                and (field_name == "transcript" or status in {"queued", "working"})
            ):
                self.request[field_name] = _bounded_text(value, MAX_DELEGATION_REQUEST)
                changed = True
        output = update.get("result_text")
        if status == "completed" and isinstance(output, str):
            self.result = {"kind": "assistant_output", **_bounded_text(output, MAX_DELEGATION_RESULT)}
            changed = True
        elif status in {"failed", "cancelled"}:
            self.result = {"kind": "terminal_explanation", **_bounded_text(explanation, MAX_DELEGATION_RESULT)}
            changed = True
        return changed

    def remember(self, event: Payload) -> None:
        self.timeline_truncated |= len(self.timeline) == MAX_DELEGATION_TIMELINE
        self.timeline.append(dict(event))

    def snapshot(self) -> Payload:
        return {
            "source": "app_callback",
            "request": {name: dict(value) for name, value in self.request.items()},
            **({"result": dict(self.result)} if self.result else {}),
            "timeline": [dict(event) for event in self.timeline],
            "timeline_truncated": self.timeline_truncated,
            "model_requests": deepcopy(self.model_requests),
            "model_requests_truncated": self.model_requests_truncated,
        }


@dataclass
class _Record:
    identifier: str = field(default_factory=lambda: uuid4().hex)
    model: str = ""
    voice: str = ""
    status: Status = "connecting"
    message: str = "Connecting to Live."
    events: deque[Payload] = field(default_factory=lambda: deque(maxlen=MAX_EVENTS))
    cursor: int = 0
    caption_keys: deque[str] = field(default_factory=lambda: deque(maxlen=MAX_EVENTS), repr=False)
    delegations: dict[str, Payload] = field(default_factory=dict)
    delegation_details: dict[str, _DelegationDetails] = field(default_factory=dict, repr=False)
    context: Payload = field(default_factory=dict)
    context_details: Payload = field(default_factory=dict, repr=False)

    def append(self, kind: str, text: str, **fields: object) -> None:
        self.cursor += 1
        self.events.append({"seq": self.cursor, "type": kind, "text": text, **fields})

    def transition(self, status: Status, message: str) -> None:
        self.status, self.message = status, message
        self.append("status", message)

    def delegation(self, update: Payload) -> None:
        """Accept app-owned task state without conflating task and voice closure."""
        identifier, status = update.get("delegation_id"), update.get("status")
        run_id, root = update.get("run_id", ""), update.get("chat_session_id")
        if (
            not isinstance(identifier, str)
            or not _DELEGATION_ID.fullmatch(identifier)
            or not isinstance(status, str)
            or status not in _DELEGATION_STATES
            or not isinstance(run_id, str)
            or (run_id and not _DELEGATION_ID.fullmatch(run_id))
            or (status in {"working", "completed"} and not run_id)
            or not isinstance(root, str)
            or not _CHAT_ID.fullmatch(root)
            or update.get("voice_session_id") != self.identifier
        ):
            return
        previous = self.delegations.get(identifier)
        if previous is None:
            if status != "queued" or run_id:
                return
        elif (
            previous["status"] in _DELEGATION_TERMINAL
            or previous["chat_session_id"] != root
            or (previous["run_id"] and previous["run_id"] != run_id)
            or (previous["status"] == "working" and status == "queued")
            or (previous["status"] == "queued" and status in _DELEGATION_TERMINAL and run_id)
            or (status == "completed" and previous["status"] != "working")
        ):
            return
        metadata: dict[str, str] = {}
        for key in ("agent", "provider", "model", "text"):
            value = update.get(key)
            if not isinstance(value, str):
                return
            metadata[key] = " ".join(_CONTROLS.sub("", value).split())[: 256 if key == "text" else 128]
        if not metadata["agent"] or not metadata["text"]:
            return
        fields: Payload = {
            **metadata,
            "delegation_id": identifier,
            "status": status,
            "voice_session_id": self.identifier,
            "chat_session_id": root,
            "run_id": run_id,
            "has_details": True,
        }
        details = self.delegation_details.get(identifier, _DelegationDetails())
        detail_changed = details.capture(update, status, metadata["text"])
        model_metadata = details.capture_model(update.get("model_request"), self.cursor + 1, status)
        detail_changed |= bool(model_metadata)
        if (
            not detail_changed
            and previous is not None
            and all(previous.get(key) == value for key, value in fields.items())
        ):
            return
        text = str(fields.pop("text"))
        if model_metadata:
            fields.update(detail_type=model_metadata["type"])
        self.append("delegation", text, **fields)
        self.delegations[identifier] = dict(self.events[-1])
        details.remember({**self.events[-1], **model_metadata})
        self.delegation_details[identifier] = details
        # Keep every admitted/pending task. Only completed history is disposable;
        # a long task may finish after many later requests have come and gone.
        terminal = sorted(
            (item for item in self.delegations.values() if item["status"] in _DELEGATION_TERMINAL),
            key=lambda item: cast("int", item["seq"]),
        )
        for item in terminal[:-MAX_COMPLETED_DELEGATIONS]:
            removed = str(item["delegation_id"])
            self.delegations.pop(removed)
            self.delegation_details.pop(removed, None)

    def snapshot(self, after: int = 0) -> Payload:
        return {
            "session_id": self.identifier,
            "status": self.status,
            "model": self.model,
            "voice": self.voice,
            "events": [dict(event) for event in self.events if cast("int", event["seq"]) > after],
            "cursor": self.cursor,
            "message": self.message,
            "context": dict(self.context),
            "delegations": [
                dict(item) for item in sorted(self.delegations.values(), key=lambda item: cast("int", item["seq"]))
            ],
        }


@dataclass
class _Call:
    record: _Record = field(default_factory=_Record)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    attached: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] = field(init=False, repr=False)
    answer: str = ""
    deadline: float = 0
    finalized: bool = False
    failure: str = ""
    http_status: int = 502
    reason: str = "Live session closed."
    stream: bool = False
    audio_in: _BrowserInput | None = None
    audio_out: _BrowserOutput | None = None
    browser: bool = False
    captions: Callable[[Payload], Awaitable[None]] | None = field(default=None, repr=False)
    seed: LiveSeed = field(default_factory=LiveSeed, repr=False)

    def fail(self, message: str, status: int = 502) -> None:
        if not self.failure:
            self.failure, self.http_status = message, status
            self.record.append("error", message)

    def request_stop(self, reason: str) -> None:
        if not self.stop.is_set() and self.record.status not in {"closed", "error"}:
            self.reason = reason
            self.stop.set()
            self.record.transition("closing", reason)


def _sdp(value: object) -> str:
    # SDP itself is not interpreted here; reject empty, oversized, or non-SDP
    # envelopes without logging the offer (which includes ICE credentials).
    if (
        not isinstance(value, str)
        or not value.startswith(("v=0\r\n", "v=0\n"))
        or len(value) > MAX_SDP_BYTES
        or len(value.encode("utf-8", errors="replace")) > MAX_SDP_BYTES
        or _CONTROLS.search(value)
    ):
        raise ValueError("Invalid SDP")
    return value


def _identifier(result: Payload) -> str:
    session = result.get("session")
    identifier = session.get("id") if isinstance(session, dict) else ""
    if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", identifier):
        raise ValueError("Invalid Live session")
    return identifier


def _transcript(record: _Record, speaker: str, text: object, payload: Payload, secret: str) -> None:
    if not isinstance(text, str):
        return
    if secret:
        text = text.replace(secret, "[redacted]")
    text = _CONTROLS.sub("", text[:MAX_TEXT_CHARACTERS])
    if not text:
        return
    fields: Payload = {"speaker": speaker}
    start, end = payload.get("start_ms"), payload.get("end_ms")
    if (
        isinstance(start, int | float)
        and not isinstance(start, bool)
        and isinstance(end, int | float)
        and not isinstance(end, bool)
        and 0 <= start <= end <= 1e12
    ):
        fields.update(start_ms=start, end_ms=end)
    source_id = payload.get("source_event_id", payload.get("event_id"))
    if isinstance(source_id, str) and source_id and len(source_id) <= 256:
        identity = json.dumps([source_id, speaker, text, fields.get("start_ms"), fields.get("end_ms")])
        if identity in record.caption_keys:
            return
        record.caption_keys.append(identity)
    record.append("transcript", text, **fields)


class _BrowserInput:
    """Bounded PCM, with one WebSocket playout clock or raw input for RTP."""

    audio_format = AudioFormat()

    def __init__(self, *, fill_gaps: bool = True) -> None:
        self.frames: asyncio.Queue[bytes] = asyncio.Queue(maxsize=50)
        self.connected = asyncio.Event()
        self.fill_gaps = fill_gaps

    async def __aiter__(self) -> AsyncIterator[bytes]:
        await self.connected.wait()
        loop = asyncio.get_running_loop()
        deadline = loop.time()
        fmt = self.audio_format
        bytes_per_second = fmt.sample_rate * fmt.sample_width * fmt.channels
        while True:
            if not self.fill_gaps:
                yield await self.frames.get()
                continue
            # Browser capture is already paced, but network delivery can batch
            # frames. Queued PCM and gap silence must share one output clock:
            # an independent queue timeout followed by immediate PCM forwards
            # would insert another audio timeline between delayed frames.
            now = loop.time()
            if now - deadline > 0.1:
                deadline = now
            await asyncio.sleep(max(0, deadline - now))
            # Retain small scheduling jitter in the cumulative clock. Rebase
            # only a substantial stall, including one during the sleep itself,
            # so coarse timers do not slow capture or cause a backlog burst.
            now = loop.time()
            if now - deadline > 0.1:
                deadline = now
            try:
                frame = self.frames.get_nowait()
            except asyncio.QueueEmpty:
                frame = SILENCE_FRAME
            yield frame
            deadline += len(frame) / bytes_per_second


class LiveService:
    """Single-event-loop call ownership, independent of HTTP request lifetimes.

    ``session_id`` is an opaque application ID, never a caller-selected provider
    ID. Successful polls renew a 40-second browser lease. The last eight ended
    calls retain at most 256 normalized events each, without Agents or secrets.
    """

    def __init__(
        self,
        factory: Callable[[str], Agent],
        *,
        login_factory: Callable[[str], LoginVoiceConfig | None] | None = None,
        caption_factory: Callable[[str], Awaitable[Callable[[Payload], Awaitable[None]]]] | None = None,
        context_factory: Callable[[str], Awaitable[LiveSeed]] | None = None,
    ) -> None:
        self._factory = factory
        self._login_factory = login_factory
        self._caption_factory = caption_factory
        self._context_factory = context_factory
        self._active: dict[str, _Call] = {}
        self._records: dict[str, _Record] = {}
        self._closed = False

    @property
    def active_session_id(self) -> str:
        """Discover the owned reservation, including provisioning and teardown."""
        return next(iter(self._active), "")

    def delegation_reporter(self, session_id: str) -> Callable[[Payload], None]:
        """Pin lifecycle observations to the original call, including after End."""
        record = self._lookup(session_id)

        def report(update: Payload) -> None:
            record.delegation(update)
            self._prune_records()

        return report

    async def delegation_details(self, session_id: str, delegation_id: str) -> Payload:
        """Explicit inspector read; ordinary polling contains metadata only."""
        record = self._lookup(session_id)
        state = record.delegations.get(delegation_id)
        details = record.delegation_details.get(delegation_id)
        if state is None or details is None:
            raise HTTPException(404, "Unknown Live delegation.")
        return {**state, **details.snapshot()}

    async def context_details(self, session_id: str) -> Payload:
        record = self._lookup(session_id)
        return {
            **deepcopy(record.context),
            "voice_session_id": record.identifier,
            "chat_session_id": record.context.get("chat_session_id", ""),
            **(
                deepcopy(record.context_details)
                if record.context_details
                else {
                    "available": False,
                    "instructions": text_preview("", 0),
                    "history": [],
                    "history_truncated": False,
                    "reason": "Voice setup has not been dispatched.",
                }
            ),
        }

    def _prune_records(self) -> None:
        completed = [
            key
            for key, record in self._records.items()
            if key not in self._active
            and record.status in {"closed", "error"}
            and all(item["status"] in _DELEGATION_TERMINAL for item in record.delegations.values())
        ]
        for key in completed[:-MAX_COMPLETED]:
            del self._records[key]

    async def create(self, sdp: str, voice: str = "") -> Payload:
        if self._closed:
            raise HTTPException(503, "Live is shutting down.")
        if self._active:
            raise HTTPException(409, "End the active Live conversation before starting another.")
        try:
            _sdp(sdp)
        except ValueError:
            raise HTTPException(422, "A valid SDP offer of at most 64 KiB is required.") from None
        if not isinstance(voice, str) or (voice and not _NAME.fullmatch(voice)):
            raise HTTPException(422, "Invalid Live voice.")

        # No await between admission and reservation, including factory startup.
        call = _Call()
        identifier = call.record.identifier
        self._active[identifier] = call
        self._records[identifier] = call.record
        call.record.append("status", call.record.message)
        call.task = asyncio.create_task(self._run_created(call, sdp, voice), name="web-live-session")
        try:
            await call.ready.wait()
            if call.stop.is_set() or call.failure or call.task.done():
                await join_owned(call.task)
                raise HTTPException(call.http_status, call.failure or "Live session creation was stopped.")
            return {
                "session_id": identifier,
                "sdp": call.answer,
                "model": call.record.model,
                "voice": call.record.voice,
                **({"context": dict(call.record.context)} if call.record.context else {}),
            }
        except asyncio.CancelledError:
            call.request_stop("Live session creation was cancelled.")
            await join_owned(call.task)
            raise
        finally:
            call.answer = ""

    async def create_stream(self, voice: str = "") -> Payload:
        """Start one server-to-provider WebSocket; browser audio joins separately."""
        if self._closed:
            raise HTTPException(503, "Live is shutting down.")
        if self._active:
            raise HTTPException(409, "End the active Live conversation before starting another.")
        if not isinstance(voice, str) or (voice and not _NAME.fullmatch(voice)):
            raise HTTPException(422, "Invalid Live voice.")
        call = _Call(stream=True, audio_in=_BrowserInput(), audio_out=_BrowserOutput())
        identifier = call.record.identifier
        self._active[identifier] = call
        self._records[identifier] = call.record
        call.record.append("status", call.record.message)
        call.task = asyncio.create_task(self._run_stream_created(call, voice), name="web-live-relay")
        try:
            await call.ready.wait()
            if call.stop.is_set() or call.failure or call.task.done():
                await join_owned(call.task)
                raise HTTPException(call.http_status, call.failure or "Live session creation was stopped.")
            return {
                "session_id": identifier,
                "model": call.record.model,
                "voice": call.record.voice,
                **({"context": dict(call.record.context)} if call.record.context else {}),
            }
        except asyncio.CancelledError:
            call.request_stop("Live session creation was cancelled.")
            await join_owned(call.task)
            raise

    async def serve_audio(self, session_id: str, socket: WebSocket) -> None:
        """Only the reserved browser owns the raw PCM connection."""
        call = self._active.get(session_id)
        if call is None or not call.stream or call.browser or call.stop.is_set() or call.record.status != "connected":
            await socket.close(code=1008)
            return
        source, sink = call.audio_in, call.audio_out
        assert source is not None and sink is not None
        call.browser = True
        protocol = "ngn.live.v2" if "ngn.live.v2" in socket.scope.get("subprotocols", ()) else "ngn.live.v1"
        await socket.accept(subprotocol=protocol)
        source.connected.set()

        async def playback() -> None:
            async for packet in sink.packets():
                if packet.interrupted:
                    # Cached v1 clients accept PCM only. Explicit v2 negotiation
                    # is required before sending any playback-control text.
                    if protocol == "ngn.live.v2":
                        await socket.send_json({"type": "interrupt"})
                else:
                    await socket.send_bytes(packet.data)

        sender = asyncio.create_task(playback(), name="web-live-playback")
        stopped = asyncio.create_task(call.stop.wait())
        try:
            while not call.stop.is_set() and not sender.done():
                incoming = asyncio.create_task(socket.receive())
                done, _ = await asyncio.wait({incoming, sender, stopped}, return_when=asyncio.FIRST_COMPLETED)
                if incoming not in done:
                    incoming.cancel()
                    await asyncio.gather(incoming, return_exceptions=True)
                    if sender in done:
                        sender.result()
                    break
                packet = incoming.result()
                frame = packet.get("bytes")
                if packet["type"] != "websocket.receive" or not isinstance(frame, bytes):
                    break
                if not frame or len(frame) > MAX_AUDIO_FRAME or len(frame) % 2:
                    await socket.close(code=1003)
                    break
                source.frames.put_nowait(frame)
        except (WebSocketDisconnect, asyncio.QueueFull):
            pass
        finally:
            call.request_stop("Live browser audio disconnected.")
            await sink.close()
            sender.cancel()
            stopped.cancel()
            await asyncio.gather(sender, stopped, return_exceptions=True)
            await join_owned(call.task)
            with suppress(Exception):
                await socket.close()

    async def snapshot(self, session_id: str, after: int = 0) -> Payload:
        record = self._lookup(session_id)
        if type(after) is not int or after < 0:
            raise HTTPException(422, "Live event cursor must be a nonnegative integer.")
        call = self._active.get(session_id)
        if call is not None and record.status == "connected" and not call.stop.is_set():
            now = asyncio.get_running_loop().time()
            if now >= call.deadline:
                call.request_stop("Live browser heartbeat expired.")
            else:
                call.deadline = now + LEASE_SECONDS
        return record.snapshot(after)

    async def close(self, session_id: str) -> Payload:
        record = self._lookup(session_id)
        call = self._active.get(session_id)
        if call is not None:
            call.request_stop("Live session closed.")
            await join_owned(call.task)
        return record.snapshot()

    async def shutdown(self) -> None:
        self._closed = True
        calls = list(self._active.values())
        for call in calls:
            call.request_stop("Live service shut down.")
        for call in calls:
            await join_owned(call.task)

    def _lookup(self, identifier: str) -> _Record:
        if not isinstance(identifier, str) or identifier not in self._records:
            raise HTTPException(404, "Unknown Live session.")
        return self._records[identifier]

    async def _run_created(self, call: _Call, sdp: str, voice: str) -> None:
        try:
            await self._seed(call)
            if self._caption_factory is not None:
                call.captions = await self._caption_factory(call.record.identifier)
            login = self._login_factory(voice) if self._login_factory is not None else None
        except Exception:
            call.fail("ChatGPT voice configuration is unavailable. Check your selected connection.", 503)
            call.record.transition("error", call.failure)
            self._active.pop(call.record.identifier, None)
            self._prune_records()
            call.ready.set()
            return
        if login is not None:
            await self._run_login(call, sdp, replace(login, history=call.seed.history))
        else:
            await self._run(call, sdp, voice)

    async def _seed(self, call: _Call) -> None:
        if self._context_factory is not None:
            call.seed = await self._context_factory(call.record.identifier)
            call.record.context = call.seed.report()

    def _capture_context(self, call: _Call, instructions: str, history: tuple[Payload, ...]) -> None:
        if not call.record.context_details:
            # The inspector must not affect voice admission or provider behavior.
            with suppress(Exception):
                call.record.context_details = seed_details(instructions, history)

    def _capture_agent_context(self, call: _Call, agent: Agent) -> None:
        if call.record.context_details:
            return
        with suppress(Exception):
            # Read the same session builder used by the public Live transport,
            # including its effective instruction/history semantics.
            config = agent.live_configuration()
            instructions, history = config.get("instructions"), config.get("input")
            if (
                isinstance(instructions, str)
                and isinstance(history, list)
                and len(history) <= 16
                and all(isinstance(item, dict) for item in history)
            ):
                self._capture_context(call, instructions, tuple(history))
                return
        call.record.context_details = {
            "available": False,
            "instructions": text_preview("", 0),
            "history": [],
            "history_truncated": False,
            "reason": "Voice setup details could not be captured.",
        }

    async def _run_stream_created(self, call: _Call, voice: str) -> None:
        try:
            login = self._login_factory(voice) if self._login_factory is not None else None
            if login is None:
                # API-key and Entra connections use their native server-side
                # audio transport; Codex login uses a server-owned media peer.
                await self._run_stream(call, voice)
                return
            from .live_relay import ChatGPTMediaRelay

            await self._seed(call)
            if self._caption_factory is not None:
                call.captions = await self._caption_factory(call.record.identifier)
            assert call.audio_in is not None and call.audio_out is not None
            # The RTP track owns the continuous 20 ms clock. Padding this input
            # too would add silence between slightly delayed browser frames and
            # overfeed a correctly paced sender. Primary provider WebSockets
            # instead use the input's single PCM/silence playout clock.
            call.audio_in.fill_gaps = False
            media = ChatGPTMediaRelay(call.audio_in, call.audio_out)
        except ImportError:
            call.fail("Install nagents[web] on the server to enable ChatGPT voice audio.", 503)
        except Exception:
            call.fail("ChatGPT voice configuration is unavailable. Check your selected connection.", 503)
        else:
            await self._run_login(call, "", replace(login, history=call.seed.history), media)
            return
        call.record.transition("error", call.failure)
        self._active.pop(call.record.identifier, None)
        self._prune_records()
        call.ready.set()

    async def _run_login(
        self, call: _Call, sdp: str, config: LoginVoiceConfig, media: ChatGPTMediaRelay | None = None
    ) -> None:
        connection = ChatGPTLiveConnection(config)
        observers: list[asyncio.Task[None]] = []
        call.record.model, call.record.voice = config.model, config.voice

        async def observe() -> None:
            try:
                async with aclosing(connection.events()) as events:
                    async for payload in events:
                        event = LiveEvent(event_type=str(payload.get("type", "")), payload=payload)
                        if await self._capture_event(call, event, connection.identifier, ""):
                            return
            except LoginVoiceError as error:
                call.fail(str(error))
            except Exception:
                call.fail("The ChatGPT voice control connection was interrupted.")
            finally:
                call.finalized = connection.finalized

        async def watch() -> None:
            reading = asyncio.create_task(observe(), name="web-chatgpt-live-events")
            workers = [reading]
            if media is not None:
                workers.append(asyncio.create_task(media.wait(), name="web-chatgpt-live-media"))
            try:
                done, _ = await asyncio.wait(workers, return_when=asyncio.FIRST_COMPLETED)
                for worker in done:
                    worker.result()
            except LoginVoiceError as error:
                call.fail(str(error))
            except Exception:
                call.fail("The ChatGPT voice connection was interrupted.")
            finally:
                for worker in workers:
                    worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)

        async def prepare() -> None:
            async with asyncio.timeout(PROVISION_SECONDS):
                offer = await media.offer() if media is not None else sdp
                if call.stop.is_set():
                    return
                self._capture_context(call, config.instructions, config.history)
                answer = _sdp(await connection.provision(offer))
                observers.append(asyncio.create_task(watch(), name="web-chatgpt-live-sideband"))
                if call.stop.is_set():
                    return
                if media is not None:
                    await media.connect(answer)
                else:
                    call.answer = answer

        preparing: list[asyncio.Task[None]] = []

        async def wait_for_stop() -> None:
            await call.stop.wait()

        try:
            if call.stop.is_set():
                return
            setup = asyncio.create_task(prepare(), name="web-chatgpt-live-provision")
            stopped = asyncio.create_task(wait_for_stop())
            preparing.extend([setup, stopped])
            await asyncio.wait(preparing, return_when=asyncio.FIRST_COMPLETED)
            if call.stop.is_set():
                # Once POST has started, cancellation cannot prove the provider
                # did not allocate a call. Keep its bounded response reader until
                # we learn the call ID, then the owned cleanup can close it. A
                # known ID is sufficient to cancel a stalled media handshake.
                if not connection.identifier:
                    await setup
                return
            await setup
            await self._connected(call, observers[0])
        except LoginVoiceError as error:
            call.fail(str(error))
        except Exception:
            call.fail("The ChatGPT voice connection could not be established.")
        finally:
            for pending in preparing:
                pending.cancel()
            await asyncio.gather(*preparing, return_exceptions=True)
            if call.audio_out is not None:
                await call.audio_out.close()
            if media is not None:
                with suppress(Exception):
                    await media.quiet()
            if call.record.status != "closing":
                call.record.transition("closing", "Closing ChatGPT voice.")
            with suppress(Exception):
                async with asyncio.timeout(CLEANUP_SECONDS):
                    await connection.close()
            if observers and not observers[0].done():
                await asyncio.wait(observers, timeout=FINALIZE_SECONDS)
            for observer in observers:
                if not observer.done():
                    observer.cancel()
            await asyncio.gather(*observers, return_exceptions=True)
            with suppress(Exception):
                async with asyncio.timeout(CLEANUP_SECONDS + 1):
                    await connection.aclose()
            if media is not None:
                try:
                    async with asyncio.timeout(CLEANUP_SECONDS):
                        await media.aclose()
                except Exception:
                    call.fail("ChatGPT voice audio cleanup could not be confirmed.")
            call.finalized = connection.finalized
            if connection.identifier and not call.finalized:
                call.fail("ChatGPT voice finalization could not be confirmed.")
            confirmation = "Finalization confirmed." if call.finalized else "Finalization is unconfirmed."
            call.record.transition(
                "error" if call.failure else "closed", f"{call.failure or call.reason} {confirmation}"
            )
            self._active.pop(call.record.identifier, None)
            self._prune_records()
            call.ready.set()

    async def _run(self, call: _Call, sdp: str, voice: str) -> None:
        try:
            if call.stop.is_set():
                return
            try:
                agent = self._factory(voice)
                if self._context_factory is not None and agent.provider.live_config is not None:
                    agent.provider.live_config = replace(
                        agent.provider.live_config, history=call.seed.history, history_in_client_context=False
                    )
            except HTTPException as error:
                if error.status_code in {400, 422}:
                    call.fail("Invalid Live voice.", 422)
                else:
                    call.fail("Live configuration is unavailable. Check the server voice setup.", 503)
                return
            except Exception:
                call.fail("Live configuration is unavailable. Check the server voice setup.", 503)
                return
            try:
                config = agent.provider.live_config
                if (
                    config is None
                    or config.delegation not in {"responses", "client"}
                    or config.attach_to
                    or config.fork_from
                    or (config.delegation == "responses" and config.client_handler is not None)
                    or (config.delegation == "client" and config.client_handler is None)
                    or agent.delegation_agent is not None
                    or agent.tool_registry.get_all()
                    or not isinstance(config.voice, str)
                    or not _NAME.fullmatch(config.voice)
                    or not _NAME.fullmatch(agent.provider.model)
                ):
                    call.fail("Live requires a dedicated voice agent with an owned backend.", 503)
                    return
                call.record.model, call.record.voice = agent.provider.model, config.voice
                api = LiveAPI(agent.provider)
                # Provisioning is bounded but is NOT canceled by an HTTP client
                # disconnect: losing its response could orphan an allocated call.
                async with asyncio.timeout(PROVISION_SECONDS):
                    self._capture_agent_context(call, agent)
                    result = await api.create_webrtc(sdp, agent.live_configuration(media=True))
                del sdp
                identifier = _identifier(result)
                observers: list[asyncio.Task[None]] = []
                try:
                    transport = result.get("transport")
                    call.answer = _sdp(transport.get("sdp") if isinstance(transport, dict) else "")
                    del result
                    if call.stop.is_set():
                        return
                    agent.provider.live_config = replace(
                        config,
                        attach_to=identifier,
                        close_session_on_exit=True,
                        close_timeout=min(config.close_timeout, FINALIZE_SECONDS),
                        handle_delegations=config.delegation == "client",
                    )
                    agent.audio = None
                    observer = asyncio.create_task(self._observe(call, agent, identifier), name="web-live-sideband")
                    observers.append(observer)
                    await self._connected(call, observer)
                except Exception:
                    call.fail("Live session setup failed.")
                finally:
                    await self._finalize(call, agent, api, identifier, observers)
            except Exception:
                call.fail("Live session creation failed. Provider finalization is unconfirmed.")
            finally:
                for owner in (agent, *([agent.delegation_agent] if agent.delegation_agent is not None else [])):
                    try:
                        async with asyncio.timeout(CLEANUP_SECONDS):
                            await owner.close()
                    except Exception:
                        call.fail("Live resource cleanup could not be confirmed.")
        finally:
            confirmation = "Finalization confirmed." if call.finalized else "Finalization is unconfirmed."
            call.record.transition(
                "error" if call.failure else "closed", f"{call.failure or call.reason} {confirmation}"
            )
            self._active.pop(call.record.identifier, None)
            self._prune_records()
            call.ready.set()

    async def _run_stream(self, call: _Call, voice: str) -> None:
        agent: Agent | None = None
        try:
            try:
                await self._seed(call)
                if self._caption_factory is not None:
                    call.captions = await self._caption_factory(call.record.identifier)
                if call.stop.is_set():
                    return
                agent = self._factory(voice)
                if self._context_factory is not None and agent.provider.live_config is not None:
                    agent.provider.live_config = replace(
                        agent.provider.live_config, history=call.seed.history, history_in_client_context=False
                    )
                config = agent.provider.live_config
                if (
                    config is None
                    or config.delegation not in {"responses", "client"}
                    or config.attach_to
                    or config.fork_from
                    or (config.delegation == "responses" and config.client_handler is not None)
                    or (config.delegation == "client" and config.client_handler is None)
                    or agent.delegation_agent is not None
                    or agent.tool_registry.get_all()
                    or not isinstance(config.voice, str)
                    or not _NAME.fullmatch(config.voice)
                    or not _NAME.fullmatch(agent.provider.model)
                ):
                    call.fail("Live requires a dedicated voice agent with an owned backend.", 503)
                    return
                call.record.model, call.record.voice = agent.provider.model, config.voice
                assert call.audio_in is not None and call.audio_out is not None
                agent.audio = AudioDuplex(input=call.audio_in, output=call.audio_out)
                observer = asyncio.create_task(self._observe(call, agent, ""), name="web-live-upstream")
                await self._connected(call, observer)
                await call.audio_out.close()
                if call.record.status != "closing":
                    call.record.transition("closing", "Closing Live session.")
                if call.attached.is_set() and not observer.done():
                    with suppress(Exception):
                        async with asyncio.timeout(CLEANUP_SECONDS):
                            if not agent.live.closing:
                                await agent.live.close()
                        await asyncio.wait({observer}, timeout=FINALIZE_SECONDS)
                if not observer.done():
                    observer.cancel()
                await asyncio.gather(observer, return_exceptions=True)
                with suppress(RuntimeError):
                    call.finalized = call.finalized or agent.live.status.finalized
                if call.attached.is_set() and not call.finalized:
                    call.fail("Live session shutdown could not be confirmed.")
            except Exception:
                if not call.failure:
                    call.fail("Live connection failed. Provider finalization is unconfirmed.")
        finally:
            if call.audio_out is not None:
                await call.audio_out.close()
            if agent is not None:
                try:
                    async with asyncio.timeout(CLEANUP_SECONDS):
                        await agent.close()
                except Exception:
                    call.fail("Live resource cleanup could not be confirmed.")
            confirmation = "Finalization confirmed." if call.finalized else "Finalization is unconfirmed."
            call.record.transition(
                "error" if call.failure else "closed", f"{call.failure or call.reason} {confirmation}"
            )
            self._active.pop(call.record.identifier, None)
            self._prune_records()
            call.ready.set()

    async def _connected(self, call: _Call, observer: asyncio.Task[None]) -> None:
        attached = asyncio.create_task(call.attached.wait())
        stopped = asyncio.create_task(call.stop.wait())
        try:
            await asyncio.wait(
                {attached, stopped, observer}, timeout=ATTACH_SECONDS, return_when=asyncio.FIRST_COMPLETED
            )
            if call.stop.is_set():
                return
            if not call.attached.is_set() or observer.done():
                call.fail("Live sideband connection could not be established.")
                return
            call.deadline = asyncio.get_running_loop().time() + LEASE_SECONDS
            call.record.transition("connected", "Live connected.")
            call.ready.set()
            while not observer.done() and not call.stop.is_set():
                remaining = call.deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    call.request_stop("Live browser heartbeat expired.")
                    break
                # A renewed lease is re-read after this wait's old deadline.
                await asyncio.wait({observer, stopped}, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
        finally:
            attached.cancel()
            stopped.cancel()
            await asyncio.gather(attached, stopped, return_exceptions=True)

    async def _observe(self, call: _Call, agent: Agent, identifier: str) -> None:
        try:
            self._capture_agent_context(call, agent)
            async with aclosing(agent.run()) as events:
                async for event in events:
                    if await self._capture_event(call, event, identifier, agent.provider.api_key):
                        return
            if not call.finalized:
                call.fail("Live connection ended before finalization.")
        except asyncio.CancelledError:
            raise
        except Exception:
            if not call.finalized:
                call.fail("Live connection was interrupted.")

    async def _capture_event(self, call: _Call, event: Event, identifier: str, secret: str) -> bool:
        previous = call.record.cursor
        stopped = self._event(call, event, identifier, secret)
        if call.captions is not None and call.record.cursor != previous:
            latest = call.record.events[-1]
            if latest["type"] == "transcript":
                await call.captions(dict(latest))
        return stopped

    @staticmethod
    def _event(call: _Call, event: Event, identifier: str, secret: str) -> bool:
        record = call.record
        if isinstance(event, InputTranscriptDeltaEvent | AudioTranscriptDeltaEvent):
            speaker = "user" if isinstance(event, InputTranscriptDeltaEvent) else "assistant"
            _transcript(record, speaker, event.delta, event.extra, secret)
        elif isinstance(event, ErrorEvent):
            if event.recoverable:
                record.append("error", "A Live request could not be completed.")
            else:
                call.fail("Live connection reported an error.")
                return True
        elif isinstance(event, LiveEvent):
            if event.event_type == "session.started" and not identifier:
                call.attached.set()
            elif event.event_type == "connection.attached":
                session = event.payload.get("session")
                if not isinstance(session, dict) or session.get("id") != identifier:
                    call.fail("Live sideband attachment could not be verified.")
                    return True
                call.attached.set()
            elif event.event_type == "session.closed":
                call.finalized = True
            elif event.event_type == "error":
                # Live command rejections are not terminal session events. The
                # native reader keeps receiving until closure or transport loss.
                record.append("error", "A Live request could not be completed.")
            elif event.event_type in {"session.input_transcript.delta", "session.output_transcript.delta"}:
                speaker = "user" if event.event_type == "session.input_transcript.delta" else "assistant"
                _transcript(record, speaker, event.payload.get("delta"), event.payload, secret)
        return False

    async def _finalize(
        self, call: _Call, agent: Agent, api: LiveAPI, identifier: str, observers: list[asyncio.Task[None]]
    ) -> None:
        if call.record.status != "closing":
            call.record.transition("closing", "Closing Live session.")
        for observer in observers:
            if not observer.done() and not call.finalized:
                try:
                    async with asyncio.timeout(CLEANUP_SECONDS):
                        if not agent.live.closing:
                            await agent.live.close()
                    await asyncio.wait({observer}, timeout=FINALIZE_SECONDS)
                except Exception:
                    pass  # Not attached, disconnected, or command rejected: use HTTP.
        with suppress(RuntimeError):
            call.finalized = call.finalized or agent.live.status.finalized
        hangup_failed = False
        if not call.finalized:
            try:
                async with asyncio.timeout(CLEANUP_SECONDS):
                    await api.hangup(identifier)
            except Exception:
                hangup_failed = True
        for observer in observers:
            if not observer.done():
                observer.cancel()
            try:
                # Agent.run drains final session usage when canceled. Its native
                # close_timeout is capped above; allow that drain before joining.
                async with asyncio.timeout(FINALIZE_SECONDS + CLEANUP_SECONDS):
                    await observer
            except asyncio.CancelledError:
                pass
            except Exception:
                call.fail("Live sideband cleanup could not be confirmed.")
        with suppress(RuntimeError):
            call.finalized = call.finalized or agent.live.status.finalized
        # Native finalization can race the HTTP fallback or arrive during drain.
        # Defer only the fallback failure; independent errors remain recorded.
        if hangup_failed and not call.finalized:
            call.fail("Live session shutdown could not be confirmed.")
