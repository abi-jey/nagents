"""Generation-boundary metadata is published before provider latency."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from nagents import Agent
from nagents import SessionManager
from nagents.events import ErrorEvent
from nagents.events import ResponseMetadataEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from tests.agent.test_extensions import OfflineProvider

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.types import GenerationConfig
    from nagents.types import Message
    from nagents.types import ToolDefinition


class BoundaryProvider(OfflineProvider):
    def __init__(self, rounds: list[list[Event]], observed: list[Event]) -> None:
        super().__init__(rounds)
        self.observed = observed
        self.observed_at_generate_start: list[tuple[ResponseMetadataEvent, ...]] = []

    async def generate(
        self,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: GenerationConfig | None = None,
        stream: bool = True,
        verify_model: bool = False,
    ) -> AsyncIterator[Event]:
        self.observed_at_generate_start.append(
            tuple(event for event in self.observed if isinstance(event, ResponseMetadataEvent))
        )
        async for event in super().generate(messages, tools, config, stream, verify_model):
            yield event


def _metadata(events: list[Event]) -> list[ResponseMetadataEvent]:
    return [event for event in events if isinstance(event, ResponseMetadataEvent)]


def _initial(event: ResponseMetadataEvent, *, session_id: str, round_number: int) -> None:
    assert event.status == "thinking"
    assert event.summary == ""
    assert not event.usage_reported
    assert event.response_session_id == session_id
    assert event.round_number == round_number
    assert event.revision == 1


def test_generation_boundary_precedes_provider_start_and_first_token(tmp_path: Path) -> None:
    async def scenario() -> None:
        observed: list[Event] = []
        provider = BoundaryProvider([[TextDoneEvent(text="Ready.")]], observed)
        agent = Agent(provider, SessionManager(tmp_path / "state.db"), compaction_strategy=None)
        events: list[Event] = []
        async for event in agent.run("go", session_id="root"):
            events.append(event)
            observed.append(event)

        metadata = _metadata(events)
        assert metadata
        initial = metadata[0]
        _initial(initial, session_id="root", round_number=0)
        assert len(provider.observed_at_generate_start) == 1
        assert [item.response_id for item in provider.observed_at_generate_start[0]] == [initial.response_id], (
            "the initial snapshot must be delivered before provider.generate begins"
        )
        assert events.index(initial) < next(i for i, event in enumerate(events) if isinstance(event, TextDoneEvent))
        terminal = next(event for event in metadata if event.status == "completed")
        assert terminal.response_id == initial.response_id
        assert terminal.response_session_id == initial.response_session_id
        assert terminal.revision > initial.revision
        await agent.close()

    asyncio.run(scenario())


def test_closing_after_initial_marker_cancels_before_provider_start(tmp_path: Path) -> None:
    async def scenario() -> None:
        observed: list[Event] = []
        provider = BoundaryProvider([[TextDoneEvent(text="Must not be requested.")]], observed)
        agent = Agent(provider, SessionManager(tmp_path / "state.db"), compaction_strategy=None)
        run = agent.run("cancel before first token", session_id="root")

        initial = await anext(run)
        assert isinstance(initial, ResponseMetadataEvent)
        _initial(initial, session_id="root", round_number=0)
        assert agent._active_generation_id("root") == initial.response_id
        assert not provider.observed_at_generate_start

        await run.aclose()
        assert not provider.observed_at_generate_start
        assert provider.requests == []
        assert agent._active_generation_id("root") == ""
        with pytest.raises(StopAsyncIteration):
            await anext(run)
        await agent.close()

    asyncio.run(scenario())


def test_each_tool_round_has_an_early_identity_and_same_id_terminal_update(tmp_path: Path) -> None:
    async def scenario() -> None:
        observed: list[Event] = []
        provider = BoundaryProvider(
            [
                [ToolCallEvent(id="echo-call", name="echo", arguments={"value": "ok"})],
                [TextDoneEvent(text="Tool returned ok.")],
            ],
            observed,
        )

        def echo(value: str) -> str:
            return value

        agent = Agent(provider, SessionManager(tmp_path / "state.db"), tools=[echo], compaction_strategy=None)
        events: list[Event] = []
        async for event in agent.run("use echo", session_id="root"):
            events.append(event)
            observed.append(event)

        metadata = _metadata(events)
        starts = [event for event in metadata if event.revision == 1]
        assert len(provider.requests) == len(provider.observed_at_generate_start) == 2
        assert len(starts) == 2
        assert len({event.response_id for event in starts}) == 2
        assert [event.round_number for event in starts] == [0, 1]
        for index, initial in enumerate(starts):
            _initial(initial, session_id="root", round_number=index)
            observed_at_start = provider.observed_at_generate_start[index]
            assert initial.response_id in {event.response_id for event in observed_at_start}
            updates = [event for event in metadata if event.response_id == initial.response_id]
            assert updates[0] == initial
            assert [event.revision for event in updates] == sorted({event.revision for event in updates})
            terminal = next(event for event in updates if event.status == "completed")
            assert terminal.response_id == initial.response_id
            assert terminal.response_session_id == initial.response_session_id
            assert terminal.round_number == index
            assert terminal.revision > initial.revision
        await agent.close()

    asyncio.run(scenario())


def test_fatal_provider_error_closes_started_generation_without_summary(tmp_path: Path) -> None:
    async def scenario() -> None:
        observed: list[Event] = []
        provider = BoundaryProvider([[ErrorEvent(message="provider unavailable", recoverable=False)]], observed)
        agent = Agent(provider, SessionManager(tmp_path / "state.db"), compaction_strategy=None)
        events: list[Event] = []
        async for event in agent.run("go", session_id="root"):
            events.append(event)
            observed.append(event)

        metadata = _metadata(events)
        initial = metadata[0]
        _initial(initial, session_id="root", round_number=0)
        failed = [event for event in metadata if event.status == "failed"]
        assert len(failed) == 1
        assert failed[0].response_id == initial.response_id
        assert failed[0].response_session_id == initial.response_session_id
        assert failed[0].round_number == initial.round_number
        assert failed[0].revision > initial.revision
        assert failed[0].summary == ""
        assert not failed[0].usage_reported
        assert not any(event.status == "completed" for event in metadata)
        await agent.close()

    asyncio.run(scenario())
