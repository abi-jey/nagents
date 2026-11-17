"""Strict, bounded Agent Runtime Protocol v1 frames."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeAlias

VERSION = 1
MAX_FRAME = 1024 * 1024
Json: TypeAlias = str | int | float | bool | None | list["Json"] | dict[str, "Json"]
Emitter: TypeAlias = Callable[[str, dict[str, object]], None]


class ProtocolError(ValueError):
    """Invalid host input; never include raw input in error messages."""


def mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ProtocolError("Expected a JSON object")
    return {str(key): item for key, item in value.items()}


def fields(value: dict[str, object], required: set[str], optional: frozenset[str] = frozenset()) -> None:
    if required - value.keys() or value.keys() - required - optional:
        raise ProtocolError("Missing or unexpected protocol fields")


def text(value: dict[str, object], key: str, maximum: int = 32_000, *, empty: bool = False) -> str:
    result = value.get(key)
    if not isinstance(result, str) or len(result.encode()) > maximum or (not empty and not result):
        raise ProtocolError("Invalid or oversized string field")
    return result


def output(run_id: str, message: dict[str, object]) -> None:
    encoded = json.dumps(
        {"protocol_version": VERSION, "run_id": run_id, "message": message}, ensure_ascii=False, allow_nan=False
    )
    if len(encoded.encode()) + 1 > MAX_FRAME:
        raise ProtocolError("Runtime frame exceeds 1 MiB")
    sys.stdout.write(encoded + "\n")
    sys.stdout.flush()


@dataclass(frozen=True)
class HostFrame:
    run_id: str
    message: dict[str, object]


async def read(reader: asyncio.StreamReader) -> HostFrame:
    try:
        line = await reader.readline()
    except (ValueError, asyncio.LimitOverrunError):
        raise ProtocolError("Host frame exceeds 1 MiB") from None
    if not line:
        raise EOFError
    if len(line) > MAX_FRAME or not line.endswith(b"\n"):
        raise ProtocolError("Oversized or incomplete host frame")
    try:
        value = mapping(json.loads(line))
    except (ValueError, UnicodeError):
        raise ProtocolError("Invalid host JSON") from None
    fields(value, {"protocol_version", "run_id", "message"})
    if type(value["protocol_version"]) is not int or value["protocol_version"] != VERSION:
        raise ProtocolError("Agent runtime protocol version mismatch")
    return HostFrame(text(value, "run_id", 80), mapping(value["message"]))
