"""Metadata-only cache accounting: local transports, never live provider calls."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from aiohttp import web

from nagents.events import ErrorEvent
from nagents.events import Usage
from nagents.observation import observer
from nagents.provider import Provider
from nagents.provider import ProviderType
from nagents.provider.cache import ConversationCacheTracker
from nagents.provider.codex.openai import OpenAIProvider
from nagents.types import Message
from tests.providers.codex.test_openai_provider import call_item
from tests.providers.codex.test_openai_provider import completion
from tests.providers.codex.test_openai_provider import credentials
from tests.providers.codex.test_openai_provider import endpoint
from tests.providers.codex.test_openai_provider import sse
from tests.providers.codex.test_openai_provider import text_item

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from nagents.provider.cache import ConversationCacheSnapshot


def test_tracker_totals_bounds_immutable_and_sanitized() -> None:
    tracker = ConversationCacheTracker(max_entries=2)
    key = tracker.key("https://user:password@example.com/v1/responses?key=secret#secret", "model", "session")
    first = tracker.record(key, Usage(prompt_tokens=20, cached_tokens=5, prompt_reported=True))
    assert first is not None
    assert first.key.endpoint == "https://example.com/v1/responses"
    second = tracker.record(key, Usage(prompt_tokens=30, cached_tokens=10, prompt_reported=True))
    assert second is not None
    assert (second.request_count, second.input_tokens, second.cached_tokens) == (2, 50, 15)
    assert (second.last_input_tokens, second.last_cached_tokens, second.last_input_reported) == (30, 10, True)
    assert first.request_count == 1
    with pytest.raises(FrozenInstanceError):
        second.request_count = 100  # type: ignore[misc]
    missing = tracker.record(key, Usage())
    assert missing is not None
    assert (missing.request_count, missing.input_tokens, missing.input_reported_requests) == (3, 50, 2)
    assert not missing.last_input_reported
    for session in ("two", "three"):
        tracker.record(tracker.key(key.endpoint, "model", session), Usage())
    assert len(tracker.snapshot()) == 2
    assert tracker.get(key.endpoint, "model", "session") is None
    assert tracker.record(tracker.key(key.endpoint, "model", ""), Usage()) is None
    assert tracker.record(tracker.key(key.endpoint, "model", "x" * 4097), Usage()) is None
    assert "password" not in repr(tracker.snapshot())
    assert "secret" not in repr(first)


@pytest.mark.parametrize("bound", [0, -1, True])
def test_invalid_bounds(bound: int) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        ConversationCacheTracker(bound)


def test_thread_safe_counters_and_route_model_isolation() -> None:
    tracker = ConversationCacheTracker()
    key = tracker.key("https://example.com/responses", "model", "session")

    def record(_: int) -> None:
        tracker.record(key, Usage(prompt_tokens=2, cached_tokens=1, prompt_reported=True))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(record, range(1000)))
    snapshot = tracker.get(key.endpoint, key.model, key.conversation_id)
    assert snapshot is not None
    assert (snapshot.request_count, snapshot.input_tokens, snapshot.cached_tokens) == (1000, 2000, 1000)
    for route, model in (("https://other.com/responses", "model"), (key.endpoint, "other")):
        tracker.record(tracker.key(route, model, "session"), Usage())
    assert len(tracker.snapshot()) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_responses_concurrency_sessions_and_provider_isolation(stream: bool) -> None:
    provider = Provider(ProviderType.OPENAI_COMPATIBLE, model="model", api_key="credential-secret", api="responses")
    observed: list[ConversationCacheSnapshot] = []

    def observe(kind: str, data: dict[str, object]) -> None:
        if kind == "conversation_cache_usage":
            observed.append(data["snapshot"])  # type: ignore[arg-type]

    async def post_json(*args: object, **kwargs: object) -> object:
        await asyncio.sleep(0)
        return completion([text_item(), call_item()])["response"]

    async def post_stream(*args: object, **kwargs: object) -> AsyncIterator[str]:
        import json

        await asyncio.sleep(0)
        yield json.dumps(completion([text_item(), call_item()]))

    provider._http.post_json = post_json  # type: ignore[method-assign,assignment]
    provider._http.post_stream = post_stream  # type: ignore[method-assign]
    token = observer.set(observe)

    async def run(session: str) -> None:
        with provider.session_context(session):
            events = [event async for event in provider.generate([Message("user", "prompt-secret")], stream=stream)]
            assert not any(isinstance(event, ErrorEvent) for event in events)

    try:
        await asyncio.gather(run("one"), run("two"), run("one"))
        await run("")
        snapshots = provider.conversation_cache_snapshot()
        assert {entry.key.conversation_id: entry.request_count for entry in snapshots} == {"one": 2, "two": 1}
        assert sum(entry.input_tokens for entry in snapshots) == 60
        assert sum(entry.cached_tokens for entry in snapshots) == 15
        assert len(observed) == 3
        assert "prompt-secret" not in repr(snapshots)
        assert "credential-secret" not in repr(snapshots)
        async with Provider(ProviderType.OPENAI_COMPATIBLE, api_key="fake", model="model", api="responses") as other:
            assert other.conversation_cache_snapshot() == ()
        provider.model = "other-model"
        await run("one")
        assert len(provider.conversation_cache_snapshot()) == 3
    finally:
        observer.reset(token)
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "incomplete", "malformed", "transport"])
async def test_unsuccessful_responses_not_counted(status: str) -> None:
    result: dict[str, object] = {"status": status, "output": [], "usage": {"input_tokens": 20}}
    if status == "incomplete":
        result["incomplete_details"] = {"reason": "max_output_tokens"}
    elif status == "malformed":
        result.update(status="completed", output=[call_item("not json")])
    async with Provider(ProviderType.OPENAI_COMPATIBLE, api_key="fake", model="model", api="responses") as provider:
        provider._http.post_json = AsyncMock(return_value=result)  # type: ignore[method-assign]
        if status == "transport":
            provider._http.post_json.side_effect = TimeoutError()
        with provider.session_context("session"):
            _ = [event async for event in provider.generate([Message("user", "secret")], stream=False)]
        assert provider.conversation_cache_snapshot() == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("valid", [False, True])
async def test_codex_counts_validated_completion_once(monkeypatch: pytest.MonkeyPatch, valid: bool) -> None:
    async def handle(request: web.Request) -> web.Response:
        output = [text_item(), call_item() if valid else call_item("not json")]
        return web.Response(text=sse([completion(output)]), content_type="text/event-stream")

    async with endpoint(monkeypatch, handle), OpenAIProvider(credentials) as provider:
        with provider.session_context("conversation"):
            events = [event async for event in provider.generate([Message("user", "private prompt")])]
        snapshots = provider.conversation_cache_snapshot()
        if valid:
            assert len(snapshots) == 1
            assert (snapshots[0].request_count, snapshots[0].input_tokens, snapshots[0].cached_tokens) == (1, 20, 5)
            assert "private prompt" not in repr(snapshots)
            assert "fake-codex-access-secret" not in repr(snapshots)
        else:
            assert any(isinstance(event, ErrorEvent) for event in events)
            assert snapshots == ()
