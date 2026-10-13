"""Codex failures keep strict tool boundaries and expose only local reason labels."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from aiohttp import web

from nagents.events import ErrorEvent
from nagents.events import ToolCallEvent
from nagents.events import ToolCallProgressEvent
from nagents.provider._codex_diagnostics import codex_protocol_reason
from nagents.provider.openai import OpenAIProvider
from nagents.types import Message
from tests.providers.test_openai_provider import call_item
from tests.providers.test_openai_provider import credentials
from tests.providers.test_openai_provider import endpoint
from tests.providers.test_openai_provider import sse

if TYPE_CHECKING:
    from nagents.provider.openai import CodexCredentials

PRIVATE = "PRIVATE-UPSTREAM-PAYLOAD"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal", "reason"),
    [
        ("error", "response_failed"),
        ("response.failed", "response_failed"),
        ("response.incomplete", "response_incomplete"),
        ("response.cancelled", "response_cancelled"),
        ("", "stream_truncated"),
    ],
)
async def test_failed_codex_stream_keeps_preview_non_executable_and_reason_private(
    monkeypatch: pytest.MonkeyPatch, terminal: str, reason: str
) -> None:
    wire: list[dict[str, object]] = [
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {**call_item(""), "status": "in_progress"},
        }
    ]
    if terminal:
        wire.append({"type": terminal, "error": {"code": PRIVATE, "message": PRIVATE}})

    async def handle(request: web.Request) -> web.Response:
        return web.Response(text=sse(wire), content_type="text/event-stream")

    async with endpoint(monkeypatch, handle), OpenAIProvider(credentials) as provider:
        events = [event async for event in provider.generate([Message(role="user", content="test")])]
    errors = [event for event in events if isinstance(event, ErrorEvent)]
    assert len(errors) == 1 and errors[0].code == "CODEX_STREAM_INVALID"
    assert codex_protocol_reason(errors[0].message) == reason
    assert not any(isinstance(event, ToolCallEvent) for event in events)
    previews = [event for event in events if isinstance(event, ToolCallProgressEvent)]
    assert [event.status for event in previews] == ["streaming", "abandoned"]
    assert PRIVATE not in repr(events)


@pytest.mark.asyncio
async def test_invalid_tool_history_is_distinct_before_request_or_credentials() -> None:
    async def forbidden() -> CodexCredentials:
        raise AssertionError("Invalid local history must not request credentials")

    async with OpenAIProvider(forbidden) as provider:
        events = [event async for event in provider.generate([Message(role="tool", content=PRIVATE)])]
    assert len(events) == 1 and isinstance(events[0], ErrorEvent)
    assert events[0].code == "CODEX_REQUEST_INVALID"
    assert codex_protocol_reason(events[0].message) == "request_history"
    assert PRIVATE not in repr(events)
