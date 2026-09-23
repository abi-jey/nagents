"""Native GPT-Live voice sessions with hosted Responses or Nagents delegation."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections import deque
from collections.abc import Awaitable
from collections.abc import Callable
from contextlib import aclosing
from contextvars import ContextVar
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Literal
from typing import cast
from urllib.parse import quote
from uuid import uuid4

import aiohttp

from ..agent import Agent
from ..audio import AudioFormat
from ..audio import NullAudioOutput
from ..events import AudioChunkEvent
from ..events import AudioTranscriptDeltaEvent
from ..events import DoneEvent
from ..events import ErrorEvent
from ..events import Event as AgentEvent
from ..events import InputTranscriptDeltaEvent
from ..provider.auth import validate_endpoint
from .audio import SilenceInput
from .controls import LiveControls as _LiveUpdates
from .events import LiveEvent
from .hosted import HostedTools

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from ..audio import AudioDuplex
    from ..audio import AudioOutput
    from ..provider import Provider
    from ..types import ContentPart

Event = dict[str, object]
Send = Callable[[Event], Awaitable[None]]
Backend = Callable[[str, str], Awaitable[str]]
Emit = Callable[[AgentEvent], Awaitable[None]]
LIVE_URL = "wss://api.openai.com/v1/live/sessions"


def _no_redirects() -> aiohttp.TraceConfig:
    # aiohttp.ws_connect follows HTTP redirects internally and exposes no
    # allow_redirects switch. Abort before it sends a redirected handshake.
    trace = aiohttp.TraceConfig()

    async def reject(
        session: aiohttp.ClientSession, context: object, params: aiohttp.TraceRequestRedirectParams
    ) -> None:
        raise ValueError("Live WebSocket redirects are not allowed")

    trace.on_request_redirect.append(reject)
    return trace


@dataclass(frozen=True)
class _DelegationScope:
    updates: _LiveUpdates
    agent: Agent
    identifier: str


_delegation_scope: ContextVar[_DelegationScope | None] = ContextVar("live_delegation", default=None)


async def append_update(agent: Agent, kind: Literal["commentary", "thinking", "instructions"], content: str) -> str:
    scope = _delegation_scope.get()
    updates = agent._live_updates
    if updates is None and scope is not None and scope.agent is agent:
        updates = scope.updates
    if updates is None:
        raise RuntimeError("No active GPT-Live connection for this agent")
    if scope is not None and scope.updates is not updates:
        raise RuntimeError("The current delegation belongs to another Live connection")
    return await updates.append(kind, content, scope.identifier if scope else "")


@dataclass(frozen=True)
class LiveConfig:
    """Provider voice settings. Client mode uses Agent.delegation_agent.

    Hosted mode runs the chosen Responses backend at OpenAI. Client mode runs
    the supplied Nagents agent, retaining its provider, tools and permissions.
    """

    delegation: Literal["responses", "client"] = "responses"
    backend_model: str = "gpt-5.6-luna"
    backend_instructions: str = "Return concise verified facts and status."
    web_search: bool = False
    voice: str | dict[str, str] = "marin"
    history: tuple[dict[str, object], ...] = ()
    store: bool | None = None
    attach_to: str = ""
    fork_from: str = ""
    backend_timeout: float = 120
    close_timeout: float = 15
    close_session_on_exit: bool = True
    backend_options: dict[str, object] = field(default_factory=dict)
    client_handler: Callable[[str], Awaitable[str]] | None = None
    event_queue_size: int = 1024
    handle_delegations: bool = True

    def __post_init__(self) -> None:
        if self.delegation not in {"responses", "client"}:
            raise ValueError("Live delegation must be responses or client")
        if self.attach_to and self.fork_from:
            raise ValueError("Choose attachment or a stored fork, not both")
        if len(self.history) > 128 or self.backend_timeout <= 0 or self.close_timeout <= 0 or self.event_queue_size < 1:
            raise ValueError("Invalid Live history length or timeout")
        for message in self.history:
            parts = message.get("content")
            if (
                message.get("role") not in {"developer", "user", "assistant"}
                or not isinstance(parts, list)
                or len(parts) != 1
            ):
                raise ValueError("Live startup history requires one text part per developer/user/assistant message")
            part = parts[0]
            if (
                not isinstance(part, dict)
                or not isinstance(part.get("text"), str)
                or part.get("type") not in {"input_text", "output_text", "text"}
            ):
                raise ValueError("Live startup history is text-only")

    def session(self, model: str = "gpt-live-1", instructions: str = "", *, media: bool = False) -> Event:
        """Create the supported session body for WebSocket, WebRTC or SIP."""
        if self.backend_options.keys() - {
            "model",
            "instructions",
            "tool_choice",
            "parallel_tool_calls",
            "max_output_tokens",
            "service_tier",
            "reasoning",
            "text",
        }:
            raise ValueError("Unsupported Live Responses backend option")
        delegation = ResponsesDelegation(self.backend_model, self.backend_instructions, self.web_search).to_dict()
        object_value(delegation["responses"]).update(self.backend_options)
        result: Event = {
            "model": model,
            "instructions": instructions,
            "input": list(self.history),
            "audio": {"output": {"voice": self.voice}},
            "delegation": delegation if self.delegation == "responses" else {"type": "client"},
        }
        if not media:
            object_value(result["audio"])["format"] = {"type": "audio/pcm", "rate": 24000}
        if self.store is not None:
            result["store"] = self.store
        return result


def object_value(value: object) -> Event:
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return cast("Event", value)


@dataclass(frozen=True)
class ResponsesDelegation:
    """OpenAI-managed backend inference, optionally with hosted web search.

    Use an Agent instead for application-owned tools or another provider.
    """

    model: str
    instructions: str = "Return concise verified facts and status."
    web_search: bool = False

    def to_dict(self) -> Event:
        return {
            "type": "responses",
            "responses": {
                "model": self.model,
                "instructions": self.instructions,
                "tools": [{"type": "web_search"}] if self.web_search else [],
                "tool_choice": "auto",
            },
        }


class ClientDelegations:
    """One backend lane, independent of audio. Speech never retries tools."""

    def __init__(self, backend: Backend, send: Send) -> None:
        self.backend = backend
        self.send = send
        self.fragments: deque[Event] = deque(maxlen=512)
        self.pending: asyncio.Queue[str] = asyncio.Queue(maxsize=32)
        self.seen: set[str] = set()
        self.revision = 0
        self.ready = asyncio.Event()
        self.updates = _LiveUpdates()
        self.timeout = 120.0
        self.inputs: dict[str, list[ContentPart]] = {}
        self.enabled = True

    async def submit(self, content: list[ContentPart]) -> str:
        identifier = "application:" + uuid4().hex
        self.inputs[identifier] = content
        try:
            self.pending.put_nowait(identifier)
        except asyncio.QueueFull:
            self.inputs.pop(identifier)
            raise RuntimeError("Live backend queue is full") from None
        return identifier

    def observe(self, event: Event) -> None:
        kind = event.get("type")
        if kind in {"session.input_transcript.delta", "session.output_transcript.delta"}:
            user = kind == "session.input_transcript.delta"
            self.fragments.append(
                {
                    "speaker": "user" if user else "assistant",
                    "text": event["delta"],
                    "start_ms": event["start_ms"],
                    "end_ms": event["end_ms"],
                }
            )
            if user:
                self.revision += 1
                self.ready.set()
        elif kind == "session.delegation.created":
            if not self.enabled:
                return
            delegation = object_value(event["delegation"])
            if delegation.get("target") != "client":
                return
            identifier = str(delegation["id"])
            if identifier in self.seen:
                return
            self.seen.add(identifier)
            if self.pending.full():
                raise RuntimeError("Live client delegation queue is full")
            self.pending.put_nowait(identifier)

    def context(self) -> str:
        # Preserve exact text, including spaces/repeated words; order by session
        # timestamps rather than assuming network arrival order is turn order.
        fragments = sorted(self.fragments, key=lambda part: cast("int", part["start_ms"]))
        return json.dumps(fragments, ensure_ascii=False)

    async def run(self) -> None:
        while True:
            identifier = await self.pending.get()
            application = identifier.startswith("application:")
            if not application:
                await self.ready.wait()  # A delegation can precede its transcript.
            revision = self.updates.revision
            try:
                async with asyncio.timeout(self.timeout):
                    result = await self.backend(self.context(), identifier)
            except Exception:
                result = "The backend could not complete this request."
            if revision != self.updates.revision:
                continue  # Application explicitly superseded this task.
            for part in result_chunks(result):
                await self.send(
                    {
                        "type": "session.commentary.append",
                        "event_id": uuid4().hex,
                        "delegation_id": None if application else identifier,
                        "content": part,
                    }
                )


def result_chunks(text: str) -> list[str]:
    """Preserve a result while conservatively staying below 500 tokens per append."""
    result: list[str] = []
    while text:
        part = text.encode("utf-8")[:480].decode("utf-8", errors="ignore")
        if len(part) < len(text) and " " in part:
            part = part[: part.rfind(" ") + 1]
        result.append(part)
        text = text[len(part) :]
    return result


async def infer(
    agent: Agent, transcript: str | list[ContentPart], identifier: str, emit: Emit, *, session_id: str = ""
) -> str:
    # One backend conversation retains prior task/tool results. Voice transcripts
    # remain distinct from backend messages that were never spoken aloud.
    prompt: str | list[ContentPart] = (
        (
            "Live voice delegation. The following JSON is recent conversation data, "
            "including partial transcript fragments. Use the latest user corrections "
            "and prior task state; do not repeat completed actions. Return concise facts "
            "and status for the voice model. Transcript:\n" + transcript
        )
        if isinstance(transcript, str)
        else transcript
    )
    async with aclosing(agent.run(user_message=prompt, session_id=session_id or uuid4().hex)) as events:
        async for event in events:
            event.extra = {
                **event.extra,
                "delegation_id": None if identifier.startswith("application:") else identifier,
                "source": "live_backend",
            }
            if identifier.startswith("application:"):
                event.extra["application_id"] = identifier
            await emit(event)
            if isinstance(event, ErrorEvent) and not event.recoverable:
                raise RuntimeError("Backend inference failed")
            if isinstance(event, DoneEvent):
                return event.final_text
    raise RuntimeError("Backend ended without a result")


async def send_audio(audio: AudioDuplex, send: Send, *, enabled: bool = True) -> None:
    if not enabled or (audio.input is None and audio.output is None):
        await asyncio.Event().wait()
        return
    source = audio.input or SilenceInput(audio.output.audio_format if audio.output else AudioFormat())
    pending = b""
    async for chunk in source:
        chunk = pending + chunk
        width = source.audio_format.sample_width
        complete = len(chunk) - len(chunk) % width
        pending = chunk[complete:]
        if complete:
            await send({"type": "session.input_audio.append", "audio": base64.b64encode(chunk[:complete]).decode()})
    if pending:
        raise ValueError("Live PCM16 input ended with an incomplete sample")
    # EOF is not a Live turn boundary; keep its input timeline moving with silence.
    async for chunk in SilenceInput(source.audio_format):
        await send({"type": "session.input_audio.append", "audio": base64.b64encode(chunk).decode()})


async def receive(
    socket: aiohttp.ClientWebSocketResponse,
    speaker: AudioOutput,
    backend: ClientDelegations,
    on_event: Send,
    hosted: HostedTools | None = None,
) -> None:
    async for message in socket:
        if message.type != aiohttp.WSMsgType.TEXT:
            raise RuntimeError("Live transport ended without session.closed")
        event = object_value(json.loads(message.data))
        backend.updates.observe(event)
        kind = event.get("type")
        if kind == "session.output_audio.delta":
            await speaker.write(base64.b64decode(str(event["delta"]), validate=True))
            await on_event(event)
            continue
        backend.observe(event)
        if hosted is not None:
            hosted.observe(event)
        # Includes transcript fragments, append acknowledgments and the nested
        # response.event envelope (hosted backend lifecycle, output, and usage).
        await on_event(event)
        if kind == "session.closed":
            return  # This event contains FINAL voice usage, not backend usage.
    raise RuntimeError("Live transport ended without final session usage")


async def converse(
    config: Event,
    api_key: str,
    backend: Backend,
    audio: AudioDuplex,
    speaker: AudioOutput,
    on_event: Send,
    updates: _LiveUpdates,
    options: LiveConfig | None = None,
    hosted: HostedTools | None = None,
    provider: Provider | None = None,
) -> None:
    options = options or LiveConfig()
    suffix = (
        f"/{quote(options.attach_to or options.fork_from, safe='')}/" if options.attach_to or options.fork_from else ""
    )
    prefix = provider.live_endpoint(websocket=True) if provider is not None else LIVE_URL
    prefix = prefix.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
    url = prefix + suffix + ("attach" if options.attach_to else "fork" if options.fork_from else "")
    validate_endpoint(url, websocket=True)
    headers = await provider.auth_headers(url) if provider is not None else {"Authorization": f"Bearer {api_key}"}
    async with (
        aiohttp.ClientSession(trace_configs=[_no_redirects()], cookie_jar=aiohttp.DummyCookieJar()) as http,
        http.ws_connect(url, headers=headers, heartbeat=20) as socket,
    ):
        send_lock = asyncio.Lock()

        async def send(event: Event) -> None:
            async with send_lock:
                await socket.send_json(event)

        # Live uses session.start, not Realtime's session.update or a model
        # URL query. Nothing else may be sent until session.started arrives.
        if options.attach_to:
            started: Event = {"type": "connection.attached", "session": {"id": options.attach_to}}
        else:
            if options.fork_from:
                config = {key: value for key, value in config.items() if key == "store"}
                if options.backend_options:
                    config["delegation"] = {"type": "responses", "responses": options.backend_options}
                config["audio"] = {"format": audio_format(audio)}
            await send({"type": "session.start", "event_id": uuid4().hex, "session": config})
            async with asyncio.timeout(20):
                started = object_value(await socket.receive_json())
        if started.get("type") not in {"session.started", "connection.attached"}:
            await on_event(started)
            raise RuntimeError("Live did not start; inspect the startup event")
        delegations = ClientDelegations(backend, send)
        for index, message in enumerate(options.history):
            parts = message.get("content")
            assert isinstance(parts, list)
            delegations.fragments.append(
                {
                    "speaker": message["role"],
                    "text": parts[0]["text"],
                    "start_ms": index - len(options.history),
                    "end_ms": 0,
                }
            )
        if options.history:
            delegations.ready.set()
        delegations.updates = updates
        delegations.enabled = options.handle_delegations
        delegations.timeout = options.backend_timeout
        updates.sender = send
        updates.delegations = delegations.seen
        updates.responses = options.delegation == "responses"
        updates.observe(started)
        if not updates.responses and options.handle_delegations:
            updates.submit_input = delegations.submit
        elif hosted is not None:
            updates.submit_input = hosted.submit

        original_backend = delegations.backend

        async def dispatch(context: str, identifier: str) -> str:
            content = delegations.inputs.pop(identifier, None)
            if content is not None:
                updates.input_content = content
            try:
                return await original_backend(context, identifier)
            finally:
                updates.input_content = []

        delegations.backend = dispatch
        reader = asyncio.create_task(receive(socket, speaker, delegations, on_event, hosted))
        sender = asyncio.create_task(send_audio(audio, send, enabled=not bool(options.attach_to)))
        worker = asyncio.create_task(hosted.run() if hosted else delegations.run())
        try:
            await on_event(started)
            done, _ = await asyncio.wait((reader, sender, worker), return_when=asyncio.FIRST_COMPLETED)
            if reader in done:
                reader.result()  # A valid terminal event wins over a simultaneous sender disconnect.
            else:
                for task in done:
                    task.result()  # Surface audio/backend/transport failures.
        finally:
            updates.sender = None  # Stop commands, but let pending acknowledgments drain.
            sender.cancel()
            worker.cancel()
            await asyncio.gather(sender, worker, return_exceptions=True)
            try:
                if reader.done():
                    reader.result()
                elif options.close_session_on_exit:
                    logging.getLogger(__name__).info(
                        "Closing Live connection; waiting up to %.0fs for final session usage…", options.close_timeout
                    )
                    if not updates.closing:
                        await send({"type": "session.close", "event_id": uuid4().hex})
                    # Keep the existing receiver alive to collect final usage.
                    async with asyncio.timeout(options.close_timeout):
                        await asyncio.shield(reader)
                    logging.getLogger(__name__).info(
                        "Live finalized: %.2f voice seconds (%s).", updates.status.seconds, updates.status.reason
                    )
            except TimeoutError:
                logging.getLogger(__name__).warning(
                    "Live finalization is unconfirmed: no session.closed event arrived."
                )
                raise RuntimeError("Incomplete finalization: no session.closed event") from None
            finally:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
                updates.disconnect()


async def _ignore_event(event: Event) -> None:
    pass


class _LiveConnection:
    """Full-duplex GPT-Live with native delegation to an Agent or Responses.

    Pass an Agent to own backend inference, tools and context locally, or a
    ResponsesDelegation for OpenAI-managed inference. The caller owns the Agent
    lifetime. run() owns audio playback cleanup and the Live connection.
    on_event receives raw non-audio Live events, including final voice usage and
    nested hosted Responses events. This callback should not block the receiver.
    """

    def __init__(
        self,
        api_key: str,
        backend: Agent | ResponsesDelegation,
        *,
        model: str = "gpt-live-1",
        voice: str | dict[str, str] = "marin",
        instructions: str = "Be concise. Delegate reasoning and tool requests to the backend. Do not invent results.",
        on_event: Send = _ignore_event,
        on_backend_event: Emit,
        updates: _LiveUpdates,
        options: LiveConfig | None = None,
        provider: Provider | None = None,
    ) -> None:
        self.api_key = api_key
        self.provider = provider
        self.backend = backend
        self.on_event = on_event
        self.on_backend_event = on_backend_event
        self.updates = updates
        self.options = options or LiveConfig()
        self.backend_session_id = uuid4().hex
        self.hosted: HostedTools | None = None
        self.config: Event = {
            "model": model,
            "instructions": instructions,
            "audio": {"format": {"type": "audio/pcm", "rate": 24000}, "output": {"voice": voice}},
            "delegation": backend.to_dict() if isinstance(backend, ResponsesDelegation) else {"type": "client"},
        }

    async def run(self, audio: AudioDuplex) -> None:
        """Run until cancellation or server close; finalize before disconnecting."""
        formats = [adapter.audio_format for adapter in (audio.input, audio.output) if adapter is not None]
        if formats and any(fmt != formats[0] for fmt in formats):
            raise ValueError("Live input and output audio formats must match")
        for adapter in (audio.input, audio.output):
            if adapter is None:
                continue
            fmt = adapter.audio_format
            if fmt.channels != 1 or (fmt.encoding, fmt.sample_rate, fmt.sample_width) not in {
                ("audio/pcm", 24000, 2),
                ("audio/pcm", 16000, 2),
                ("audio/pcmu", 8000, 1),
                ("audio/pcma", 8000, 1),
            }:
                raise ValueError("Unsupported GPT-Live audio format")
        object_value(self.config["audio"])["format"] = audio_format(audio)

        async def backend(transcript: str, identifier: str) -> str:
            if not isinstance(self.backend, Agent):
                if self.options.client_handler:
                    return await self.options.client_handler(transcript)
                return "No backend capability is configured for this request."
            live_id = "" if identifier.startswith("application:") else identifier
            token = _delegation_scope.set(_DelegationScope(self.updates, self.backend, live_id))
            try:
                return await infer(
                    self.backend,
                    self.updates.input_content or transcript,
                    identifier,
                    self.on_backend_event,
                    session_id=self.backend_session_id,
                )
            finally:
                _delegation_scope.reset(token)

        speaker = audio.output or NullAudioOutput()
        try:
            await converse(
                self.config,
                self.api_key,
                backend,
                audio,
                speaker,
                self.on_event,
                self.updates,
                self.options,
                self.hosted,
                self.provider,
            )
        finally:
            await speaker.close()


async def run_live(
    agent: Agent, audio: AudioDuplex, *, log_file: Path | str | None = None
) -> AsyncGenerator[AgentEvent, None]:
    """Internal Agent.run() bridge: own the connection and stream normal events."""
    config = agent.provider.live_config
    assert config is not None
    if agent._live_updates is not None:
        raise RuntimeError("This agent already has an active GPT-Live run")
    backend: Agent | ResponsesDelegation
    if config.delegation == "client":
        if agent.delegation_agent is agent:
            raise ValueError("Client Live delegation requires a separate delegation_agent")
        backend = agent.delegation_agent or ResponsesDelegation("")
    else:
        if agent.delegation_agent is not None:
            raise ValueError("Select client mode for a delegation_agent")
        backend = ResponsesDelegation(config.backend_model, config.backend_instructions, config.web_search)
    queue: asyncio.Queue[AgentEvent | None] = asyncio.Queue(maxsize=config.event_queue_size)
    closing = False
    path = Path(log_file) if log_file else None
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)

    async def emit(event: AgentEvent) -> None:
        if not closing:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                raise BufferError(
                    "Live event consumer is not keeping up; connection stopped without dropping events"
                ) from None

    async def observe(event: Event) -> None:
        if path:
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        kind = str(event.get("type", ""))
        if kind == "session.input_transcript.delta":
            await emit(InputTranscriptDeltaEvent(delta=str(event["delta"]), extra=dict(event)))
        elif kind == "session.output_transcript.delta":
            await emit(AudioTranscriptDeltaEvent(delta=str(event["delta"]), extra=dict(event)))
        elif kind == "session.output_audio.delta":
            fmt = audio_format(audio)
            await emit(
                AudioChunkEvent(
                    chunk=str(event["delta"]),
                    format=str(fmt["type"]),
                    extra={
                        "sample_rate": fmt["rate"],
                        **{key: event[key] for key in ("start_ms", "end_ms") if key in event},
                    },
                )
            )
        else:
            await emit(LiveEvent(event_type=kind, payload=event))

    updates = _LiveUpdates()
    connection = _LiveConnection(
        agent.provider.api_key,
        backend,
        model=agent.provider.model,
        voice=config.voice,
        instructions=agent.system_prompt or "Be concise. Delegate reasoning and tool requests to the backend.",
        on_event=observe,
        on_backend_event=emit,
        updates=updates,
        options=config,
        provider=agent.provider,
    )
    connection.config = build_session(agent)
    if config.delegation == "responses" and config.handle_delegations:
        connection.hosted = HostedTools(agent, updates, emit)

    async def run() -> None:
        try:
            await connection.run(audio)
        finally:
            await queue.put(None)

    agent._live_updates = updates
    runner = asyncio.create_task(run())
    try:
        while (event := await queue.get()) is not None:
            yield event
        await runner
    finally:
        closing = True
        updates.sender = None
        while not queue.empty():
            queue.get_nowait()
        runner.cancel()
        try:
            await asyncio.gather(runner, return_exceptions=True)
        finally:
            if agent._live_updates is updates:
                agent._last_live_updates = updates
                agent._live_updates = None


def audio_format(audio: AudioDuplex) -> Event:
    adapter = audio.input or audio.output
    fmt = adapter.audio_format if adapter else AudioFormat()
    return {"type": fmt.encoding, "rate": fmt.sample_rate}


def build_session(agent: Agent, *, media: bool = False) -> Event:
    config = agent.provider.live_config
    if config is None:
        raise ValueError("The provider is not configured for GPT-Live")
    session = config.session(agent.provider.model, agent.system_prompt or "", media=media)
    if config.delegation == "responses":
        responses = object_value(object_value(session["delegation"])["responses"])
        tools = [
            {
                "type": "function",
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
                "strict": False,
            }
            for tool in agent.tool_registry.get_all()
        ]
        responses["tools"] = ([{"type": "web_search"}] if config.web_search else []) + tools
    return session
