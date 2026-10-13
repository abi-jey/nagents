"""Provider summaries remain display-only; cumulative billing stays per generation."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from nagents import Agent
from nagents import SessionManager
from nagents.events import DoneEvent
from nagents.events import ErrorEvent
from nagents.events import ReasoningChunkEvent
from nagents.events import ResponseMetadataEvent
from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.events import Usage
from tests.agent.test_extensions import OfflineProvider

if TYPE_CHECKING:
    from pathlib import Path


def test_reasoning_before_text_and_late_partial_repeated_usage_do_not_inflate_totals(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = OfflineProvider(
            [
                [
                    ReasoningChunkEvent(displayable=True, chunk=""),
                    ReasoningChunkEvent(displayable=True, chunk="Display summary"),
                    TextChunkEvent(chunk="Answer"),
                    TextDoneEvent(text="Answer", usage=Usage(prompt_tokens=12, completion_tokens=8, total_tokens=20)),
                    TextDoneEvent(
                        text="Answer",
                        usage=Usage(reasoning_tokens=6, reasoning_reported=True, reasoning_in_output="included"),
                    ),
                    TextDoneEvent(
                        text="Answer",
                        usage=Usage(reasoning_tokens=6, reasoning_reported=True, reasoning_in_output="included"),
                    ),
                ]
            ]
        )
        agent = Agent(provider, SessionManager(tmp_path / "state.db"), compaction_strategy=None)
        events = [event async for event in agent.run("hello", session_id="root")]
        metadata = [event for event in events if isinstance(event, ResponseMetadataEvent)]
        assert len({event.response_id for event in metadata}) == 1
        assert any(event.status == "thinking" and not event.summary and not event.usage_reported for event in metadata)
        assert metadata[-1].summary == "Display summary"
        assert metadata[-1].history_id == "2" and metadata[-1].status == "completed"
        assert [event.revision for event in metadata] == sorted({event.revision for event in metadata})
        done = events[-1]
        assert isinstance(done, DoneEvent)
        assert (
            done.usage.prompt_tokens,
            done.usage.completion_tokens,
            done.usage.total_tokens,
            done.usage.reasoning_tokens,
        ) == (12, 8, 20, 6)
        assert done.usage.session and done.usage.session.reasoning_tokens == 6
        assert done.usage.session.reasoning_reported
        assert agent._session_tokens["root"] == 12
        history = await agent.session.get_history("root")
        assert [message.content for message in history] == ["hello", "Answer"]
        assert all("Display summary" not in str(request.messages) for request in provider.requests)
        await agent.close()

    asyncio.run(scenario())


def test_rounds_and_retry_have_distinct_response_ids_and_unknown_reasoning_is_not_zero(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = OfflineProvider(
            [
                [ReasoningChunkEvent(displayable=True, chunk="Attempt"), ErrorEvent(message="retry", recoverable=True)],
                [
                    ReasoningChunkEvent(displayable=True, chunk="Tool summary"),
                    ToolCallEvent(
                        id="one",
                        name="missing",
                        arguments={},
                        usage=Usage(
                            prompt_tokens=10,
                            completion_tokens=5,
                            total_tokens=15,
                            reasoning_reported=True,
                            reasoning_in_output="included",
                        ),
                    ),
                ],
                [TextDoneEvent(text="Done", usage=Usage(prompt_tokens=20, completion_tokens=7, total_tokens=27))],
            ]
        )
        agent = Agent(provider, SessionManager(tmp_path / "state.db"), compaction_strategy=None)
        events = [event async for event in agent.run("go", session_id="root")]
        final = [
            event
            for event in events
            if isinstance(event, ResponseMetadataEvent) and event.status in {"completed", "failed"}
        ]
        assert len(final) == 3 and len({event.response_id for event in final}) == 3
        assert final[0].status == "failed" and not final[0].usage_reported
        assert final[1].usage.reasoning_reported and final[1].usage.reasoning_tokens == 0
        assert final[1].history_id == "2"
        assert not final[2].usage.reasoning_reported and final[2].history_id == "4"
        done = events[-1]
        assert isinstance(done, DoneEvent) and done.usage.session
        assert (
            done.usage.session.prompt_tokens,
            done.usage.session.completion_tokens,
            done.usage.session.total_tokens,
        ) == (30, 12, 42)
        assert not done.usage.session.reasoning_reported
        assert agent._session_tokens["root"] == 20
        assert "Tool summary" not in str(provider.requests[-1].messages)
        await agent.close()

    asyncio.run(scenario())


def test_summary_cap_preserves_prefix_and_reported_zero_usage(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = OfflineProvider(
            [
                [
                    ReasoningChunkEvent(displayable=True, chunk="x" * 20000),
                    TextDoneEvent(text="ok", usage=Usage(reported=True, reasoning_reported=True)),
                ]
            ]
        )
        agent = Agent(provider, SessionManager(tmp_path / "state.db"), compaction_strategy=None)
        events = [event async for event in agent.run("go", session_id="root")]
        final = [event for event in events if isinstance(event, ResponseMetadataEvent)][-1]
        assert len(final.summary) == 16384 and final.summary_truncated
        assert final.usage_reported and final.usage.reasoning_reported and final.usage.reasoning_tokens == 0
        await agent.close()

    asyncio.run(scenario())


def test_native_reasoning_only_usage_does_not_claim_zero_input_output_or_inferred_total(tmp_path: Path) -> None:
    async def scenario() -> None:
        usage = Usage(
            reasoning_tokens=7, reasoning_reported=True, reasoning_in_output="separate", total_tokens=7, reported=True
        )
        provider = OfflineProvider([[TextDoneEvent(text="ok", usage=usage)]])
        agent = Agent(provider, SessionManager(tmp_path / "state.db"), compaction_strategy=None)
        events = [event async for event in agent.run("go", session_id="root")]
        final = [event for event in events if isinstance(event, ResponseMetadataEvent)][-1]
        assert final.usage_reported and final.usage.reasoning_reported
        assert (
            not final.usage.prompt_reported and not final.usage.completion_reported and not final.usage.total_reported
        )
        assert final.usage.reasoning_tokens == 7 and final.usage.total_tokens == 7
        done = events[-1]
        assert done.usage.session and done.usage.session.reasoning_reported
        assert not done.usage.session.prompt_reported and not done.usage.session.completion_reported
        await agent.close()

    asyncio.run(scenario())


def test_saturated_summary_only_publishes_changes() -> None:
    from nagents.response_metadata import ResponseTrace

    trace = ResponseTrace("response", "session", 0)
    trace.observe(ReasoningChunkEvent(displayable=True, chunk="x" * 20000))
    trace.snapshot()
    for _ in range(100):
        trace.observe(ReasoningChunkEvent(displayable=True, chunk="ignored after cap"))
        assert not trace.dirty
    trace.observe(TextChunkEvent(chunk="answer"))
    assert trace.dirty


def test_private_framework_reasoning_is_not_promoted_to_display_metadata() -> None:
    from nagents.response_metadata import ResponseTrace

    trace = ResponseTrace("response", "session", 0)
    trace.observe(ReasoningChunkEvent(chunk="Private framework reasoning"))
    assert not trace.summary and trace.status == "responding" and not trace.dirty
    trace.observe(ReasoningChunkEvent(chunk="Provider-supplied display summary", displayable=True))
    public = trace.snapshot()
    assert public.summary == "Provider-supplied display summary" and public.status == "thinking"
    assert "Private framework reasoning" not in str(public)
