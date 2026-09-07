"""Offline regressions for agent-owned usage and completion state."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import TYPE_CHECKING

import pytest

from nagents import Agent
from nagents import Provider
from nagents import ProviderType
from nagents import SessionManager
from nagents.events import DoneEvent
from nagents.events import ErrorEvent
from nagents.events import FinishReason
from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.events import TokenUsage
from nagents.events import ToolCallEvent
from nagents.events import ToolResultEvent
from nagents.events import Usage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.types import GenerationConfig
    from nagents.types import Message
    from nagents.types import ToolDefinition


class ScriptedProvider(Provider):
    def __init__(self, rounds: list[list[Event]]) -> None:
        super().__init__(ProviderType.OPENAI_COMPATIBLE, "offline-key", "offline-model")
        self.rounds = iter(rounds)

    async def verify_model(self, force: bool = False) -> bool:
        return True

    async def generate(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: GenerationConfig | None = None,
        stream: bool = True,
        verify_model: bool = False,
    ) -> AsyncIterator[Event]:
        for event in next(self.rounds):
            yield deepcopy(event)


def test_usage_aggregates_snapshots_once_and_preserves_details(tmp_path: Path) -> None:
    async def drive() -> None:
        first = Usage(prompt_tokens=10, completion_tokens=4, total_tokens=14, cached_tokens=2, reasoning_tokens=1)
        second = Usage(prompt_tokens=20, completion_tokens=6, total_tokens=26, audio_tokens=3, reasoning_tokens=2)
        provider = ScriptedProvider(
            [
                [
                    TextChunkEvent(chunk="call", usage=Usage(prompt_tokens=10, completion_tokens=1, total_tokens=11)),
                    TextDoneEvent(text="call", usage=first, finish_reason=FinishReason.TOOL_CALLS),
                    ToolCallEvent(id="a", name="work", usage=first),
                    ToolCallEvent(id="b", name="work", usage=first),
                ],
                [TextDoneEvent(text="done", usage=second)],
            ]
        )

        def work() -> str:
            """Return a local result."""
            return "result"

        agent = Agent(provider, SessionManager(tmp_path / "usage.db"), tools=[work], compactor=None)
        try:
            events = [event async for event in agent.run("go", session_id="s")]
            assert events[0].usage.session == TokenUsage(10, 1, 11)
            for event in events[1:-2]:
                assert event.usage.session == TokenUsage(10, 4, 14)
                assert event.usage.cached_tokens == 2
                assert event.usage.reasoning_tokens == 1
            assert len([event for event in events if isinstance(event, ToolResultEvent)]) == 2
            for event in events[-2:]:
                assert event.usage == Usage(
                    prompt_tokens=20,
                    completion_tokens=6,
                    total_tokens=26,
                    audio_tokens=3,
                    reasoning_tokens=2,
                    session=TokenUsage(30, 10, 40),
                )
            assert agent._session_tokens["s"] == 20  # Context size is not cumulative billing usage.
            assert len({id(event.usage.session) for event in events}) == len(events)
        finally:
            await agent.close()

    asyncio.run(drive())


def test_usage_survives_runs_without_cross_session_or_consumer_mutation(tmp_path: Path) -> None:
    async def drive() -> None:
        usage = Usage(prompt_tokens=10, completion_tokens=4, total_tokens=14)
        provider = ScriptedProvider([[TextDoneEvent(text="done", usage=usage)]] * 3)
        agent = Agent(provider, SessionManager(tmp_path / "sessions.db"), compactor=None)
        try:
            first = [event async for event in agent.run("one", session_id="s")]
            assert first[-1].usage.session == TokenUsage(10, 4, 14)
            first[-1].usage.prompt_tokens = 999
            first[-1].usage.session.prompt_tokens = 999
            second = [event async for event in agent.run("two", session_id="s")]
            assert second[-1].usage.session == TokenUsage(20, 8, 28)
            other = [event async for event in agent.run("three", session_id="other")]
            assert other[-1].usage.session == TokenUsage(10, 4, 14)
            assert first[0].usage.session == TokenUsage(10, 4, 14)
        finally:
            await agent.close()

    asyncio.run(drive())


@pytest.mark.parametrize("recoverable", [False, True])
def test_usage_keeps_failed_rounds_but_does_not_reuse_missing_usage(tmp_path: Path, recoverable: bool) -> None:
    async def drive() -> None:
        provider = ScriptedProvider(
            [
                [
                    TextChunkEvent(
                        chunk="partial", usage=Usage(prompt_tokens=10, completion_tokens=4, total_tokens=14)
                    ),
                    ErrorEvent(message="failed", recoverable=recoverable),
                ],
                [TextDoneEvent(text="done")],
            ]
        )
        agent = Agent(provider, SessionManager(tmp_path / "failure.db"), compactor=None)
        try:
            events = [event async for event in agent.run("go", session_id="s")]
            assert events[-1].usage.session == TokenUsage(10, 4, 14)
            if not recoverable:
                assert isinstance(events[-1], DoneEvent)
                assert events[-1].finish_reason == FinishReason.UNKNOWN
                events = [event async for event in agent.run("again", session_id="s")]
            assert events[-1].usage == Usage(session=TokenUsage(10, 4, 14))
        finally:
            await agent.close()

    asyncio.run(drive())


@pytest.mark.parametrize(
    "reason", [FinishReason.STOP, FinishReason.LENGTH, FinishReason.CONTENT_FILTER, FinishReason.UNKNOWN]
)
def test_done_preserves_provider_finish_reason(tmp_path: Path, reason: FinishReason) -> None:
    async def drive() -> None:
        provider = ScriptedProvider([[TextDoneEvent(text="answer", finish_reason=reason)]])
        agent = Agent(provider, SessionManager(tmp_path / "finish.db"), compactor=None)
        try:
            events = [event async for event in agent.run("go", session_id="s")]
            assert isinstance(events[-1], DoneEvent)
            assert events[-1].finish_reason == reason
            assert events[-1].final_text == "answer"
        finally:
            await agent.close()

    asyncio.run(drive())


def test_round_limit_does_not_report_a_natural_stop(tmp_path: Path) -> None:
    async def drive() -> None:
        provider = ScriptedProvider([[ToolCallEvent(id="a", name="missing", usage=Usage(10, 4, 14))]])
        agent = Agent(provider, SessionManager(tmp_path / "limit.db"), compactor=None, max_tool_rounds=1)
        try:
            events = [event async for event in agent.run("go", session_id="s")]
            assert isinstance(events[-2], ErrorEvent)
            assert isinstance(events[-1], DoneEvent)
            assert events[-1].finish_reason == FinishReason.UNKNOWN
            assert events[-2].usage == events[-1].usage == Usage(10, 4, 14, session=TokenUsage(10, 4, 14))
        finally:
            await agent.close()

    asyncio.run(drive())
