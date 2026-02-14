"""
Gemini native format adapters.

Handles conversion between our internal types and Gemini REST API format.
"""

import uuid
from typing import Any

from ..types import AudioContent
from ..types import ContentPart
from ..types import DocumentContent
from ..types import GenerationConfig
from ..types import ImageContent
from ..types import Message
from ..types import TextContent
from ..types import ToolCall
from ..types import ToolDefinition


def _format_content_part(part: ContentPart) -> dict[str, Any]:
    """
    Format a single content part to Gemini format.

    Args:
        part: A ContentPart (TextContent, ImageContent, AudioContent, DocumentContent)

    Returns:
        Dict in Gemini part format
    """
    if isinstance(part, TextContent):
        return {"text": part.text}

    elif isinstance(part, ImageContent):
        # Gemini uses inline_data for base64 images
        if part.base64_data and part.media_type:
            return {
                "inline_data": {
                    "mime_type": part.media_type,
                    "data": part.base64_data,
                }
            }
        elif part.url:
            # Check if it's a data URL
            if part.url.startswith("data:"):
                # Parse data URL: data:image/jpeg;base64,<data>
                try:
                    header, data = part.url.split(",", 1)
                    mime_type = header.split(":")[1].split(";")[0]
                    return {
                        "inline_data": {
                            "mime_type": mime_type,
                            "data": data,
                        }
                    }
                except (IndexError, ValueError) as err:
                    raise ValueError(f"Invalid data URL format: {part.url[:50]}...") from err
            else:
                # Regular URL - Gemini supports file_data for URLs
                return {
                    "file_data": {
                        "file_uri": part.url,
                    }
                }
        else:
            raise ValueError("ImageContent must have either 'url' or both 'base64_data' and 'media_type'")

    elif isinstance(part, AudioContent):
        # Gemini supports audio via inline_data
        mime_type = f"audio/{part.format}"
        return {
            "inline_data": {
                "mime_type": mime_type,
                "data": part.base64_data,
            }
        }

    elif isinstance(part, DocumentContent):
        # Gemini supports PDFs via inline_data
        return {
            "inline_data": {
                "mime_type": part.media_type,
                "data": part.base64_data,
            }
        }

    else:
        raise ValueError(f"Unknown content part type: {type(part)}")


def _format_content(content: str | list[ContentPart] | None) -> list[dict[str, Any]]:
    """
    Format message content for Gemini API.

    Args:
        content: String, list of ContentParts, or None

    Returns:
        List of parts for Gemini API
    """
    if content is None:
        return []

    if isinstance(content, str):
        if content:
            return [{"text": content}]
        return []

    # It's a list of content parts
    return [_format_content_part(part) for part in content]


def format_request(
    messages: list[Message],
    tools: list[ToolDefinition] | None = None,
    config: GenerationConfig | None = None,
) -> dict[str, Any]:
    """
    Format a complete request body for Gemini API.

    Args:
        messages: List of Message objects
        tools: Optional list of tools
        config: Optional generation config

    Returns:
        Request body dict for Gemini API
    """
    body: dict[str, Any] = {
        "contents": format_contents(messages),
    }

    # Extract system instruction from messages
    system_messages = [m for m in messages if m.role == "system"]
    if system_messages:
        # Collect all system parts
        system_parts: list[dict[str, Any]] = []
        for m in system_messages:
            system_parts.extend(_format_content(m.content))
        if system_parts:
            body["systemInstruction"] = {"parts": system_parts}

    if tools:
        body["tools"] = [{"functionDeclarations": format_tools(tools)}]

    if config:
        gen_config: dict[str, Any] = {}
        if config.temperature is not None:
            gen_config["temperature"] = config.temperature
        if config.max_tokens is not None:
            gen_config["maxOutputTokens"] = config.max_tokens
        if config.top_p is not None:
            gen_config["topP"] = config.top_p
        if config.stop:
            gen_config["stopSequences"] = config.stop
        if config.thinking_config:
            gen_config["thinkingConfig"] = config.thinking_config

        if gen_config:
            body["generationConfig"] = gen_config

    return body


def format_contents(messages: list[Message]) -> list[dict[str, Any]]:
    """
    Convert our Message objects to Gemini contents format.

    Gemini uses: contents: [{role: "user", parts: [{text: "..."}, {inline_data: {...}}]}]

    Supports multimodal content including images, audio, and documents.

    Args:
        messages: List of Message objects

    Returns:
        List of content dicts in Gemini format
    """
    contents = []

    for msg in messages:
        # Skip system messages (handled separately)
        if msg.role == "system":
            continue

        # Map roles
        role = "user" if msg.role == "user" else "model"

        parts: list[dict[str, Any]] = []

        # Add content parts (handles str, list[ContentPart], or None)
        parts.extend(_format_content(msg.content))

        # Add function calls (for model messages)
        if msg.tool_calls:
            for tc in msg.tool_calls:
                parts.append(
                    {
                        "functionCall": {
                            "name": tc.name,
                            "args": tc.arguments,
                        }
                    }
                )

        # Add function response (for tool result messages)
        if msg.role == "tool" and msg.name:
            # In Gemini, function responses are in user turn
            role = "user"
            # Get result text from content
            result_text = ""
            if isinstance(msg.content, str):
                result_text = msg.content
            elif isinstance(msg.content, list) and msg.content:
                # Get first text content
                for part in msg.content:
                    if isinstance(part, TextContent):
                        result_text = part.text
                        break
            parts = [
                {
                    "functionResponse": {
                        "name": msg.name,
                        "response": {
                            "result": result_text,
                        },
                    }
                }
            ]

        if parts:
            contents.append(
                {
                    "role": role,
                    "parts": parts,
                }
            )

    return contents


def format_tools(tools: list[ToolDefinition]) -> list[dict[str, Any]]:
    """
    Convert our ToolDefinition to Gemini function declarations.

    Args:
        tools: List of ToolDefinition objects

    Returns:
        List of function declaration dicts
    """
    return [
        {
            "name": t.name,
            "description": t.description,
            "parameters": t.parameters,
        }
        for t in tools
    ]


def parse_response(
    response: dict[str, Any],
) -> tuple[str, list[ToolCall], dict[str, Any] | None]:
    """
    Parse text, tool calls, and usage from Gemini response.

    Args:
        response: Gemini API response dict

    Returns:
        Tuple of (text, tool_calls, usage_metadata)
    """
    text = ""
    tool_calls: list[ToolCall] = []
    usage = None

    # Get candidates
    candidates = response.get("candidates") or []
    if candidates:
        candidate = candidates[0]
        content = candidate.get("content") or {}
        parts = content.get("parts") or []

        for part in parts:
            # Extract text
            if "text" in part:
                text += part["text"]

            # Extract function calls
            if "functionCall" in part:
                fc = part["functionCall"]
                tool_calls.append(
                    ToolCall(
                        id=str(uuid.uuid4()),  # Gemini doesn't provide IDs
                        name=fc.get("name", ""),
                        arguments=fc.get("args", {}),
                    )
                )

    # Get usage metadata
    usage = response.get("usageMetadata")

    return text, tool_calls, usage
