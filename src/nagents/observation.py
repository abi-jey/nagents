"""Opt-in execution observation, independent of clients and persistence.

Context variables keep concurrent child/model attempts isolated. Observers see
application payloads, never transport authentication values.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import asdict
from dataclasses import is_dataclass
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING

import aiohttp

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import SimpleNamespace

Observer = Callable[[str, dict[str, object]], None]
observer: ContextVar[Observer | None] = ContextVar("nagents_observer", default=None)
scope: ContextVar[Mapping[str, object]] = ContextVar("nagents_observation_scope", default=MappingProxyType({}))
attempt: ContextVar[str] = ContextVar("nagents_http_attempt", default="")


def json_default(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    return f"<{type(value).__name__}>"


def observe(kind: str, **data: object) -> None:
    sink = observer.get()
    if sink is not None:
        sink(kind, {**scope.get(), **data})


def headers(values: Mapping[str, str]) -> dict[str, str]:
    allowed = {"content-type", "content-length", "accept", "user-agent", "retry-after", "x-request-id"}
    return {name: value if name.lower() in allowed else "[redacted]" for name, value in values.items()}


def trace_config() -> aiohttp.TraceConfig:
    trace = aiohttp.TraceConfig()

    async def start(
        session: aiohttp.ClientSession, context: SimpleNamespace, params: aiohttp.TraceRequestStartParams
    ) -> None:
        context.attempt_id = uuid.uuid4().hex
        attempt.set(context.attempt_id)
        observe(
            "http_request",
            attempt_id=context.attempt_id,
            method=params.method,
            url=str(params.url.with_query(None)),
            headers=headers(params.headers),
        )

    async def sent(
        session: aiohttp.ClientSession, context: SimpleNamespace, params: aiohttp.TraceRequestChunkSentParams
    ) -> None:
        if observer.get() is not None:
            observe(
                "http_request_body",
                attempt_id=context.attempt_id,
                body=params.chunk.decode("utf-8", errors="replace"),
                bytes=len(params.chunk),
            )

    async def end(
        session: aiohttp.ClientSession, context: SimpleNamespace, params: aiohttp.TraceRequestEndParams
    ) -> None:
        observe(
            "http_response",
            attempt_id=context.attempt_id,
            status=params.response.status,
            headers=headers(params.response.headers),
        )

    async def received(
        session: aiohttp.ClientSession, context: SimpleNamespace, params: aiohttp.TraceResponseChunkReceivedParams
    ) -> None:
        if observer.get() is not None:
            observe(
                "http_response_body", attempt_id=context.attempt_id, body=params.chunk.decode("utf-8", errors="replace")
            )

    async def failed(
        session: aiohttp.ClientSession, context: SimpleNamespace, params: aiohttp.TraceRequestExceptionParams
    ) -> None:
        observe("http_error", attempt_id=context.attempt_id, error=type(params.exception).__name__)

    trace.on_request_start.append(start)
    trace.on_request_chunk_sent.append(sent)
    trace.on_request_end.append(end)
    trace.on_response_chunk_received.append(received)
    trace.on_request_exception.append(failed)
    return trace


def response_chunk(data: str) -> None:
    observe("http_stream", attempt_id=attempt.get(), data=data)


def detached(value: object) -> object:
    return json.loads(json.dumps(value, default=json_default))
