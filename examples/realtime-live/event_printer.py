"""
Comprehensive event printer for the Realtime examples.

Prints every nagents event with a relative timestamp and relevant details:
audio chunk sizes and durations, transcripts, tool calls/results, usage,
rate limits, and round-trip latency. Used to see *everything* the session emits.
"""

import base64
import time

from nagents import AudioChunkEvent
from nagents import AudioTranscriptDeltaEvent
from nagents import DoneEvent
from nagents import ErrorEvent
from nagents import InputTranscriptCompletedEvent
from nagents import InputTranscriptDeltaEvent
from nagents import RealtimeRateLimitsEvent
from nagents import RealtimeRawEvent
from nagents import RealtimeSessionCreatedEvent
from nagents import RealtimeSessionUpdatedEvent
from nagents import ReasoningChunkEvent
from nagents import ResponseCancelledEvent
from nagents import ResponseCreatedEvent
from nagents import SpeechStartedEvent
from nagents import SpeechStoppedEvent
from nagents import TextChunkEvent
from nagents import ToolCallEvent
from nagents import ToolResultEvent


def start_timer() -> float:
    """Return a monotonic reference time for relative timestamps."""
    return time.monotonic()


def _ts(start: float) -> str:
    return f"[+{(time.monotonic() - start) * 1000:7.0f}ms]"


def _duration_ms(raw: bytes, sample_rate: int) -> float:
    return len(raw) / 2 / sample_rate * 1000


def print_event(event: object, start: float, sample_rate: int = 24000) -> None:
    """Print a single event with a relative timestamp and its key details."""
    ts = _ts(start)

    if isinstance(event, AudioChunkEvent):
        raw = base64.b64decode(event.chunk)
        print(f"{ts} audio          {len(raw):6d} bytes  ~{_duration_ms(raw, sample_rate):6.0f} ms")
    elif isinstance(event, AudioTranscriptDeltaEvent):
        print(event.delta, end="", flush=True)
    elif isinstance(event, TextChunkEvent):
        print(event.chunk, end="", flush=True)
    elif isinstance(event, ReasoningChunkEvent):
        print(f"\n{ts} reasoning      ", end="")
        print(event.chunk, end="", flush=True)
    elif isinstance(event, InputTranscriptDeltaEvent):
        print(f"\n{ts} input (partial) '{event.delta}'", end="", flush=True)
    elif isinstance(event, InputTranscriptCompletedEvent):
        print(f"\n{ts} input (final)   '{event.transcript}'")
    elif isinstance(event, SpeechStartedEvent):
        print(f"\n{ts} speech_started")
    elif isinstance(event, SpeechStoppedEvent):
        print(f"\n{ts} speech_stopped")
    elif isinstance(event, ResponseCreatedEvent):
        print(f"\n{ts} response_created")
    elif isinstance(event, ResponseCancelledEvent):
        print(f"\n{ts} response_cancelled")
    elif isinstance(event, RealtimeSessionCreatedEvent):
        print(f"{ts} session_created  id={event.session_id}")
    elif isinstance(event, RealtimeSessionUpdatedEvent):
        print(f"{ts} session_updated")
    elif isinstance(event, RealtimeRateLimitsEvent):
        for rl in event.rate_limits:
            print(
                f"{ts} rate_limit      {rl.get('name')}: {rl.get('remaining')}/{rl.get('limit')} "
                f"(reset in {rl.get('reset_seconds')}s)"
            )
    elif isinstance(event, ToolCallEvent):
        print(f"\n{ts} tool_call       {event.name}({event.arguments})")
    elif isinstance(event, ToolResultEvent):
        detail = str(event.result) if event.error is None else f"ERROR: {event.error}"
        print(f"{ts} tool_result     {event.name} -> {detail}")
    elif isinstance(event, DoneEvent):
        extra = event.extra
        print(
            f"\n{ts} done            text={event.final_text!r} "
            f"tokens={event.usage.total_tokens} "
            f"first_output={extra.get('time_to_first_output_ms')}ms "
            f"latency={extra.get('response_latency_ms')}ms"
        )
    elif isinstance(event, ErrorEvent):
        print(f"\n{ts} error           {event.message}")
    elif isinstance(event, RealtimeRawEvent):
        print(f"{ts} raw             {event.event_type}")
    else:
        print(f"{ts} event           {type(event).__name__}")
