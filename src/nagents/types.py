"""
Core types for the v2 LLM integration module.
"""

from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from typing import Any
from typing import Literal

# =============================================================================
# Multimodal Content Types
# =============================================================================


@dataclass
class TextContent:
    """Text content part."""

    text: str
    type: Literal["text"] = "text"


@dataclass
class ImageContent:
    """
    Image content part.

    Supports both URL-based and base64-encoded images.
    - For URL: Set `url` (can be a data URL like "data:image/jpeg;base64,...")
    - For base64: Set `base64_data` and `media_type`
    """

    type: Literal["image"] = "image"
    url: str | None = None  # URL or data URL
    base64_data: str | None = None  # Raw base64 data (without data URL prefix)
    media_type: str | None = None  # e.g., "image/jpeg", "image/png", "image/gif", "image/webp"
    detail: str | None = None  # OpenAI-specific: "auto", "low", "high"


@dataclass
class AudioContent:
    """
    Audio content part (OpenAI-specific for input).

    Used for audio input in OpenAI's GPT-4o and similar models.
    """

    base64_data: str
    format: str = "wav"  # "wav" or "mp3"
    type: Literal["audio"] = "audio"


@dataclass
class DocumentContent:
    """
    Document/PDF content part (Anthropic-specific).

    Used for PDF and document input in Claude models.
    """

    base64_data: str
    media_type: str = "application/pdf"
    title: str | None = None
    type: Literal["document"] = "document"


# Union type for all content parts
ContentPart = TextContent | ImageContent | AudioContent | DocumentContent


@dataclass
class ToolCall:
    """A function/tool call requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolDefinition:
    """Definition of a tool/function the model can call."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema
    func: Callable[..., Any] | None = None  # The actual callable (not sent to API)


@dataclass
class Message:
    """
    A message in a conversation.

    Content can be:
    - str: Simple text content (for backward compatibility)
    - list[ContentPart]: Multimodal content (text, images, audio, documents)
    - None: For assistant messages with only tool calls
    """

    role: Literal["system", "user", "assistant", "tool"]
    content: str | list[ContentPart] | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None  # For tool result messages
    name: str | None = None  # Tool name for tool results


@dataclass
class Usage:
    """Token usage statistics."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass
class GenerationConfig:
    """Configuration for generation."""

    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    stop: list[str] | None = None
    # Gemini-specific
    thinking_config: dict[str, Any] | None = None
