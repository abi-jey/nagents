"""Bounded, server-owned WebRTC calls; the browser owns all audio devices.

One supervisor owns provisioning, the Agent sideband, and finalization. Request
cancellation stops that supervisor cooperatively: an in-flight provisioning
response is collected so that its newly allocated session can still be hung up.
Only normalized captions and application status survive a completed call.
"""

from __future__ import annotations

import asyncio
import re
from collections import deque
from contextlib import aclosing
from contextlib import suppress
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from typing import TYPE_CHECKING
from typing import Literal
from typing import cast
from uuid import uuid4

from fastapi import HTTPException

from nagents.events import AudioTranscriptDeltaEvent
from nagents.events import ErrorEvent
from nagents.events import InputTranscriptDeltaEvent
from nagents.live import LiveAPI
from nagents.live import LiveEvent

from ._async import join_owned

if TYPE_CHECKING:
    from collections.abc import Callable

    from nagents import Agent
    from nagents.events import Event

MAX_SDP_BYTES = 65536
MAX_EVENTS = 256
MAX_TEXT_CHARACTERS = 4096
MAX_COMPLETED = 8
LEASE_SECONDS = 40.0
PROVISION_SECONDS = 25.0
ATTACH_SECONDS = 10.0
FINALIZE_SECONDS = 10.0
CLEANUP_SECONDS = 5.0

Status = Literal["connecting", "connected", "closing", "closed", "error"]
Payload = dict[str, object]
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}\Z")
_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass
class _Record:
    identifier: str = field(default_factory=lambda: uuid4().hex)
    model: str = ""
    voice: str = ""
    status: Status = "connecting"
    message: str = "Connecting to Live."
    events: deque[Payload] = field(default_factory=lambda: deque(maxlen=MAX_EVENTS))
    cursor: int = 0

    def append(self, kind: str, text: str, **fields: object) -> None:
        self.cursor += 1
        self.events.append({"seq": self.cursor, "type": kind, "text": text, **fields})

    def transition(self, status: Status, message: str) -> None:
        self.status, self.message = status, message
        self.append("status", message)

    def snapshot(self, after: int = 0) -> Payload:
        return {
            "session_id": self.identifier,
            "status": self.status,
            "model": self.model,
            "voice": self.voice,
            "events": [dict(event) for event in self.events if cast("int", event["seq"]) > after],
            "cursor": self.cursor,
            "message": self.message,
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
    record.append("transcript", text, **fields)


class LiveService:
    """Single-event-loop call ownership, independent of HTTP request lifetimes.

    ``session_id`` is an opaque application ID, never a caller-selected provider
    ID. Successful polls renew a 40-second browser lease. The last eight ended
    calls retain at most 256 normalized events each, without Agents or secrets.
    """

    def __init__(self, factory: Callable[[str], Agent]) -> None:
        self._factory = factory
        self._active: dict[str, _Call] = {}
        self._records: dict[str, _Record] = {}
        self._closed = False

    @property
    def active_session_id(self) -> str:
        """Discover the owned reservation, including provisioning and teardown."""
        return next(iter(self._active), "")

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
        call.task = asyncio.create_task(self._run(call, sdp, voice), name="web-live-session")
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
            }
        except asyncio.CancelledError:
            call.request_stop("Live session creation was cancelled.")
            await join_owned(call.task)
            raise
        finally:
            call.answer = ""

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

    async def _run(self, call: _Call, sdp: str, voice: str) -> None:
        try:
            if call.stop.is_set():
                return
            try:
                agent = self._factory(voice)
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
                    or config.delegation != "responses"
                    or config.attach_to
                    or config.fork_from
                    or config.client_handler is not None
                    or agent.delegation_agent is not None
                    or agent.tool_registry.get_all()
                    or not isinstance(config.voice, str)
                    or not _NAME.fullmatch(config.voice)
                    or not _NAME.fullmatch(agent.provider.model)
                ):
                    call.fail("Live requires a dedicated hosted voice agent.", 503)
                    return
                call.record.model, call.record.voice = agent.provider.model, config.voice
                api = LiveAPI(agent.provider)
                # Provisioning is bounded but is NOT canceled by an HTTP client
                # disconnect: losing its response could orphan an allocated call.
                async with asyncio.timeout(PROVISION_SECONDS):
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
                        handle_delegations=False,
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
            completed = [key for key in self._records if key not in self._active]
            for key in completed[:-MAX_COMPLETED]:
                del self._records[key]
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
            async with aclosing(agent.run()) as events:
                async for event in events:
                    if self._event(call, event, identifier, agent.provider.api_key):
                        return
            if not call.finalized:
                call.fail("Live connection ended before finalization.")
        except asyncio.CancelledError:
            raise
        except Exception:
            if not call.finalized:
                call.fail("Live connection was interrupted.")

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
            if event.event_type == "connection.attached":
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
        if not call.finalized:
            try:
                async with asyncio.timeout(CLEANUP_SECONDS):
                    await api.hangup(identifier)
            except Exception:
                call.fail("Live session shutdown could not be confirmed.")
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
