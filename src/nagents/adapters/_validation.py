"""Validation shared by API-key wire adapters. Errors never contain wire data."""

import json
from typing import cast

from ..events import Usage
from ..types import ToolArguments


class ProtocolError(ValueError):
    """Only static, credential-free messages may be passed to this exception."""


def object_data(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ProtocolError("Provider returned malformed object data.")
    return cast("dict[str, object]", value)


def list_data(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ProtocolError("Provider returned malformed list data.")
    return cast("list[object]", value)


def string_data(value: object) -> str:
    if not isinstance(value, str):
        raise ProtocolError("Provider returned malformed text data.")
    return value


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("Provider returned duplicate JSON object keys.")
        result[key] = value
    return result


def load_object(value: str) -> dict[str, object]:
    try:
        result = object_data(json.loads(value, object_pairs_hook=_unique_object))
        json.dumps(result, allow_nan=False)
        return result
    except (ValueError, TypeError, RecursionError):
        raise ProtocolError("Provider returned invalid JSON object data.") from None


def tool_arguments(value: object) -> ToolArguments:
    result = load_object(value) if isinstance(value, str) else object_data(value)
    try:
        json.dumps(result, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        raise ProtocolError("Provider returned invalid tool arguments; no tools were released.") from None
    return cast("ToolArguments", result)


def json_values_equal(left: object, right: object) -> bool:
    """Compare validated JSON values without treating booleans as numbers."""
    pending = [(left, right)]
    while pending:
        left, right = pending.pop()
        if isinstance(left, dict) and isinstance(right, dict):
            if left.keys() != right.keys():
                return False
            pending.extend((left[key], right[key]) for key in left)
        elif isinstance(left, list) and isinstance(right, list):
            if len(left) != len(right):
                return False
            pending.extend(zip(left, right, strict=True))
        elif (
            type(left) is not type(right) and not (type(left) in (int, float) and type(right) in (int, float))
        ) or left != right:
            return False
    return True


def token_usage(value: object, api: str) -> Usage:
    data = object_data(value) if value is not None else {}

    def count(source: dict[str, object], key: str) -> int:
        result = source.get(key, 0)
        if result is None:
            return 0
        if type(result) is not int or result < 0:
            raise ProtocolError("Provider returned invalid token usage.")
        return result

    if api == "gemini":
        prompt = count(data, "promptTokenCount")
        completion = count(data, "candidatesTokenCount")
        reasoning = count(data, "thoughtsTokenCount")
        return Usage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=count(data, "totalTokenCount")
            if data.get("totalTokenCount") is not None
            else prompt + completion + reasoning,
            reasoning_tokens=reasoning,
            prompt_reported=data.get("promptTokenCount") is not None,
            completion_reported=data.get("candidatesTokenCount") is not None,
            total_reported=data.get("totalTokenCount") is not None,
            reasoning_reported=data.get("thoughtsTokenCount") is not None,
            reasoning_in_output="separate" if data.get("thoughtsTokenCount") is not None else "unknown",
            reported=any(
                data.get(key) is not None
                for key in ("promptTokenCount", "candidatesTokenCount", "totalTokenCount", "thoughtsTokenCount")
            ),
        )
    if api == "messages":
        cached = count(data, "cache_read_input_tokens")
        prompt = count(data, "input_tokens") + cached + count(data, "cache_creation_input_tokens")
        completion = count(data, "output_tokens")
        details = object_data(data.get("output_tokens_details") or {})
        return Usage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
            cached_tokens=cached,
            prompt_reported=any(
                data.get(key) is not None
                for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
            ),
            completion_reported=data.get("output_tokens") is not None,
            reasoning_tokens=count(details, "thinking_tokens"),
            reasoning_reported=details.get("thinking_tokens") is not None,
            reasoning_in_output="included" if details.get("thinking_tokens") is not None else "unknown",
            reported=any(
                data.get(key) is not None
                for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
            )
            or details.get("thinking_tokens") is not None,
        )
    input_key, output_key = (
        ("input_tokens", "output_tokens") if api == "responses" else ("prompt_tokens", "completion_tokens")
    )
    inputs = object_data(data.get(f"{input_key}_details") or {})
    outputs = object_data(data.get(f"{output_key}_details") or {})
    prompt, completion = count(data, input_key), count(data, output_key)
    return Usage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=count(data, "total_tokens") if data.get("total_tokens") is not None else prompt + completion,
        cached_tokens=count(inputs, "cached_tokens"),
        prompt_reported=data.get(input_key) is not None,
        completion_reported=data.get(output_key) is not None,
        total_reported=data.get("total_tokens") is not None,
        reasoning_tokens=count(outputs, "reasoning_tokens"),
        reasoning_reported=outputs.get("reasoning_tokens") is not None,
        reasoning_in_output="included" if outputs.get("reasoning_tokens") is not None else "unknown",
        reported=any(data.get(key) is not None for key in (input_key, output_key, "total_tokens"))
        or outputs.get("reasoning_tokens") is not None,
        audio_tokens=count(inputs, "audio_tokens") + count(outputs, "audio_tokens"),
    )
