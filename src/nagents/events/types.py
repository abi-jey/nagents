"""
Event types for the v2 LLM integration module.

Events are emitted during generation to provide visibility into the process.
"""

from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from enum import Enum
from typing import Any

# Type alias for tool result - can be any JSON-serializable value
ToolResultType = str | int | float | bool | None | list[Any] | dict[str, Any]


class EventType(Enum):
    """Types of events that can be emitted during generation."""

    # Streaming events
    TEXT_CHUNK = "text_chunk"  # Partial text during streaming
    REASONING_CHUNK = "reasoning_chunk"  # Partial reasoning/thinking during streaming

    # Audio events (Realtime speech-to-speech)
    AUDIO_CHUNK = "audio_chunk"  # Base64-encoded output audio delta
    AUDIO_TRANSCRIPT_DELTA = "audio_transcript_delta"  # Partial output audio transcript
    INPUT_TRANSCRIPT_DELTA = "input_transcript_delta"  # Partial input audio transcript
    INPUT_TRANSCRIPT_COMPLETED = "input_transcript_completed"  # Final input transcript
    SPEECH_STARTED = "speech_started"  # User started speaking (VAD)
    SPEECH_STOPPED = "speech_stopped"  # User stopped speaking (VAD)

    # Realtime session lifecycle events
    REALTIME_SESSION_CREATED = "realtime_session_created"  # Server session ready
    REALTIME_SESSION_UPDATED = "realtime_session_updated"  # Session config updated
    RESPONSE_CREATED = "response_created"  # Model started a new response
    RESPONSE_CANCELLED = "response_cancelled"  # In-progress response was cancelled
    REALTIME_RATE_LIMITS = "realtime_rate_limits"  # Rate limit / usage counters
    REALTIME_RAW = "realtime_raw"  # Unmapped server event (raw passthrough)

    # Completion events
    TEXT_DONE = "text_done"  # Final complete text

    # Tool events
    TOOL_CALL = "tool_call"  # Model wants to call a tool
    TOOL_RESULT = "tool_result"  # Tool execution completed

    # Compaction events
    COMPACTION_STARTED = "compaction_started"  # Context compaction started
    COMPACTION_DONE = "compaction_done"  # Context compaction completed

    # Meta events
    ERROR = "error"  # Error occurred
    RATE_LIMIT = "rate_limit"  # Rate limit hit, retrying
    DONE = "done"  # Generation complete


class FinishReason(Enum):
    """Reason why the model stopped generating."""

    STOP = "stop"  # Natural stop or stop sequence
    TOOL_CALLS = "tool_calls"  # Model called tools
    LENGTH = "length"  # Max tokens reached
    CONTENT_FILTER = "content_filter"  # Content policy violation (Azure)
    NULL = "null"  # Still generating (streaming)
    UNKNOWN = "unknown"  # Fallback/unknown reason


@dataclass
class TokenUsage:
    """Token counts for a generation or session."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class Usage:
    """
    Token usage statistics with current generation and session totals.

    Attributes:
        prompt_tokens: Input tokens for current generation
        completion_tokens: Output tokens for current generation
        total_tokens: Total tokens for current generation
        cached_tokens: Cached prompt tokens (OpenAI)
        audio_tokens: Audio input/output tokens
        reasoning_tokens: Tokens used for reasoning/thinking (chain-of-thought models)
        session: Cumulative token usage across the entire session/run
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    audio_tokens: int = 0
    reasoning_tokens: int = 0
    session: TokenUsage | None = None

    def has_usage(self) -> bool:
        """Check if this usage has any actual token counts."""
        return self.prompt_tokens > 0 or self.completion_tokens > 0 or self.total_tokens > 0


@dataclass
class Event:
    """Base event with common fields."""

    type: EventType
    timestamp: datetime = field(default_factory=datetime.now)
    usage: Usage = field(default_factory=Usage)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class TextChunkEvent(Event):
    """Streaming text chunk."""

    type: EventType = field(default=EventType.TEXT_CHUNK)
    chunk: str = ""


@dataclass
class ReasoningChunkEvent(Event):
    """Streaming reasoning/thinking chunk (e.g., from models with chain-of-thought)."""

    type: EventType = field(default=EventType.REASONING_CHUNK)
    chunk: str = ""


@dataclass
class TextDoneEvent(Event):
    """Complete text response."""

    type: EventType = field(default=EventType.TEXT_DONE)
    text: str = ""
    finish_reason: FinishReason = FinishReason.STOP


@dataclass
class AudioChunkEvent(Event):
    """Streaming output audio chunk from a Realtime speech-to-speech session.

    Attributes:
        chunk: Base64-encoded audio bytes (PCM16 by default).
        format: Audio format of the chunk (e.g. "pcm16").
    """

    type: EventType = field(default=EventType.AUDIO_CHUNK)
    chunk: str = ""
    format: str = "pcm16"


@dataclass
class AudioTranscriptDeltaEvent(Event):
    """Streaming transcript delta for model-generated audio.

    Emitted as the model speaks, providing a live text rendering of its
    spoken output.
    """

    type: EventType = field(default=EventType.AUDIO_TRANSCRIPT_DELTA)
    delta: str = ""


@dataclass
class InputTranscriptDeltaEvent(Event):
    """Streaming transcript delta for user input audio.

    Emitted as the user speaks, providing a live text rendering of their
    speech (requires input transcription to be enabled).
    """

    type: EventType = field(default=EventType.INPUT_TRANSCRIPT_DELTA)
    delta: str = ""


