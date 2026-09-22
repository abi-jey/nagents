"""
Comprehensive event printer for the Realtime examples.

Prints every nagents event with a relative timestamp and relevant details:
audio chunk sizes and durations, transcripts, tool calls/results, usage,
rate limits, and round-trip latency. Used to see *everything* the session emits.
"""

import atexit
import base64
import logging
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
from nagents.live import LiveEvent

try:
    from rich.logging import RichHandler

    logging.basicConfig(level=logging.INFO, format="%(message)s", handlers=[RichHandler(rich_tracebacks=True)])
except ImportError:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

logger = logging.getLogger(__name__)
_stream_kind = ""


def _finish_stream() -> None:
    """Finish a partial line before another event or ordinary log record."""
    global _stream_kind
    if _stream_kind:
        print(flush=True)
        _stream_kind = ""


class _FinishStreamBeforeLog(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        _finish_stream()
        return True


for _handler in logging.getLogger().handlers:
    _handler.addFilter(_FinishStreamBeforeLog())
atexit.register(_finish_stream)


def _append_stream(kind: str, chunk: str, start: float) -> None:
    """Append literal text; works in terminals and pipes without cursor escapes."""
    global _stream_kind
    if not chunk or not logger.isEnabledFor(logging.INFO):
        return
    if kind != _stream_kind:
        _finish_stream()
        print(f"{_ts(start)} {kind:14} ", end="", flush=True)
        _stream_kind = kind
    print(chunk, end="", flush=True)


def start_timer() -> float:
    """Return a monotonic reference time for relative timestamps."""
    _finish_stream()
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
        logger.debug(f"{ts} audio          {len(raw):6d} bytes  ~{_duration_ms(raw, sample_rate):6.0f} ms")
    elif isinstance(event, AudioTranscriptDeltaEvent):
        _append_stream("assistant", event.delta, start)
    elif isinstance(event, TextChunkEvent):
        _append_stream("text", event.chunk, start)
    elif isinstance(event, ReasoningChunkEvent):
        _append_stream("reasoning", event.chunk, start)
    elif isinstance(event, InputTranscriptDeltaEvent):
        _append_stream("input", event.delta, start)
    elif isinstance(event, InputTranscriptCompletedEvent):
        logger.info(f"\n{ts} input (final)   '{event.transcript}'")
    elif isinstance(event, SpeechStartedEvent):
        logger.info(f"\n{ts} speech_started")
    elif isinstance(event, SpeechStoppedEvent):
        logger.info(f"\n{ts} speech_stopped")
    elif isinstance(event, ResponseCreatedEvent):
        logger.info(f"\n{ts} response_created")
    elif isinstance(event, ResponseCancelledEvent):
        logger.info(f"\n{ts} response_cancelled")
    elif isinstance(event, RealtimeSessionCreatedEvent):
        logger.info(f"{ts} session_created  id={event.session_id}")
    elif isinstance(event, RealtimeSessionUpdatedEvent):
        logger.info(f"{ts} session_updated")
    elif isinstance(event, RealtimeRateLimitsEvent):
        for rl in event.rate_limits:
            logger.info(
                f"{ts} rate_limit      {rl.get('name')}: {rl.get('remaining')}/{rl.get('limit')} "
                f"(reset in {rl.get('reset_seconds')}s)"
            )
    elif isinstance(event, ToolCallEvent):
        logger.info(f"\n{ts} tool_call       {event.name}({event.arguments})")
    elif isinstance(event, ToolResultEvent):
        detail = str(event.result) if event.error is None else f"ERROR: {event.error}"
        logger.info(f"{ts} tool_result     {event.name} -> {detail}")
    elif isinstance(event, DoneEvent):
        extra = event.extra
        logger.info(
            f"\n{ts} done            text={event.final_text!r} "
            f"tokens={event.usage.total_tokens} "
            f"first_output={extra.get('time_to_first_output_ms')}ms "
            f"latency={extra.get('response_latency_ms')}ms"
        )
    elif isinstance(event, ErrorEvent):
        logger.info(f"\n{ts} error           {event.message}")
    elif isinstance(event, RealtimeRawEvent | LiveEvent):
        logger.info(f"{ts} raw             {event.event_type}")
    else:
        logger.info(f"{ts} event           {type(event).__name__}")
