"""Codex cache affinity follows the conversation without custom configuration."""

import pytest
from aiohttp import web

from nagents.events import ErrorEvent
from nagents.provider.codex.openai import OpenAIProvider
from nagents.types import Message
from tests.providers.codex.test_openai_provider import completion
from tests.providers.codex.test_openai_provider import credentials
from tests.providers.codex.test_openai_provider import endpoint
from tests.providers.codex.test_openai_provider import sse
from tests.providers.codex.test_openai_provider import text_item


@pytest.mark.asyncio
async def test_cache_affinity_defaults_follow_session_context(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = []

    async def handle(request: web.Request) -> web.Response:
        captured.append((dict(request.headers), await request.json()))
        return web.Response(text=sse([completion([text_item()])]), content_type="text/event-stream")

    async with endpoint(monkeypatch, handle), OpenAIProvider(credentials) as provider:
        provider.session_id_header = "X-Custom-Session"
        for session in ("one", "one", "two", ""):
            with provider.session_context(session):
                events = [event async for event in provider.generate([Message("user", "Reply OK")])]
                assert not any(isinstance(event, ErrorEvent) for event in events)
    for (headers, body), session in zip(captured, ("one", "one", "two", ""), strict=True):
        if session:
            assert headers["session-id"] == headers["thread-id"] == session
            assert headers["X-Custom-Session"] == session
            assert body["prompt_cache_key"] == session
        else:
            assert "session-id" not in headers
            assert "thread-id" not in headers
            assert "prompt_cache_key" not in body
