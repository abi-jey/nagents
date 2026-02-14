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

    # Completion events
    TEXT_DONE = "text_done"  # Final complete text

    # Tool events
    TOOL_CALL = "tool_call"  # Model wants to call a tool
    TOOL_RESULT = "tool_result"  # Tool execution completed

    # Meta events
    ERROR = "error"  # Error occurred
    DONE = "done"  # Generation complete


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
        session: Cumulative token usage across the entire session/run
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
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


@dataclass
class ToolCallEvent(Event):
    """Model requesting a tool call."""

    type: EventType = field(default=EventType.TOOL_CALL)
    id: str = ""
    name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)


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
class DoneEvent(Event):
    """Generation complete."""

    type: EventType = field(default=EventType.DONE)
    final_text: str = ""
    session_id: str | None = None
