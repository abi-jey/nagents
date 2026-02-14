"""
Anthropic Claude format adapters.

Handles conversion between our internal types and Anthropic Claude API format.
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
    Format a single content part to Anthropic format.

    Args:
        part: A ContentPart (TextContent, ImageContent, AudioContent, DocumentContent)

    Returns:
        Dict in Anthropic content part format
    """
    if isinstance(part, TextContent):
        return {"type": "text", "text": part.text}

    elif isinstance(part, ImageContent):
        # Anthropic image format
        if part.base64_data and part.media_type:
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": part.media_type,
                    "data": part.base64_data,
                },
            }
        elif part.url:
            # Check if it's a data URL
            if part.url.startswith("data:"):
                # Parse data URL: data:image/jpeg;base64,<data>
                try:
                    header, data = part.url.split(",", 1)
                    media_type = header.split(":")[1].split(";")[0]
                    return {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": data,
                        },
                    }
                except (IndexError, ValueError) as err:
                    raise ValueError(f"Invalid data URL format: {part.url[:50]}...") from err
            else:
                # Regular URL
                return {
                    "type": "image",
                    "source": {
                        "type": "url",
                        "url": part.url,
                    },
                }
        else:
            raise ValueError("ImageContent must have either 'url' or both 'base64_data' and 'media_type'")

    elif isinstance(part, AudioContent):
        # Anthropic doesn't support audio input natively
        raise ValueError("AudioContent is not supported by Anthropic API. Use OpenAI for audio input.")

    elif isinstance(part, DocumentContent):
        # Anthropic document/PDF format
        result: dict[str, Any] = {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": part.media_type,
                "data": part.base64_data,
            },
        }
        if part.title:
            result["title"] = part.title
        return result

    else:
        raise ValueError(f"Unknown content part type: {type(part)}")


def _format_content(content: str | list[ContentPart] | None) -> str | list[dict[str, Any]]:
    """
    Format message content for Anthropic API.

    Args:
        content: String, list of ContentParts, or None

    Returns:
        Formatted content for Anthropic API (string or list of content blocks)
    """
    if content is None:
        return ""

    if isinstance(content, str):
        return content

    # It's a list of content parts - convert to Anthropic format
    return [_format_content_part(part) for part in content]


def format_messages(messages: list[Message]) -> tuple[str | None, list[dict[str, Any]]]:
    """
    Convert our Message objects to Anthropic API format.

    Anthropic handles system messages separately, so we extract them.

    Args:
        messages: List of Message objects

    Returns:
        Tuple of (system_prompt, messages_list)
    """
    system_prompt: str | None = None
    result = []

    for msg in messages:
        # Extract system message (Anthropic takes it as a separate parameter)
        if msg.role == "system":
            if isinstance(msg.content, str):
                system_prompt = msg.content
            elif isinstance(msg.content, list):
                # For multimodal system, extract text parts only
                text_parts = [p.text for p in msg.content if isinstance(p, TextContent)]
                system_prompt = "\n".join(text_parts) if text_parts else None
            continue

        formatted: dict[str, Any] = {"role": msg.role}

        # Handle content
        if msg.role == "assistant":
            # Assistant messages can have content and/or tool_use blocks
            content_parts: list[dict[str, Any]] = []

            # Add text content if present
            if msg.content:
                formatted_content = _format_content(msg.content)
                if isinstance(formatted_content, str):
                    if formatted_content:
                        content_parts.append({"type": "text", "text": formatted_content})
                else:
                    content_parts.extend(formatted_content)

            # Add tool_use blocks for tool calls
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    content_parts.append(
                        {
                            "type": "tool_use",
                            "id": tc.id,
                            "name": tc.name,
                            "input": tc.arguments,
                        }
                    )

            formatted["content"] = content_parts if content_parts else ""

        elif msg.role == "tool":
            # Tool result messages in Anthropic format
            formatted["role"] = "user"  # Tool results come from user role
            tool_result_content: str | list[dict[str, Any]]
            if isinstance(msg.content, str):
                tool_result_content = msg.content
            elif isinstance(msg.content, list):
                tool_result_content = [_format_content_part(p) for p in msg.content]
            else:
                tool_result_content = ""

            formatted["content"] = [
                {
                    "type": "tool_result",
                    "tool_use_id": msg.tool_call_id,
                    "content": tool_result_content,
                }
            ]

        else:
            # User messages
            formatted_content = _format_content(msg.content)
            if isinstance(formatted_content, str):
                formatted["content"] = formatted_content
            else:
                formatted["content"] = formatted_content

        result.append(formatted)

    return system_prompt, result


def format_tools(tools: list[ToolDefinition]) -> list[dict[str, Any]]:
    """
    Convert our ToolDefinition to Anthropic tool format.

    Args:
        tools: List of ToolDefinition objects

    Returns:
        List of tool dicts in Anthropic format
    """
    return [
        {
            "name": t.name,
            "description": t.description,
            "input_schema": t.parameters,
        }
        for t in tools
    ]


def parse_response(response: dict[str, Any]) -> tuple[str, list[ToolCall], dict[str, Any] | None]:
    """
    Parse text, tool calls, and usage from Anthropic response.

    Args:
        response: Anthropic API response dict

    Returns:
        Tuple of (text, tool_calls, usage)
    """
    text = ""
    tool_calls: list[ToolCall] = []

    # Parse content blocks
    content = response.get("content") or []
    for block in content:
        block_type = block.get("type")

        if block_type == "text":
            text += block.get("text", "")

        elif block_type == "tool_use":
            tool_calls.append(
                ToolCall(
                    id=block.get("id", ""),
                    name=block.get("name", ""),
                    arguments=block.get("input", {}),
                )
            )

    # Parse usage
    usage = response.get("usage")

    return text, tool_calls, usage


class StreamingToolCallAccumulator:
    """
    Accumulates streaming tool call deltas into complete tool calls.

    Anthropic streams tool calls as:
    - content_block_start with tool_use type (has id and name)
    - content_block_delta with input_json_delta (argument fragments)
    - content_block_stop
    """

    def __init__(self) -> None:
        self._tool_calls: dict[int, dict[str, Any]] = {}
        self._current_index: int = 0

    def start_tool_call(self, index: int, tool_id: str, name: str) -> None:
        """Start a new tool call block."""
        self._tool_calls[index] = {
            "id": tool_id,
            "name": name,
            "arguments": "",
        }
        self._current_index = index

    def add_input_delta(self, index: int, partial_json: str) -> None:
        """Add input JSON fragment to a tool call."""
        if index in self._tool_calls:
            self._tool_calls[index]["arguments"] += partial_json

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
        self._current_index = 0
