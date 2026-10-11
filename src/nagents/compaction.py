"""Context compaction utilities for managing conversation history.

This module provides utility functions for token estimation and context limit detection.
ModelCompaction is the built-in provider-backed CompactionStrategy.
"""

import logging
from contextlib import aclosing
from typing import TYPE_CHECKING
from typing import cast

from .types import ContentPart
from .types import Message
from .types import TextContent

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from .events import Event
    from .extensions import CompactionRequest
    from .extensions import CompactionResult
    from .provider import Provider

logger = logging.getLogger(__name__)

# Default context limit for unknown models
DEFAULT_CONTEXT_LIMIT = 200000

# Approximate characters per token (GPT-style tokenization)
CHARS_PER_TOKEN = 4

# Maximum tool result characters to keep in context before truncation
MAX_TOOL_RESULT_CHARS = 10000
DEFAULT_COMPACTION_PROMPT = (
    "Summarize the conversation so far for continuity. Preserve user goals, decisions, "
    "important facts, constraints, unresolved questions, and relevant tool results. "
    "Be concise and do not invent details. Return only the summary."
)


class ModelCompaction:
    """Summarize active history with a provider when its context budget is reached."""

    def __init__(
        self,
        token_threshold: int | None = None,
        message_threshold: int | None = None,
        prompt: str | None = None,
        provider: "Provider | None" = None,
    ) -> None:
        if token_threshold is not None and (type(token_threshold) is not int or token_threshold <= 0):
            raise ValueError("token_threshold must be a positive integer")
        if message_threshold is not None and (type(message_threshold) is not int or message_threshold <= 0):
            raise ValueError("message_threshold must be a positive integer")
        if token_threshold is not None and message_threshold is not None:
            raise ValueError("token_threshold and message_threshold are mutually exclusive")
        self.token_threshold = token_threshold
        self.message_threshold = message_threshold
        self.prompt = prompt if prompt is not None else DEFAULT_COMPACTION_PROMPT
        if not isinstance(self.prompt, str) or not self.prompt.strip():
            raise ValueError("prompt must be nonempty text")
        self.provider = provider

    async def should_compact(self, request: "CompactionRequest") -> bool:
        if self.message_threshold is not None:
            return len(request.messages) >= self.message_threshold
        threshold = self.token_threshold
        if threshold is None:
            threshold = get_model_context_limit(request.provider) * 7 // 10
        observed = request.actual_input_tokens or request.estimated_tokens
        return observed >= threshold

    async def compact(self, request: "CompactionRequest") -> "CompactionResult":
        from .events import ErrorEvent
        from .events import FinishReason
        from .events import TextDoneEvent
        from .events import ToolCallEvent
        from .events import ToolCallProgressEvent
        from .extensions import CompactionResult

        active_provider = self.provider or request.provider
        bounded = [
            message if message.role == "tool" and message.name == "skill" else truncate_tool_results([message])[0]
            for message in request.messages
        ]
        prompt_messages = [
            Message(role="system", content=self.prompt),
            Message(role="user", content=format_messages_as_text(bounded)),
        ]
        summary = ""
        async with aclosing(
            cast("AsyncGenerator[Event, None]", active_provider.generate(prompt_messages, tools=[], stream=False))
        ) as events:
            async for event in events:
                if isinstance(event, (ErrorEvent, ToolCallEvent, ToolCallProgressEvent)):
                    raise ValueError("Compaction provider returned an error or tool call")
                if isinstance(event, TextDoneEvent):
                    if summary or event.finish_reason != FinishReason.STOP:
                        raise ValueError("Compaction provider did not complete a plain-text summary")
                    summary = event.text.strip()
        if not summary:
            raise ValueError("Compaction provider returned an empty summary")
        return CompactionResult(messages=[Message(role="compaction_summary", content=summary)], summary=summary)


