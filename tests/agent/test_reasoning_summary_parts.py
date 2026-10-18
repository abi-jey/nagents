"""Agent display metadata orders, bounds, and isolates provider summary parts."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from nagents import Agent
from nagents import SessionManager
from nagents.events import ReasoningChunkEvent
from nagents.events import ResponseMetadataEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.response_metadata import SUMMARY_LIMIT
from nagents.response_metadata import ResponseTrace
from tests.agent.test_extensions import OfflineProvider

if TYPE_CHECKING:
    from pathlib import Path


def part_event(output_index: int, summary_index: int, chunk: str) -> ReasoningChunkEvent:
    return ReasoningChunkEvent(displayable=True, summary_part=(output_index, summary_index), chunk=chunk)


def test_late_earlier_part_backfill_keeps_summary_order_and_paragraph_boundary() -> None:
    trace = ResponseTrace("response", "session", 0)
    trace.observe(part_event(1, 1, "Second heading\n"))
    trace.observe(part_event(1, 1, "Second body."))
    trace.observe(part_event(0, 0, "First heading"))
    trace.observe(part_event(0, 0, "\nFirst body."))

    snapshot = trace.snapshot()
    assert snapshot.summary == "First heading\nFirst body.\n\nSecond heading\nSecond body."
    assert snapshot.status == "thinking"


def test_unkeyed_titles_and_usage_only_responses_remain_honest() -> None:
    title_only = ResponseTrace("title", "session", 0)
    title_only.observe(ReasoningChunkEvent(displayable=True, chunk="Plan"))
    assert title_only.snapshot().summary == "Plan"

    usage_only = ResponseTrace("usage", "session", 1)
    snapshot = usage_only.snapshot()
    assert snapshot.summary == "" and not snapshot.usage_reported


def test_keyed_summary_retention_is_bounded_and_tool_rounds_stay_isolated() -> None:
    first_round = ResponseTrace("response-a", "session", 0)
    second_round = ResponseTrace("response-b", "session", 1)
    first_round.observe(part_event(0, 0, "A" * (SUMMARY_LIMIT + 100)))
    first_snapshot = first_round.snapshot()
    second_round.observe(part_event(0, 0, "Tool follow-up"))
    second_snapshot = second_round.snapshot()

    assert len(first_snapshot.summary) == SUMMARY_LIMIT and first_snapshot.summary_truncated
    assert second_snapshot.summary == "Tool follow-up" and not second_snapshot.summary_truncated
    assert first_snapshot.response_id != second_snapshot.response_id


def test_agent_keeps_summary_parts_on_their_own_tool_round(tmp_path: Path) -> None:
    async def scenario() -> None:
        provider = OfflineProvider(
            [
                [
                    part_event(0, 0, "Before tool"),
                    ToolCallEvent(id="tool-call", name="unavailable", arguments={}),
                ],
                [part_event(0, 0, "After tool"), TextDoneEvent(text="Done")],
            ]
        )
        agent = Agent(provider, SessionManager(tmp_path / "state.db"), compaction_strategy=None)
        events = [event async for event in agent.run("start", session_id="root")]
        finals = [
            event
            for event in events
            if isinstance(event, ResponseMetadataEvent) and event.status in {"completed", "failed"}
        ]
        assert [event.summary for event in finals] == ["Before tool", "After tool"]
        assert len({event.response_id for event in finals}) == 2
        await agent.close()

    asyncio.run(scenario())
