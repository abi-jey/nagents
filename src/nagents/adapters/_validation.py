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
        return Usage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=count(data, "totalTokenCount")
            if data.get("totalTokenCount") is not None
            else prompt + completion,
        )
    if api == "messages":
        cached = count(data, "cache_read_input_tokens")
        prompt = count(data, "input_tokens") + cached + count(data, "cache_creation_input_tokens")
        completion = count(data, "output_tokens")
        return Usage(
            prompt_tokens=prompt, completion_tokens=completion, total_tokens=prompt + completion, cached_tokens=cached
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
        reasoning_tokens=count(outputs, "reasoning_tokens"),
        audio_tokens=count(inputs, "audio_tokens") + count(outputs, "audio_tokens"),
    )
