"""Offline regressions for native Gemini completion and tool-release boundaries."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from nagents import Agent
from nagents import Provider
from nagents import ProviderType
from nagents import SessionManager
from nagents.events import ErrorEvent
from nagents.events import FinishReason
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.events import ToolResultEvent
from nagents.types import Message

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


def candidate(parts: list[object], reason: str = "STOP") -> dict[str, object]:
    value: dict[str, object] = {"index": 0, "content": {"role": "model", "parts": parts}}
    if reason:
        value["finishReason"] = reason
    return {"candidates": [value]}


CALL = {"functionCall": {"name": "work", "args": {}}, "thoughtSignature": "test-signature"}


@asynccontextmanager
async def gemini_response(payloads: list[object]) -> AsyncIterator[Provider]:
    provider = Provider(ProviderType.GEMINI_NATIVE, "offline-key", "offline-model")

    async def post_stream(*args: object, **kwargs: object) -> AsyncIterator[str]:
        for payload in payloads:
            yield payload if isinstance(payload, str) else json.dumps(payload)

    try:
        with (
            patch.object(provider._http, "post_stream", side_effect=post_stream),
            patch.object(provider._http, "post_json", AsyncMock(return_value=payloads[0])),
            patch.object(provider, "verify_model", AsyncMock(return_value=True)),
        ):
            yield provider
    finally:
        await provider.close()


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "payload",
    [
        candidate([CALL], ""),
        candidate([CALL], "MAX_TOKENS"),
        candidate([CALL], "SAFETY"),
        candidate([CALL], "MALFORMED_FUNCTION_CALL"),
        candidate([CALL, {"functionCall": {"name": "work", "args": []}}]),
        candidate([CALL, {"functionCall": {"name": "", "args": {}}}]),
        candidate([CALL, {"functionCall": {"name": "work", "args": '{"bad":true}'}}]),
        candidate([CALL, {"functionCall": {"name": "work", "args": {"value": float("nan")}}}]),
        {"candidates": [{"content": {"parts": [CALL]}, "finishReason": "STOP"}] * 2},
        {"candidates": [{"index": 1, "content": {"parts": [CALL]}, "finishReason": "STOP"}]},
        {**candidate([CALL]), "usageMetadata": {"promptTokenCount": -1}},
        {**candidate([CALL]), "error": {"message": "wire-secret-must-not-escape"}},
        {"promptFeedback": {"blockReason": "SAFETY"}},
        {},
    ],
)
def test_gemini_rejects_invalid_completion_before_tool_release(payload: dict[str, object], stream: bool) -> None:
    async def drive() -> None:
        async with gemini_response([payload]) as provider:
            events = [event async for event in provider.generate([Message(role="user", content="go")], stream=stream)]
        assert not any(isinstance(event, ToolCallEvent | TextDoneEvent) for event in events)
        errors = [event for event in events if isinstance(event, ErrorEvent)]
        assert len(errors) == 1
        assert errors[0].code == "PROVIDER_PROTOCOL_ERROR"
        assert errors[0].recoverable is False
        assert "wire-secret" not in errors[0].message

    asyncio.run(drive())


@pytest.mark.parametrize(
    "payloads",
    [
        [candidate([CALL], ""), "{invalid", candidate([], "STOP")],
        [candidate([CALL]), candidate([{"text": "late"}])],
        [candidate([CALL]), candidate([CALL])],
        [candidate([CALL]), candidate([], "MAX_TOKENS")],
        [candidate([CALL]), {"error": {"message": "wire-secret-must-not-escape"}}],
        [candidate([CALL], ""), '{"candidates": [], "candidates": []}', candidate([], "STOP")],
    ],
)
def test_gemini_stream_validates_all_frames_before_tool_release(payloads: list[object], tmp_path: Path) -> None:
    async def drive() -> None:
        executed: list[str] = []

        def work() -> str:
            """Record an execution."""
            executed.append("work")
            return "done"

        async with gemini_response(payloads) as provider:
            agent = Agent(
                provider,
                SessionManager(tmp_path / "invalid.db"),
                tools=[work],
                streaming=True,
                compactor=None,
                max_tool_rounds=1,
            )
            try:
                events = [event async for event in agent.run("go", session_id="s")]
                assert executed == []
                assert not any(isinstance(event, ToolCallEvent | ToolResultEvent) for event in events)
                assert any(
                    isinstance(event, ErrorEvent) and event.code == "PROVIDER_PROTOCOL_ERROR" for event in events
                )
                assert await agent.session.get_history("s") == [Message(role="user", content="go")]
            finally:
                await agent.close()

    asyncio.run(drive())


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("with_tools", [False, True])
def test_gemini_preserves_completed_text_tools_signatures_and_usage(stream: bool, with_tools: bool) -> None:
    async def drive() -> None:
        parts: list[object] = [{"text": "ready"}]
        if with_tools:
            parts.extend([CALL, CALL])
        usage = {"usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 4, "totalTokenCount": 16}}
        payloads: list[object] = (
            [candidate(parts, ""), candidate([], "STOP"), usage] if stream else [{**candidate(parts), **usage}]
        )
        async with gemini_response(payloads) as provider:
            events = [event async for event in provider.generate([Message(role="user", content="go")], stream=stream)]
        assert not any(isinstance(event, ErrorEvent) for event in events)
        done = [event for event in events if isinstance(event, TextDoneEvent)]
        assert len(done) == 1 and done[0].text == "ready"
        assert done[0].finish_reason == (FinishReason.TOOL_CALLS if with_tools else FinishReason.STOP)
        calls = [event for event in events if isinstance(event, ToolCallEvent)]
        assert len(calls) == (2 if with_tools else 0)
        assert len({event.id for event in calls}) == len(calls)
        for call in calls:
            assert call.name == "work" and call.arguments == {}
            assert call.metadata == {"thoughtSignature": "test-signature"}
        for event in [*done, *calls]:
            assert event.usage.prompt_tokens == 12
            assert event.usage.completion_tokens == 4
            assert event.usage.total_tokens == 16

    asyncio.run(drive())


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    ("reason", "expected"),
    [("STOP", FinishReason.STOP), ("MAX_TOKENS", FinishReason.LENGTH), ("SAFETY", FinishReason.CONTENT_FILTER)],
)
def test_gemini_reports_text_only_and_empty_completion_reasons(
    stream: bool, reason: str, expected: FinishReason
) -> None:
    async def drive() -> None:
        async with gemini_response([candidate([], reason)]) as provider:
            events = [event async for event in provider.generate([Message(role="user", content="go")], stream=stream)]
        assert len(events) == 1
        assert isinstance(events[0], TextDoneEvent)
        assert events[0].finish_reason == expected

    asyncio.run(drive())