def estimate_tokens(content: str | list[ContentPart] | None) -> int:
    """Estimate token count for content.

    Uses a simple heuristic: ~4 characters per token for text.
    For multimodal content, estimates based on content type.

    Args:
        content: Message content (string, list of ContentParts, or None)

    Returns:
        Estimated token count
    """
    if content is None:
        return 0

    if isinstance(content, str):
        if len(content) == 0:
            return 0
        return max(1, len(content) // CHARS_PER_TOKEN)

    total = 0
    for part in content:
        if isinstance(part, TextContent):
            total += max(1, len(part.text) // CHARS_PER_TOKEN)
        else:
            # For images/audio/documents, estimate based on base64 size
            if hasattr(part, "base64_data") and part.base64_data:
                # Base64 is ~4/3 of binary, tokens are roughly chars/4
                # For images, this is a rough estimate
                total += max(100, len(part.base64_data) // (CHARS_PER_TOKEN * 4))
            else:
                total += 10  # Minimal estimate for unknown parts

    return total


def estimate_messages_tokens(messages: list[Message]) -> int:
    """Estimate total token count for a list of messages.

    Includes message overhead (role, metadata, etc.).

    Args:
        messages: List of messages to estimate

    Returns:
        Estimated total token count
    """
    total = 0
    for msg in messages:
        # Role overhead (~4 tokens for role markers)
        total += 4

        # Content
        total += estimate_tokens(msg.content)

        # Tool calls (arguments are often large JSON)
        if msg.tool_calls:
            for tc in msg.tool_calls:
                total += len(tc.name) // CHARS_PER_TOKEN + 4
                # Arguments are often large JSON objects
                if tc.arguments:
                    import json

                    args_str = json.dumps(tc.arguments)
                    total += len(args_str) // CHARS_PER_TOKEN
                # Metadata
                if tc.metadata:
                    total += len(str(tc.metadata)) // CHARS_PER_TOKEN

        # Tool call ID and name overhead
        if msg.tool_call_id:
            total += len(msg.tool_call_id) // CHARS_PER_TOKEN
        if msg.name:
            total += len(msg.name) // CHARS_PER_TOKEN

    return total


def truncate_tool_results(messages: list[Message], max_chars: int = MAX_TOOL_RESULT_CHARS) -> list[Message]:
    """Truncate large tool results in messages.

    Replaces large tool result content with a placeholder indicating
    the result was truncated.

    Args:
        messages: List of messages to process
        max_chars: Maximum characters for tool results (default: 10000)

    Returns:
        List of messages with truncated tool results
    """
    result = []
    for msg in messages:
        if msg.role == "tool" and isinstance(msg.content, str) and len(msg.content) > max_chars:
            truncated_msg = Message(
                role=msg.role,
                content=f"[Tool result truncated - {len(msg.content)} chars total. "
                f"Result was too large for context. Ask to re-run if needed.]",
                tool_call_id=msg.tool_call_id,
                name=msg.name,
            )
            result.append(truncated_msg)
            continue
        result.append(msg)
    return result


def get_model_context_limit(provider: "Provider") -> int:
    """Get the context window limit for a provider's model.

    This is a best-effort estimation based on known model limits.
    For unknown models, returns a safe default of 200,000 tokens.

    Args:
        provider: The LLM provider

    Returns:
        Estimated context window limit in tokens
    """
    model = provider.model.lower() if provider.model else ""

    # Known model context limits (as of 2024)
    # Order matters: check more specific models first

    # OpenAI - check gpt-4o and gpt-4-turbo before gpt-4
    if "gpt-4o" in model or "gpt-4o-mini" in model:
        return 128000
    if "gpt-4-turbo" in model or "gpt-4-0125" in model or "gpt-4-1106" in model:
        return 128000
    if "gpt-4-32k" in model:
        return 32768
    if "gpt-4" in model:
        return 8192
    if "gpt-3.5-turbo-16k" in model:
        return 16384
    if "gpt-3.5-turbo" in model:
        return 4096
    # OpenAI o-series (reasoning models)
    if "o1-" in model or "o3-" in model or "o4-" in model:
        return 200000
    if "gpt-5" in model:
        return 200000

    # Anthropic Claude
    if "claude-3-opus" in model or "claude-3-sonnet" in model or "claude-3-haiku" in model:
        return 200000
    if "claude-3.5" in model:
        return 200000
    if "claude-2" in model:
        return 100000
    if "claude" in model:
        return 100000

    # Google Gemini
    if "gemini-1.5-pro" in model or "gemini-1.5-flash" in model:
        return 1000000  # 1M tokens
    if "gemini-2.5" in model:
        return 1000000  # 1M tokens
    if "gemini-pro" in model:
        return 32000

    # DeepSeek
    if "deepseek" in model:
        return 64000

    # Kimi / Moonshot
    if "kimi" in model or "moonshot" in model:
        return 128000

    # GLM / Zhipu
    if "glm" in model or "chatglm" in model:
        return 128000

    # Llama
    if "llama-3" in model and "70b" in model:
        return 128000
    if "llama-3" in model:
        return 8192

    # Mistral
    if "mistral-large" in model or "mistral-medium" in model:
        return 32000
    if "mistral" in model:
        return 32000

    # Fireworks / FW- models
    if "fw-" in model:
        return 8000  # Conservative default

    # OpenRouter vendor-prefixed models
    # OpenAI models via OpenRouter
    if model.startswith("openai/gpt-5"):
        return 200000
    if model.startswith("openai/gpt-4o"):
        return 128000
    if model.startswith("openai/gpt-4"):
        return 8192
    if model.startswith("openai/gpt-3.5"):
        return 4096
    if model.startswith("openai/o1") or model.startswith("openai/o3") or model.startswith("openai/o4"):
        return 200000
    # Google models via OpenRouter
    if model.startswith("google/gemini-2.5") or model.startswith("google/gemini-3"):
        return 1000000  # 1M tokens
    if model.startswith("google/gemini-1.5"):
        return 1000000
    # Anthropic models via OpenRouter
    if model.startswith("anthropic/claude"):
        return 200000
    # DeepSeek via OpenRouter
    if model.startswith("deepseek/"):
        return 64000

    # Unknown model - use safe default
    logger.debug(f"Unknown model context limit for: {model}, using default {DEFAULT_CONTEXT_LIMIT}")
    return DEFAULT_CONTEXT_LIMIT


def format_messages_as_text(messages: list[Message]) -> str:
    """Format messages as a single text string for compaction.

    Args:
        messages: List of messages to format

    Returns:
        Formatted text string representing the conversation
    """
    import json

    lines = []
    for msg in messages:
        role = msg.role.upper()
        if msg.role == "system":
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            lines.append(f"[SYSTEM]\n{content}\n")
        elif msg.role == "user":
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            lines.append(f"[USER]\n{content}\n")
        elif msg.role == "assistant":
            if msg.content:
                lines.append(f"[ASSISTANT]\n{msg.content}\n")
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    args_str = json.dumps(tc.arguments) if tc.arguments else "{}"
                    lines.append(f"[ASSISTANT TOOL CALL: {tc.name}]\n{args_str}\n")
        elif msg.role == "tool":
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            lines.append(f"[TOOL RESULT: {msg.name}]\n{content}\n")
        elif msg.role == "developer":
            # Convert developer messages to assistant for better compatibility
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            lines.append(f"[ASSISTANT]\n{content}\n")
        elif msg.role == "compaction_summary":
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            lines.append(f"[PREVIOUS SUMMARY]\n{content}\n")
        else:
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            lines.append(f"[{role}]\n{content}\n")

    return "\n".join(lines)
