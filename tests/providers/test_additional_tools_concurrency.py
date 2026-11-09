"""Overlapping requests and recovery for typed additional_tools input items.

The wire contract is {type: additional_tools, role: developer, tools: [...]},
not a message containing an additional_tools property. All transports are mocked.
"""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from nagents.adapters._additional_tools import rejects_additional_tools
from nagents.events import ErrorEvent
from nagents.events import Event
from nagents.events import TextDoneEvent
from nagents.http import HTTPError
from nagents.provider import Provider
from nagents.provider import ProviderType
from nagents.types import Message
from nagents.types import RetryConfig
from nagents.types import ToolDefinition
from tests.providers.test_gateway_provider import payload

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

A = ToolDefinition("a", "first", {"type": "object"})
B = ToolDefinition("b", "second", {"type": "object"})
C = ToolDefinition("c", "third", {"type": "object"})


def provider_for_test() -> Provider:
    return Provider(
        ProviderType.OPENAI_COMPATIBLE,
        api_key="fake-test-key",
        model="gpt-6-astra",
        api="responses",
        retry_config=RetryConfig(max_retries=0),
    )


def additions(body: dict[str, object]) -> list[str]:
    inputs = body["input"]
    assert isinstance(inputs, list)
    names: list[str] = []
    for item in inputs:
        assert isinstance(item, dict)
        assert "additional_tools" not in item
        if item.get("type") == "additional_tools":
            assert item["role"] == "developer" and "content" not in item
            names.extend(tool["name"] for tool in item["tools"])
    return names


async def generate(provider: Provider, session: str, tools: list[ToolDefinition]) -> list[Event]:
    with provider.session_context(session):
        return [event async for event in provider.generate([Message("user", "one")], tools, stream=False)]


@pytest.mark.asyncio
async def test_overlapping_sessions_keep_independent_timelines_and_wire_snapshots() -> None:
    async with provider_for_test() as provider:
        with patch.object(provider._http, "post_json", AsyncMock(return_value=payload("responses"))):
            await generate(provider, "left", [A])
            await generate(provider, "right", [C])
        arrived = asyncio.Event()
        release = asyncio.Event()
        bodies: dict[str, dict[str, object]] = {}

        async def post(url: str, body: dict[str, object], headers: dict[str, str]) -> dict[str, object]:
            session = provider._http.session_id
            bodies[session] = body
            if len(bodies) == 2:
                arrived.set()
            await release.wait()
            return payload("responses")

        with patch.object(provider._http, "post_json", post):
            left = asyncio.create_task(generate(provider, "left", [A, B]))
            right = asyncio.create_task(generate(provider, "right", [C, A]))
            try:
                await asyncio.wait_for(arrived.wait(), timeout=5)
                snapshot = deepcopy(bodies)
                assert additions(bodies["left"]) == ["b"]
                assert additions(bodies["right"]) == ["a"]
                # A third request updates one timeline while both old bodies
                # remain in flight. It must not mutate either request snapshot.
                with patch.object(provider._http, "post_json", AsyncMock(return_value=payload("responses"))):
                    await generate(provider, "left", [A, B, C])
                assert bodies == snapshot
            finally:
                release.set()
                results = await asyncio.gather(left, right)
            assert all(any(isinstance(event, TextDoneEvent) for event in events) for events in results)
            assert not any(isinstance(event, ErrorEvent) for events in results for event in events)


@pytest.mark.parametrize("failure", ["cancel", "http"])
@pytest.mark.asyncio
async def test_speculative_addition_is_replayed_after_cancel_or_failed_request(failure: str) -> None:
    async with provider_for_test() as provider:
        with patch.object(provider._http, "post_json", AsyncMock(return_value=payload("responses"))):
            await generate(provider, "session", [A])
        entered = asyncio.Event()
        blocked = asyncio.Event()
        attempted: list[dict[str, object]] = []

        async def post(url: str, body: dict[str, object], headers: dict[str, str]) -> dict[str, object]:
            attempted.append(deepcopy(body))
            entered.set()
            if failure == "http":
                raise HTTPError(400, "Invalid unrelated setting")
            await blocked.wait()
            return payload("responses")

        with patch.object(provider._http, "post_json", post):
            task = asyncio.create_task(generate(provider, "session", [A, B]))
            try:
                await asyncio.wait_for(entered.wait(), timeout=5)
                if failure == "cancel":
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    events = await task
                    assert any(isinstance(event, ErrorEvent) for event in events)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        retry = AsyncMock(return_value=payload("responses"))
        with patch.object(provider._http, "post_json", retry):
            recovered = await generate(provider, "session", [A, B])
        assert any(isinstance(event, TextDoneEvent) for event in recovered)
        assert not any(isinstance(event, ErrorEvent) for event in recovered)
        replay = retry.call_args.args[1]
        assert additions(replay) == ["b"]
        assert replay == attempted[0]


@pytest.mark.parametrize("preceding_frame", [False, True])
@pytest.mark.asyncio
async def test_first_sse_error_falls_back_but_not_after_nonvisible_frame(preceding_frame: bool) -> None:
    async with provider_for_test() as provider:
        with patch.object(provider._http, "post_json", AsyncMock(return_value=payload("responses"))):
            await generate(provider, "session", [A])
        bodies: list[dict[str, object]] = []

        async def chunks(url: str, body: dict[str, object], headers: dict[str, str]) -> AsyncIterator[str]:
            bodies.append(deepcopy(body))
            if additions(body):
                if preceding_frame:
                    yield '{"type":"response.created"}'
                yield json.dumps({"type": "error", "error": {"message": "additional_tools is not supported"}})
            else:
                yield json.dumps({"type": "response.completed", "response": payload("responses")})

        with patch.object(provider._http, "post_stream", chunks), provider.session_context("session"):
            events = [event async for event in provider.generate([Message("user", "one")], [A, B])]
        if preceding_frame:
            assert len(bodies) == 1
            assert any(isinstance(event, ErrorEvent) for event in events)
            assert not any(isinstance(event, TextDoneEvent) for event in events)
        else:
            assert len(bodies) == 2
            assert additions(bodies[0]) == ["b"]
            assert additions(bodies[1]) == []
            tools = bodies[1]["tools"]
            assert isinstance(tools, list)
            assert [tool["name"] for tool in tools] == ["a", "b"]
            assert any(isinstance(event, TextDoneEvent) for event in events)
            assert not any(isinstance(event, ErrorEvent) for event in events)


@pytest.mark.parametrize("code", [[], {}])
def test_malformed_rejection_code_does_not_crash_classifier(code: object) -> None:
    assert not rejects_additional_tools({"message": "Invalid additional_tools schema", "code": code})
