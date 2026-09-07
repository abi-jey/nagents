"""
Gemini native format adapters.

Handles conversion between our internal types and Gemini REST API format.
"""

import uuid
from typing import Any

from ..events import FinishReason
from ..types import COMPACTION_SUMMARY_PREFIX
from ..types import AudioContent
from ..types import ContentPart
from ..types import DocumentContent
from ..types import GenerationConfig
from ..types import ImageContent
from ..types import Message
from ..types import TextContent
from ..types import ToolCall
from ..types import ToolDefinition
from ._validation import ProtocolError
from ._validation import list_data
from ._validation import object_data
from ._validation import string_data
from ._validation import tool_arguments


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
        return {
            "inline_data": {
                "mime_type": part.media_type,
                "data": part.base64_data,
            }
        }

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

    system_messages = [m for m in messages if m.role == "system"]
    compaction_messages = [m for m in messages if m.role == "compaction_summary"]

    system_parts: list[dict[str, Any]] = []
    for m in system_messages:
        system_parts.extend(_format_content(m.content))

    if compaction_messages:
        compaction_text = "\n\n".join(m.content if isinstance(m.content, str) else "" for m in compaction_messages)
        system_parts.append({"text": COMPACTION_SUMMARY_PREFIX + compaction_text})

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
        if msg.role == "system":
            continue
        if msg.role == "compaction_summary":
            continue

        # Map roles - developer converts to model (assistant) for compatibility
        role = "model" if msg.role in ("developer", "assistant", "tool") else "user"

        parts: list[dict[str, Any]] = []

        # Add content parts (handles str, list[ContentPart], or None)
        parts.extend(_format_content(msg.content))

        # Add function calls (for model messages)
        if msg.tool_calls:
            for tc in msg.tool_calls:
                fc_part: dict[str, Any] = {
                    "functionCall": {
                        "name": tc.name,
                        "args": tc.arguments,
                    }
                }
                # Preserve thought signature for multi-turn context continuity
                # Gemini thinking models require this to maintain reasoning state
                if tc.metadata.get("thoughtSignature"):
                    fc_part["thoughtSignature"] = tc.metadata["thoughtSignature"]
                parts.append(fc_part)

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


def _response_candidate(response: object) -> dict[str, object]:
    data = object_data(response)
    if data.get("error") or object_data(data.get("promptFeedback") or {}).get("blockReason"):
        raise ProtocolError("Gemini reported a generation error or blocked prompt; no tools were released.")
    candidates = list_data(data.get("candidates", []))
    if not candidates:
        return {}
    if len(candidates) != 1:
        raise ProtocolError("Gemini returned multiple candidates for a single-candidate request.")
    candidate = object_data(candidates[0])
    index = candidate.get("index", 0)
    if type(index) is not int or index != 0:
        raise ProtocolError("Gemini returned an unexpected candidate index.")
    return candidate


def parse_finish_reason(response: object) -> FinishReason:
    reason = _response_candidate(response).get("finishReason")
    if reason is None:
        return FinishReason.UNKNOWN
    reasons = {
        "STOP": FinishReason.STOP,
        "MAX_TOKENS": FinishReason.LENGTH,
        "SAFETY": FinishReason.CONTENT_FILTER,
    }
    if string_data(reason) not in reasons:
        raise ProtocolError("Gemini returned an unsuccessful finish reason; no tools were released.")
    return reasons[string_data(reason)]


def parse_response(
    response: dict[str, object],
) -> tuple[str, list[ToolCall], dict[str, object] | None]:
    """
    Parse text, tool calls, and usage from Gemini response.

    Args:
        response: Gemini API response dict

    Returns:
        Tuple of (text, tool_calls, usage_metadata)
    """
    text = ""
    tool_calls: list[ToolCall] = []
    candidate = _response_candidate(response)
    if candidate:
        content = object_data(candidate.get("content") or {})
        parts = list_data(content.get("parts", []))

        for raw_part in parts:
            part = object_data(raw_part)
            # Extract text
            if "text" in part:
                text += string_data(part["text"])

            # Extract function calls
            if "functionCall" in part:
                fc = object_data(part["functionCall"])
                name = string_data(fc.get("name", ""))
                if not name.strip():
                    raise ProtocolError("Gemini returned an empty tool name; no tools were released.")
                metadata: dict[str, str] = {}
                # Preserve thought signature for multi-turn context continuity
                # Gemini thinking models (3.x, 2.5) attach thoughtSignature to
                # functionCall parts; it must be sent back in subsequent turns.
                if "thoughtSignature" in part:
                    metadata["thoughtSignature"] = string_data(part["thoughtSignature"])
                tool_calls.append(
                    ToolCall(
                        id=str(uuid.uuid4()),  # Gemini doesn't provide IDs
                        name=name,
                        arguments=tool_arguments(object_data(fc.get("args", {}))),
                        metadata=metadata,
                    )
                )

    # Get usage metadata
    raw_usage = response.get("usageMetadata")
    usage = object_data(raw_usage) if raw_usage is not None else None

    return text, tool_calls, usage