@dataclass
class InputTranscriptCompletedEvent(Event):
    """Final recognized transcript for a committed user audio turn.

    Attributes:
        transcript: The full transcribed text of the user's utterance.
    """

    type: EventType = field(default=EventType.INPUT_TRANSCRIPT_COMPLETED)
    transcript: str = ""


@dataclass
class SpeechStartedEvent(Event):
    """Voice activity detection detected the start of user speech."""

    type: EventType = field(default=EventType.SPEECH_STARTED)


@dataclass
class SpeechStoppedEvent(Event):
    """Voice activity detection detected the end of user speech."""

    type: EventType = field(default=EventType.SPEECH_STOPPED)


@dataclass
class RealtimeSessionCreatedEvent(Event):
    """A Realtime session has been created on the server.

    Attributes:
        session_id: The server-assigned session identifier.
    """

    type: EventType = field(default=EventType.REALTIME_SESSION_CREATED)
    session_id: str = ""


@dataclass
class RealtimeSessionUpdatedEvent(Event):
    """The Realtime session configuration was updated."""

    type: EventType = field(default=EventType.REALTIME_SESSION_UPDATED)


@dataclass
class ResponseCancelledEvent(Event):
    """The in-progress model response was cancelled (e.g. user interruption)."""

    type: EventType = field(default=EventType.RESPONSE_CANCELLED)


@dataclass
class ResponseCreatedEvent(Event):
    """The model started generating a new response."""

    type: EventType = field(default=EventType.RESPONSE_CREATED)


@dataclass
class RealtimeRateLimitsEvent(Event):
    """Rate-limit and usage counters reported by the Realtime API.

    Attributes:
        rate_limits: Raw ``rate_limits`` array from the server, where each
            entry has ``name``, ``limit``, ``remaining``, and ``reset_seconds``.
    """

    type: EventType = field(default=EventType.REALTIME_RATE_LIMITS)
    rate_limits: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class RealtimeRawEvent(Event):
    """A server event the client does not explicitly model, passed through raw.

    Useful for debugging and for reacting to events not yet covered by the
    client (e.g. new server features). The full payload is preserved.

    Attributes:
        event_type: The raw server event type string.
        payload: The full raw event payload.
    """

    type: EventType = field(default=EventType.REALTIME_RAW)
    event_type: str = ""
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCallEvent(Event):
    """Model requesting a tool call."""

    type: EventType = field(default=EventType.TOOL_CALL)
    id: str = ""
    name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    finish_reason: FinishReason = FinishReason.TOOL_CALLS
    # Provider-specific metadata (e.g., Gemini thought signatures)
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass
class ToolResultEvent(Event):
    """Result from tool execution."""

    type: EventType = field(default=EventType.TOOL_RESULT)
    id: str = ""
    name: str = ""
    result: ToolResultType = None
    error: str | None = None
    duration_ms: float = 0.0


@dataclass
class ErrorEvent(Event):
    """Error during generation."""

    type: EventType = field(default=EventType.ERROR)
    message: str = ""
    code: str | None = None
    recoverable: bool = False


@dataclass
class RateLimitEvent(Event):
    """Rate limit hit during generation, automatic retry in progress.

    Emitted each time a retryable HTTP error (429 or 5xx) is encountered
    and the provider will retry after a delay.
    """

    type: EventType = field(default=EventType.RATE_LIMIT)
    attempt: int = 0  # Current retry attempt (1-based)
    max_retries: int = 0  # Maximum retry attempts configured
    retry_after: float = 0.0  # Delay in seconds before retry
    status_code: int = 0  # HTTP status code (429, 500, etc.)
    message: str = ""  # Human-readable message
    rate_limit_info: dict[str, str] = field(default_factory=dict)  # x-ratelimit-* headers


@dataclass
class DoneEvent(Event):
    """Generation complete."""

    type: EventType = field(default=EventType.DONE)
    final_text: str = ""
    session_id: str | None = None
    finish_reason: FinishReason = FinishReason.STOP


@dataclass
class CompactionStartedEvent(Event):
    """Event emitted when compaction begins.

    Attributes:
        trigger: The trigger that caused compaction (Tokens or Messages)
        message_count: Number of messages before compaction
        estimated_tokens: Estimated token count before compaction
        session_id: Original session ID
        compaction_session_id: Session ID used for compaction
    """

    type: EventType = field(default=EventType.COMPACTION_STARTED)
    trigger: Any = None  # Tokens | Messages - using Any to avoid circular import
    message_count: int = 0
    estimated_tokens: int = 0
    session_id: str = ""
    compaction_session_id: str = ""


@dataclass
class CompactionDoneEvent(Event):
    """Event emitted when compaction completes.

    Attributes:
        original_message_count: Number of messages before compaction
        original_token_count: Estimated tokens before compaction
        new_message_count: Number of messages after compaction
        summary_tokens: Estimated tokens in the summary
        summary_text: Full summary text
        trigger: The trigger that caused compaction
        compactor_used: Model name used for compaction ("self" or model name)
        session_id: Original session ID
        compaction_session_id: Session ID used for compaction
    """

    type: EventType = field(default=EventType.COMPACTION_DONE)
    original_message_count: int = 0
    original_token_count: int = 0
    new_message_count: int = 0
    summary_tokens: int = 0
    summary_text: str = ""
    trigger: Any = None  # Tokens | Messages - using Any to avoid circular import
    compactor_used: str = ""
    session_id: str = ""
    compaction_session_id: str = ""
