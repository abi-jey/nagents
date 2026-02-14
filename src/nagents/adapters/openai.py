"""
OpenAI format adapters.

Handles conversion between our internal types and OpenAI API format.
Works with OpenAI, Gemini via OpenAI compatibility, OpenRouter, Ollama, etc.
"""

import json
from typing import Any

from ..types import AudioContent
from ..types import ContentPart
from ..types import DocumentContent
from ..types import ImageContent
from ..types import Message
from ..types import TextContent
from ..types import ToolCall
from ..types import ToolDefinition


def _format_content_part(part: ContentPart) -> dict[str, Any]:
    """
    Format a single content part to OpenAI format.

    Args:
        part: A ContentPart (TextContent, ImageContent, AudioContent, DocumentContent)

    Returns:
        Dict in OpenAI content part format
    """
    if isinstance(part, TextContent):
        return {"type": "text", "text": part.text}

    elif isinstance(part, ImageContent):
        # Build image_url object
        if part.url:
            # URL provided directly (could be regular URL or data URL)
            image_url: dict[str, Any] = {"url": part.url}
        elif part.base64_data and part.media_type:
            # Build data URL from base64 data
            image_url = {"url": f"data:{part.media_type};base64,{part.base64_data}"}
        else:
            raise ValueError("ImageContent must have either 'url' or both 'base64_data' and 'media_type'")

        if part.detail:
            image_url["detail"] = part.detail

        return {"type": "image_url", "image_url": image_url}

    elif isinstance(part, AudioContent):
        # OpenAI audio input format
        return {
            "type": "input_audio",
            "input_audio": {
                "data": part.base64_data,
                "format": part.format,
            },
        }

    elif isinstance(part, DocumentContent):
        # OpenAI doesn't natively support documents, but we can include as text or skip
        # For now, we'll raise an error as it's not supported
        raise ValueError("DocumentContent is not supported by OpenAI API. Use Anthropic for PDF/document support.")

    else:
        raise ValueError(f"Unknown content part type: {type(part)}")


def _format_content(content: str | list[ContentPart] | None) -> str | list[dict[str, Any]] | None:
    """
    Format message content for OpenAI API.

    Args:
        content: String, list of ContentParts, or None

    Returns:
        Formatted content for OpenAI API
    """
    if content is None:
        return None

    if isinstance(content, str):
        return content

    # It's a list of content parts
    return [_format_content_part(part) for part in content]


def format_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """
    Convert our Message objects to OpenAI API format.

    Supports both simple string content and multimodal content (images, audio).

    Args:
        messages: List of Message objects

    Returns:
        List of message dicts in OpenAI format
    """
    result = []
    for msg in messages:
        formatted: dict[str, Any] = {"role": msg.role}

        # Format content (handles str, list[ContentPart], or None)
        formatted_content = _format_content(msg.content)

        if formatted_content is not None:
            formatted["content"] = formatted_content
        elif msg.role != "assistant":
            # Non-assistant messages should have content
            formatted["content"] = ""

        # Tool calls (for assistant messages)
        if msg.tool_calls:
            formatted["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments) if isinstance(tc.arguments, dict) else tc.arguments,
                    },
                }
                for tc in msg.tool_calls
            ]

        # Tool call ID (for tool result messages)
        if msg.tool_call_id:
            formatted["tool_call_id"] = msg.tool_call_id

        # Name (for tool result messages)
        if msg.name:
            formatted["name"] = msg.name

        result.append(formatted)

    return result


def format_tools(tools: list[ToolDefinition]) -> list[dict[str, Any]]:
    """
    Convert our ToolDefinition objects to OpenAI API format.

    Args:
        tools: List of ToolDefinition objects

    Returns:
        List of tool dicts in OpenAI format
    """
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        }
        for t in tools
    ]


def parse_tool_calls(choice: dict[str, Any]) -> list[ToolCall]:
    """
    Parse tool calls from an OpenAI response choice.

    Args:
        choice: A choice object from the response

    Returns:
        List of ToolCall objects
    """
    message = choice.get("message") or {}
    tool_calls_data = message.get("tool_calls") or []

    result = []
    for tc in tool_calls_data:
        func = tc.get("function", {})
        args_str = func.get("arguments", "{}")

        # Parse arguments from JSON string
        try:
            arguments = json.loads(args_str) if isinstance(args_str, str) else args_str
        except json.JSONDecodeError:
            arguments = {"raw": args_str}

        result.append(
            ToolCall(
                id=tc.get("id", ""),
                name=func.get("name", ""),
                arguments=arguments,
            )
        )

    return result


def parse_stream_delta(delta: dict[str, Any]) -> tuple[str | None, list[ToolCall]]:
    """
    Parse a streaming delta from OpenAI response.

    Args:
        delta: The delta object from a streaming chunk

    Returns:
        Tuple of (text_content, tool_calls)
    """
    text = delta.get("content")
    tool_calls = []

    # Tool calls in streaming come as deltas too
    tc_deltas = delta.get("tool_calls") or []
    for tc in tc_deltas:
        func = tc.get("function", {})
        args_str = func.get("arguments", "")

        # In streaming, arguments come in chunks - we return partial data
        # The caller needs to accumulate these
        if tc.get("id") or func.get("name"):
            try:
                arguments = json.loads(args_str) if args_str else {}
            except json.JSONDecodeError:
                arguments = {}

            tool_calls.append(
                ToolCall(
                    id=tc.get("id", ""),
                    name=func.get("name", ""),
                    arguments=arguments,
                )
            )

    return text, tool_calls


class StreamingToolCallAccumulator:
    """
    Accumulates streaming tool call deltas into complete tool calls.

    OpenAI streams tool calls in chunks:
    - First chunk has id and function name
    - Subsequent chunks have argument fragments
    """

    def __init__(self) -> None:
        self._tool_calls: dict[int, dict[str, Any]] = {}

    def add_delta(self, delta: dict[str, Any]) -> ToolCall | None:
        """
        Add a streaming delta and return a complete ToolCall if ready.

        Args:
            delta: The delta object containing tool_calls array

        Returns:
            Complete ToolCall if we have all data, None otherwise
        """
        tc_deltas = delta.get("tool_calls") or []

        for tc in tc_deltas:
            idx = tc.get("index", 0)

            if idx not in self._tool_calls:
                self._tool_calls[idx] = {
                    "id": "",
                    "name": "",
                    "arguments": "",
                }

            current = self._tool_calls[idx]

            # Accumulate id
            if tc.get("id"):
                current["id"] = tc["id"]

            # Accumulate function data
            func = tc.get("function", {})
            if func.get("name"):
                current["name"] = func["name"]
            if func.get("arguments"):
                current["arguments"] += func["arguments"]

        return None

    def get_complete_tool_calls(self) -> list[ToolCall]:
        """
        Get all accumulated tool calls.

        Returns:
            List of complete ToolCall objects
        """
        result = []
        for idx in sorted(self._tool_calls.keys()):
            tc = self._tool_calls[idx]
            if tc["id"] and tc["name"]:
                try:
                    arguments = json.loads(tc["arguments"]) if tc["arguments"] else {}
                except json.JSONDecodeError:
                    arguments = {"raw": tc["arguments"]}

                result.append(
                    ToolCall(
                        id=tc["id"],
                        name=tc["name"],
                        arguments=arguments,
                    )
                )
        return result

    def clear(self) -> None:
        """Clear accumulated tool calls."""
        self._tool_calls.clear()
