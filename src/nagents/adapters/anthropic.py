"""
Anthropic Claude format adapters.

Handles conversion between our internal types and Anthropic Claude API format.
"""

from typing import Any

from ..types import COMPACTION_SUMMARY_PREFIX
from ..types import AudioContent
from ..types import ContentPart
from ..types import DocumentContent
from ..types import ImageContent
from ..types import Message
from ..types import TextContent
from ..types import ToolCall
from ..types import ToolDefinition
from ._validation import ProtocolError
from ._validation import string_data
from ._validation import tool_arguments


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
        return {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": part.media_type,
                "data": part.base64_data,
            },
        }

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
    Compaction summaries are appended to the system prompt.

    Args:
        messages: List of Message objects

    Returns:
        Tuple of (system_prompt, messages_list)
    """
    system_prompt: str | None = None
    compaction_summaries: list[str] = []
    result: list[dict[str, Any]] = []

    for msg in messages:
        if msg.role in {"system", "developer"}:
            if isinstance(msg.content, str):
                text = msg.content
            elif isinstance(msg.content, list):
                if not all(isinstance(part, TextContent) for part in msg.content):
                    raise ProtocolError("Messages system instructions must contain text only.")
                text_parts = [p.text for p in msg.content if isinstance(p, TextContent)]
                text = "\n".join(text_parts)
            else:
                text = ""
            if text:
                system_prompt = f"{system_prompt}\n\n{text}" if system_prompt else text
            continue

        if msg.role == "compaction_summary":
            content_str = msg.content if isinstance(msg.content, str) else ""
            compaction_summaries.append(content_str)
            continue

        role = msg.role
        formatted: dict[str, Any] = {"role": role}

        # Handle content
        if role == "assistant":
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
            if not msg.tool_call_id:
                raise ProtocolError("Messages tool results require a tool call ID.")
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

        if (
            msg.role == "tool"
            and result
            and result[-1]["role"] == "user"
            and isinstance(result[-1]["content"], list)
            and all(part.get("type") == "tool_result" for part in result[-1]["content"])
        ):
            result[-1]["content"].extend(formatted["content"])
        else:
            result.append(formatted)

    if compaction_summaries:
        combined = "\n\n".join(compaction_summaries)
        if system_prompt:
            system_prompt = system_prompt + "\n\n" + COMPACTION_SUMMARY_PREFIX + combined
        else:
            system_prompt = COMPACTION_SUMMARY_PREFIX + combined

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
            text += string_data(block.get("text", ""))

        elif block_type == "tool_use":
            if not block.get("id") or not block.get("name") or any(call.id == block["id"] for call in tool_calls):
                raise ProtocolError("Provider returned invalid or duplicate tool identities; no tools were released.")
            tool_calls.append(
                ToolCall(
                    id=string_data(block.get("id", "")),
                    name=string_data(block.get("name", "")),
                    arguments=tool_arguments(block.get("input", {})),
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

    def start_tool_call(self, index: int, tool_id: str, name: str, initial_input: object = None) -> None:
        """Start a new tool call block."""
        if index in self._tool_calls or type(index) is not int or not 0 <= index < 1024:
            raise ProtocolError("Provider returned a duplicate or invalid tool block index.")
        self._tool_calls[index] = {
            "id": tool_id,
            "name": name,
            "arguments": "",
            "initial_input": tool_arguments(initial_input) if initial_input is not None else {},
        }
        self._current_index = index

    def add_input_delta(self, index: int, partial_json: str) -> None:
        """Add input JSON fragment to a tool call."""
        if index in self._tool_calls:
            self._tool_calls[index]["arguments"] += partial_json
        else:
            raise ProtocolError("Provider returned arguments without a tool block.")

    def get_complete_tool_calls(self) -> list[ToolCall]:
        """
        Get all accumulated tool calls.

        Returns:
            List of complete ToolCall objects
        """
        result: list[ToolCall] = []
        for idx in sorted(self._tool_calls.keys()):
            tc = self._tool_calls[idx]
            if not tc["id"] or not tc["name"] or any(call.id == tc["id"] for call in result):
                raise ProtocolError("Provider returned invalid or duplicate tool identities; no tools were released.")
            arguments = tool_arguments(tc["arguments"]) if tc["arguments"] else tc["initial_input"]
            if tc["initial_input"] and arguments != tc["initial_input"]:
                raise ProtocolError("Provider returned inconsistent tool block arguments.")
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
