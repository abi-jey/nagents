"""
OpenAI Realtime (speech-to-speech) client over WebSocket.

Implements the client side of the OpenAI Realtime API protocol so that a
``RealtimeSession`` can run low-latency voice conversations with a
speech-to-speech model (e.g. ``gpt-realtime-2.1``) directly from Python,
without an intermediate speech-to-text or text-to-speech step.

The session maps server events onto the standard nagents event stream
(:class:`~nagents.events.AudioChunkEvent`, :class:`~nagents.events.ToolCallEvent`,
etc.) and handles tool calling automatically, mirroring the behavior of the
text-based :class:`~nagents.Agent`.

Server-to-server usage::

    from nagents.realtime import RealtimeSession

    def get_weather(city: str) -> str:
        '''Get the current weather for a city.'''
        return f"Weather in {city}: 20C, sunny"

    async with RealtimeSession(
        api_key="sk-...",
        instructions="You are a helpful voice assistant.",
        tools=[get_weather],
    ) as session:
        # Stream audio in from a producer task:
        await session.append_audio(base64_pcm_chunk)

        # Consume events:
        async for event in session:
            if isinstance(event, AudioChunkEvent):
                play(event.chunk)
            elif isinstance(event, DoneEvent):
                print(event.final_text)

Audio is exchanged as base64-encoded PCM16 chunks over the WebSocket. Voice
activity detection (VAD) is on by default, so the server decides when the user
has finished speaking and responds automatically.
"""

import asyncio
import base64
import json
import logging
import time
from collections.abc import AsyncIterator
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import aiohttp

from ..adapters.openai import format_realtime_tools
from ..events import AudioChunkEvent
from ..events import AudioTranscriptDeltaEvent
from ..events import DoneEvent
from ..events import ErrorEvent
from ..events import Event
from ..events import FinishReason
from ..events import InputTranscriptCompletedEvent
from ..events import InputTranscriptDeltaEvent
from ..events import RealtimeRateLimitsEvent
from ..events import RealtimeRawEvent
from ..events import RealtimeSessionCreatedEvent
from ..events import RealtimeSessionUpdatedEvent
from ..events import ReasoningChunkEvent
from ..events import ResponseCancelledEvent
from ..events import ResponseCreatedEvent
from ..events import SpeechStartedEvent
from ..events import SpeechStoppedEvent
from ..events import TextChunkEvent
from ..events import ToolCallEvent
from ..events import Usage
from ..tools import ToolExecutor
from ..tools import ToolRegistry
from ..types import ToolCall
from ..types import ToolDefinition
from .audio import AudioInput
from .audio import AudioOutput
from .types import DEFAULT_REALTIME_MODEL
from .types import RealtimeConfig

logger = logging.getLogger(__name__)

# Server events that carry no information the client needs to act on.
_NOOP_EVENT_TYPES = frozenset(
    {
        "conversation.created",
        "conversation.item.created",
        "input_audio_buffer.committed",
    }
)


class RealtimeSession:
    """
    A live speech-to-speech conversation with a Realtime model over WebSocket.

    Use as an async context manager (``async with``) to connect and disconnect
    automatically, or call :meth:`connect` and :meth:`close` explicitly.

    The session is designed to be used concurrently: stream audio in with
    :meth:`append_audio` from one task while iterating server events from
    another. Tool calls are executed inline using the registered tools.
    """

    def __init__(
        self,
        api_key: str,
        model: str | None = None,
        instructions: str = "",
        voice: str = "marin",
        tools: list[Callable[..., Any]] | None = None,
        config: RealtimeConfig | None = None,
        audio_in: AudioInput | None = None,
        audio_out: AudioOutput | None = None,
        base_url: str = "wss://api.openai.com/v1/realtime",
        heartbeat: float | None = None,
        log_file: str | Path | None = None,
    ):
        """
        Initialize a Realtime session.

        Args:
            api_key: OpenAI API key.
            model: Realtime model identifier. If omitted, falls back to
                ``config.model`` and then :data:`DEFAULT_REALTIME_MODEL`.
            instructions: System instructions for the voice agent.
            voice: Output voice. One of alloy, ash, ballad, coral, echo, sage,
                shimmer, verse, marin, cedar.
            tools: Optional tool functions to register for function calling.
            config: Optional full :class:`RealtimeConfig`. When provided, its
                values take precedence over the individual keyword arguments.
            audio_in: Optional :class:`AudioInput` adapter. Its
                :attr:`audio_format` configures the session's input audio.
            audio_out: Optional :class:`AudioOutput` adapter. Its
                :attr:`audio_format` configures the session's output audio.
            base_url: WebSocket endpoint (defaults to OpenAI).
            heartbeat: Optional aiohttp WebSocket heartbeat interval in seconds.
            log_file: Optional path to a JSON-lines log file. When set, every
                raw client/server event is recorded with a monotonic millisecond
                timestamp so round-trip latency can be measured after the fact.
        """
        self._api_key = api_key
        self._model = model
        self._instructions = instructions
        self._voice = voice
        self._base_url = base_url
        self._heartbeat = heartbeat

        self._config = config or RealtimeConfig(
            model=model or DEFAULT_REALTIME_MODEL,
            instructions=instructions,
            voice=voice,
        )
        if self._config.model is None:
            self._config.model = model or DEFAULT_REALTIME_MODEL

        self._audio_in = audio_in
        self._audio_out = audio_out

        self._tool_registry = ToolRegistry()
        if tools:
            for tool in tools:
                self._tool_registry.register(tool)
        self._tool_executor = ToolExecutor(self._tool_registry)

        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._send_lock = asyncio.Lock()
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._session_id: str = ""
        self._response_text: str = ""
        self._connected: bool = False

        # Event log (JSON-lines) and latency tracking.
        self._log_path = Path(log_file) if log_file else None
        self._log_fh: Any = None
        self._log_lock = asyncio.Lock()
        self._log_start: float = 0.0
        self._response_create_ts: float | None = None
        self._first_output_ts: float | None = None

    @property
    def session_id(self) -> str:
        """The server-assigned Realtime session identifier."""
        return self._session_id

    @property
    def is_connected(self) -> bool:
        """Whether the WebSocket connection is currently open."""
        return self._connected

    @property
    def tool_names(self) -> list[str]:
        """Names of the tools registered on this session."""
        return self._tool_registry.names()

    def register_tool(
        self,
        func: Callable[..., Any],
        name: str | None = None,
        description: str | None = None,
    ) -> ToolDefinition:
        """Register a tool function for function calling."""
        return self._tool_registry.register(func, name, description)

    def _audio_formats(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return (input_format, output_format) dicts for the session.

        Prefers the adapters' :attr:`AudioFormat` (which codify the I/O
        quality), falling back to the session config.
        """
        config = self._config

        if self._audio_in is not None:
            in_fmt = self._audio_in.audio_format
            input_format: dict[str, Any] = {"type": in_fmt.encoding, "rate": in_fmt.sample_rate}
        else:
            input_format = {"type": config.input_audio_format, "rate": config.input_audio_rate}

        if self._audio_out is not None:
            out_fmt = self._audio_out.audio_format
            output_format: dict[str, Any] = {"type": out_fmt.encoding, "rate": out_fmt.sample_rate}
        else:
            output_format = {"type": config.output_audio_format, "rate": config.output_audio_rate}

        return input_format, output_format

    def _build_session(self) -> dict[str, Any]:
        """Build the GA ``session`` object sent in ``session.update``."""
        config = self._config
        input_format, output_format = self._audio_formats()

        session: dict[str, Any] = {
            "type": "realtime",
            "model": config.model or DEFAULT_REALTIME_MODEL,
            "output_modalities": config.output_modalities,
            "audio": {
                "input": {
                    "format": input_format,
                    "turn_detection": ({"type": config.turn_detection_type} if config.turn_detection else None),
                },
                "output": {
                    "format": output_format,
                    "voice": config.voice,
                },
            },
        }

        if config.instructions:
            session["instructions"] = config.instructions

        if config.input_transcription_model is not None:
            session["audio"]["input"]["transcription"] = {"model": config.input_transcription_model}

        if config.max_output_tokens is not None:
            session["max_output_tokens"] = config.max_output_tokens

        if config.reasoning_effort is not None:
            session["reasoning"] = {"effort": config.reasoning_effort}

        tools = self._tool_registry.get_all()
        if tools:
            session["tools"] = format_realtime_tools(tools)
            session["tool_choice"] = config.tool_choice
            if config.parallel_tool_calls is not None:
                session["parallel_tool_calls"] = config.parallel_tool_calls

        return session

    async def connect(self) -> None:
        """Open the WebSocket, wait for the session, and apply configuration."""
        if self._connected:
            return

        self._session = aiohttp.ClientSession()
        url = f"{self._base_url}?{urlencode({'model': self._config.model or DEFAULT_REALTIME_MODEL})}"
        headers = {"Authorization": f"Bearer {self._api_key}"}

        # Open the event log before any traffic flows.
        if self._log_path is not None:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_fh = self._log_path.open("a", encoding="utf-8")
            self._log_start = time.monotonic()

        self._ws = await self._session.ws_connect(url, headers=headers, heartbeat=self._heartbeat)

        # Wait for session.created, buffering any events that arrive first.
        assert self._ws is not None
        while True:
            msg = await self._ws.receive()
            if msg.type == aiohttp.WSMsgType.TEXT:
                raw = json.loads(msg.data)
                await self._log("recv", raw)
                self._queue.put_nowait(raw)
                if raw.get("type") == "session.created":
                    self._session_id = raw.get("session", {}).get("id", "")
                    break
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                raise RuntimeError("Realtime WebSocket closed before the session was created")

        # Start reading before sending the update so session.updated is captured.
        self._reader_task = asyncio.create_task(self._read_loop())
        self._connected = True

        await self._send({"type": "session.update", "session": self._build_session()})
        logger.info(f"Realtime session connected: {self._session_id} (model={self._config.model})")

    async def _log(self, direction: str, event: dict[str, Any]) -> None:
        """Append a raw event to the JSON-lines log with a relative timestamp."""
        if self._log_fh is None:
            return
        async with self._log_lock:
            entry = {
                "t_ms": round((time.monotonic() - self._log_start) * 1000, 2),
                "direction": direction,
                "event": event,
            }
            self._log_fh.write(json.dumps(entry) + "\n")
            self._log_fh.flush()

    async def _read_loop(self) -> None:
        """Read raw server events and push them onto the incoming queue."""
        assert self._ws is not None
        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        raw = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue
                    await self._log("recv", raw)
                    await self._queue.put(raw)
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
        except (aiohttp.ClientError, asyncio.CancelledError):
            pass
        finally:
            await self._queue.put(None)

    async def _send(self, event: dict[str, Any]) -> None:
        """Send a JSON client event over the WebSocket."""
        if self._ws is None or self._ws.closed:
            raise RuntimeError("Realtime session is not connected")
        if event.get("type") == "response.create":
            self._response_create_ts = time.monotonic()
            self._first_output_ts = None
        await self._log("send", event)
        async with self._send_lock:
            await self._ws.send_str(json.dumps(event))

    async def append_audio(self, audio_base64: str) -> None:
        """
        Stream a chunk of audio input to the model.

        Args:
            audio_base64: Base64-encoded PCM audio chunk (up to 15 MB).
        """
        await self._send({"type": "input_audio_buffer.append", "audio": audio_base64})

    async def commit_audio(self) -> None:
        """Commit the buffered input audio (used when VAD is disabled)."""
        await self._send({"type": "input_audio_buffer.commit"})

    async def clear_audio(self) -> None:
        """Clear the input audio buffer."""
        await self._send({"type": "input_audio_buffer.clear"})

    async def send_text(self, text: str) -> None:
        """Send a text message and request a response."""
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        )
        await self._send({"type": "response.create"})

    async def create_response(self) -> None:
        """Request a response from the model (used when VAD is disabled)."""
        await self._send({"type": "response.create"})

    async def cancel_response(self) -> None:
        """Cancel the in-progress response (e.g. on user interruption)."""
        await self._send({"type": "response.cancel"})

    async def update_instructions(self, instructions: str) -> None:
        """Update the system instructions for the session."""
        self._config.instructions = instructions
        await self._send({"type": "session.update", "session": {"type": "realtime", "instructions": instructions}})

    async def run_duplex(
        self,
        audio_in: AudioInput | None = None,
        audio_out: AudioOutput | None = None,
        *,
        auto_commit: bool | None = None,
    ) -> AsyncIterator[Event]:
        """
        Run a full-duplex voice conversation between two audio adapters.

        Audio from ``audio_in`` is streamed into the model, and model audio is
        streamed out to ``audio_out``. Non-audio events (transcripts, tool
        calls/results, speech detection, errors) are yielded to the caller.

        Either end may be ``None`` to disable it. If ``audio_in`` is ``None``,
        no audio is captured (responses are driven externally, e.g. by
        :meth:`send_text`). If ``audio_out`` is ``None``, model audio is
        discarded and only transcripts are yielded.

        The adapters' :attr:`AudioFormat` is applied to the session before the
        loop starts, so the session's input/output codecs and sample rates
        match what the adapters produce/expect.

        Args:
            audio_in: :class:`AudioInput` to read user audio from. Defaults to
                the adapter passed to the constructor.
            audio_out: :class:`AudioOutput` to write model audio to. Defaults
                to the adapter passed to the constructor.
            auto_commit: When True, commits the input buffer and requests a
                response once ``audio_in`` is exhausted (push-to-talk). When
                False, never auto-commits (live streaming with VAD). Defaults
                to the inverse of ``turn_detection`` (i.e. auto-commit when
                VAD is disabled).

        Yields:
            Non-audio events (e.g. :class:`~nagents.events.AudioTranscriptDeltaEvent`,
            :class:`~nagents.events.ToolCallEvent`, :class:`~nagents.events.DoneEvent`).

        Raises:
            ValueError: If both input and output adapters are unavailable.
        """
        if audio_in is not None:
            self._audio_in = audio_in
        if audio_out is not None:
            self._audio_out = audio_out

        source = self._audio_in
        sink = self._audio_out
        if source is None and sink is None:
            raise ValueError("run_duplex requires at least one of AudioInput/AudioOutput")

        if auto_commit is None:
            auto_commit = not self._config.turn_detection

        # Apply the adapters' audio format to the session. Only the audio
        # format is updated here so the loop is safe to call multiple times.
        input_format, output_format = self._audio_formats()
        await self._send(
            {
                "type": "session.update",
                "session": {
                    "type": "realtime",
                    "audio": {
                        "input": {"format": input_format},
                        "output": {"format": output_format},
                    },
                },
            }
        )

        input_exhausted = False
        producer_error: Exception | None = None

        async def _produce() -> None:
            nonlocal input_exhausted, producer_error
            assert source is not None
            try:
                async for chunk in source:
                    await self.append_audio(base64.b64encode(chunk).decode("ascii"))
                input_exhausted = True
                if auto_commit:
                    await self.commit_audio()
                    await self.create_response()
            except Exception as exc:
                producer_error = exc

        producer: asyncio.Task[None] | None = asyncio.create_task(_produce()) if source is not None else None

        # When the user interrupts, drop buffered audio and ignore any stale
        # audio deltas from the cancelled response until a new response starts.
        muted = False

        try:
            async for event in self.events():
                if producer_error is not None:
                    raise producer_error
                if isinstance(event, AudioChunkEvent):
                    if sink is not None and not muted:
                        await sink.write(base64.b64decode(event.chunk))
                    continue
                if isinstance(event, (SpeechStartedEvent, ResponseCancelledEvent)):
                    muted = True
                    if sink is not None:
                        await sink.interrupt()
                elif isinstance(event, ResponseCreatedEvent):
                    muted = False
                yield event
                if (
                    input_exhausted
                    and isinstance(event, DoneEvent)
                    and event.finish_reason is not FinishReason.TOOL_CALLS
                ):
                    break
        finally:
            if producer is not None and not producer.done():
                producer.cancel()
            if producer is not None:
                with suppress(asyncio.CancelledError):
                    await producer
            if sink is not None:
                await sink.close()

    @staticmethod
    def _parse_usage(response: dict[str, Any]) -> Usage:
        """Extract usage from a ``response.done`` payload."""
        usage = response.get("usage") or {}
        input_details = usage.get("input_token_details") or {}
        output_details = usage.get("output_token_details") or {}
        return Usage(
            prompt_tokens=usage.get("input_tokens", 0),
            completion_tokens=usage.get("output_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
            audio_tokens=input_details.get("audio_tokens", 0) + output_details.get("audio_tokens", 0),
            reasoning_tokens=output_details.get("reasoning_tokens", 0),
        )

    async def _handle_response_done(self, raw: dict[str, Any]) -> list[Event]:
        """Handle a ``response.done`` event, executing any function calls."""
        response = raw.get("response") or {}
        usage = self._parse_usage(response)
        events: list[Event] = []

        output = response.get("output") or []
        # Validate the entire batch before any tool can produce side effects.
        if response.get("status") != "completed" or any(
            item.get("status") != "completed" for item in output if item.get("type") == "function_call"
        ):
            if response.get("status") == "cancelled":
                events.append(ResponseCancelledEvent())
            else:
                events.append(ErrorEvent(message="Realtime response or tool call did not complete"))
            events.append(
                DoneEvent(
                    final_text=self._response_text,
                    session_id=self._session_id,
                    finish_reason=FinishReason.UNKNOWN,
                    usage=usage,
                    extra=self._latency_extra(),
                )
            )
            return events

        has_tool_calls = False

        for item in output:
            if item.get("type") != "function_call":
                continue

            name = item.get("name", "")
            call_id = item.get("call_id", "")
            arguments_raw = item.get("arguments", "{}")
            try:
                arguments: dict[str, Any] = (
                    json.loads(arguments_raw) if isinstance(arguments_raw, str) else arguments_raw
                )
            except json.JSONDecodeError:
                arguments = {"raw": arguments_raw}

            events.append(ToolCallEvent(id=call_id, name=name, arguments=arguments, usage=usage))

            result_event = await self._tool_executor.execute(ToolCall(id=call_id, name=name, arguments=arguments))
            result_event.usage = usage
            events.append(result_event)

            if result_event.error is not None:
                output_str = json.dumps({"error": result_event.error})
            else:
                output_str = str(result_event.result)

            await self._send(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": output_str,
                    },
                }
            )
            has_tool_calls = True

        if has_tool_calls:
            await self._send({"type": "response.create"})

        events.append(
            DoneEvent(
                final_text=self._response_text,
                session_id=self._session_id,
                finish_reason=FinishReason.TOOL_CALLS if has_tool_calls else FinishReason.STOP,
                usage=usage,
                extra=self._latency_extra(),
            )
        )
        return events

    async def _handle_server_event(self, raw: dict[str, Any]) -> list[Event]:
        """Translate a raw server event into zero or more nagents events."""
        etype = raw.get("type")
        if etype in _NOOP_EVENT_TYPES:
            return []

        events: list[Event] = []

        if etype == "session.created":
            self._session_id = raw.get("session", {}).get("id", "")
            events.append(RealtimeSessionCreatedEvent(session_id=self._session_id))
        elif etype == "session.updated":
            events.append(RealtimeSessionUpdatedEvent())
        elif etype == "input_audio_buffer.speech_started":
            events.append(SpeechStartedEvent())
        elif etype == "input_audio_buffer.speech_stopped":
            events.append(SpeechStoppedEvent())
        elif etype == "conversation.item.input_audio_transcription.delta":
            events.append(InputTranscriptDeltaEvent(delta=raw.get("delta", "")))
        elif etype == "conversation.item.input_audio_transcription.completed":
            events.append(InputTranscriptCompletedEvent(transcript=raw.get("transcript", "")))
        elif etype in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
            events.append(ReasoningChunkEvent(chunk=raw.get("delta", "")))
        elif etype == "response.created":
            self._response_text = ""
            events.append(ResponseCreatedEvent())
        elif etype == "response.output_audio.delta":
            self._note_first_output()
            events.append(AudioChunkEvent(chunk=raw.get("delta", ""), format="pcm16"))
        elif etype == "response.output_audio_transcript.delta":
            delta = raw.get("delta", "")
            self._response_text += delta
            events.append(AudioTranscriptDeltaEvent(delta=delta))
        elif etype == "response.output_text.delta":
            self._note_first_output()
            delta = raw.get("delta", "")
            self._response_text += delta
            events.append(TextChunkEvent(chunk=delta))
        elif etype == "response.done":
            events.extend(await self._handle_response_done(raw))
        elif etype == "response.cancelled":
            events.append(ResponseCancelledEvent())
        elif etype == "rate_limits.updated":
            events.append(RealtimeRateLimitsEvent(rate_limits=raw.get("rate_limits", [])))
        elif etype == "error":
            events.append(
                ErrorEvent(
                    message=raw.get("message", "Realtime API error"),
                    code=raw.get("code"),
                    recoverable=False,
                )
            )
        else:
            events.append(RealtimeRawEvent(event_type=etype or "", payload=raw))

        return events

    def _note_first_output(self) -> None:
        """Record when the first output chunk arrives, for latency tracking."""
        if self._first_output_ts is None and self._response_create_ts is not None:
            self._first_output_ts = time.monotonic()

    def _latency_extra(self) -> dict[str, float]:
        """Compute round-trip latency stats for the current response."""
        extra: dict[str, float] = {}
        if self._response_create_ts is not None:
            extra["response_latency_ms"] = round((time.monotonic() - self._response_create_ts) * 1000, 1)
        if self._first_output_ts is not None and self._response_create_ts is not None:
            extra["time_to_first_output_ms"] = round((self._first_output_ts - self._response_create_ts) * 1000, 1)
        return extra

    async def events(self) -> AsyncIterator[Event]:
        """Iterate over server events, translated to nagents events.

        Blocks until the session is closed (``None`` sentinel) or the
        connection drops.
        """
        while True:
            raw = await self._queue.get()
            if raw is None:
                break
            for event in await self._handle_server_event(raw):
                yield event

    def __aiter__(self) -> AsyncIterator[Event]:
        return self.events()

    async def close(self) -> None:
        """Close the WebSocket and release resources."""
        if self._reader_task is not None:
            self._reader_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reader_task
            self._reader_task = None

        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        self._ws = None

        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

        if self._log_fh is not None:
            self._log_fh.close()
            self._log_fh = None

        self._connected = False
        logger.info("Realtime session closed")

    async def __aenter__(self) -> "RealtimeSession":
        await self.connect()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()
