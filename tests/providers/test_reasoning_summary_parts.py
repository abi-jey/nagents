"""Responses API summary parts are display text, not opaque reasoning state."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from aiohttp import web

from nagents.adapters._validation import ProtocolError
from nagents.adapters.responses import ResponseAccumulator
from nagents.events import ErrorEvent
from nagents.events import ReasoningChunkEvent
from nagents.events import TextDoneEvent
from nagents.provider.openai import OpenAIProvider
from nagents.types import Message
from tests.providers.test_openai_provider import completion
from tests.providers.test_openai_provider import credentials
from tests.providers.test_openai_provider import endpoint
from tests.providers.test_openai_provider import sse
from tests.providers.test_openai_provider import text_item

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pytest import MonkeyPatch


def summary_part_done(output_index: int, summary_index: int, text: str) -> dict[str, object]:
    return {
        "type": "response.reasoning_summary_part.done",
        "output_index": output_index,
        "summary_index": summary_index,
        "part": {"type": "summary_text", "text": text},
    }


def reasoning_item(identity: str, *parts: str, encrypted: str = "opaque-reasoning") -> dict[str, object]:
    return {
        "type": "reasoning",
        "id": identity,
        "summary": [{"type": "summary_text", "text": text} for text in parts],
        "encrypted_content": encrypted,
    }


def terminal(*items: dict[str, object]) -> dict[str, object]:
    return {"status": "completed", "output": list(items)}


def chunks(events: Sequence[object]) -> str:
    return "".join(event.chunk for event in events if isinstance(event, ReasoningChunkEvent))


@pytest.mark.parametrize("streaming", [False, True])
def test_summary_part_done_supplies_body_after_streamed_heading(streaming: bool) -> None:
    accumulator = ResponseAccumulator(streaming=streaming)
    events = accumulator.add(
        {"type": "response.reasoning_summary_text.delta", "output_index": 0, "summary_index": 0, "delta": "Plan\n"}
    )
    part_events = accumulator.add(summary_part_done(0, 0, "Plan\nFirst paragraph."))
    if streaming:
        # The completed summary part is useful before response.completed arrives.
        assert chunks(events + part_events) == "Plan\nFirst paragraph."
    events += part_events
    events += accumulator.finish(terminal(reasoning_item("reason-0", "Plan\nFirst paragraph.")))

    assert chunks(events) == "Plan\nFirst paragraph."
    assert "opaque-reasoning" not in repr(events)
    assert next(event for event in events if isinstance(event, TextDoneEvent)).text == ""


def test_multiple_summary_parts_and_items_keep_paragraph_boundaries_and_dedupe_snapshots() -> None:
    accumulator = ResponseAccumulator()
    events: list[object] = []
    # The heading arrives as text deltas; completed part snapshots may repeat.
    events += accumulator.add(
        {"type": "response.reasoning_summary_text.delta", "output_index": 0, "summary_index": 0, "delta": "First"}
    )
    events += accumulator.add(
        {"type": "response.reasoning_summary_text.delta", "output_index": 0, "summary_index": 0, "delta": "\n"}
    )
    part = summary_part_done(0, 0, "First\nBody one.")
    events += accumulator.add(part)
    events += accumulator.add(part)
    events += accumulator.add(summary_part_done(0, 1, "Second\nBody two."))
    events += accumulator.add(
        {
            "type": "response.output_item.done",
            "output_index": 1,
            "item": reasoning_item("reason-1", "Third\nBody three."),
        }
    )
    events += accumulator.add(summary_part_done(1, 0, "Third\nBody three."))
    events += accumulator.finish(
        terminal(
            reasoning_item("reason-0", "First\nBody one.", "Second\nBody two."),
            reasoning_item("reason-1", "Third\nBody three."),
        )
    )

    assert chunks(events) == "First\nBody one.Second\nBody two.Third\nBody three."
    assert "opaque-reasoning" not in repr(events)


def test_final_only_summary_is_displayed_and_usage_does_not_create_summary() -> None:
    accumulator = ResponseAccumulator(streaming=False)
    events = accumulator.finish(terminal(reasoning_item("reason-0", "Final only.")))
    assert chunks(events) == "Final only."

    usage_only = ResponseAccumulator(streaming=False).finish(
        {"status": "completed", "output": [], "usage": {"input_tokens": 12, "output_tokens": 4, "total_tokens": 16}}
    )
    assert chunks(usage_only) == ""


@pytest.mark.parametrize(
    "event",
    [
        {
            "type": "response.reasoning_summary_part.done",
            "output_index": -1,
            "summary_index": 0,
            "part": {"type": "summary_text", "text": "x"},
        },
        {
            "type": "response.reasoning_summary_part.done",
            "output_index": 0,
            "summary_index": 1024,
            "part": {"type": "summary_text", "text": "x"},
        },
        {
            "type": "response.reasoning_summary_part.done",
            "output_index": 0,
            "summary_index": 0,
            "item_id": "",
            "part": {"type": "summary_text", "text": "x"},
        },
    ],
)
def test_malformed_summary_part_identity_or_index_is_rejected(event: dict[str, object]) -> None:
    with pytest.raises(ProtocolError):
        ResponseAccumulator().add(event)


def test_summary_item_identity_cannot_change_between_parts() -> None:
    accumulator = ResponseAccumulator()
    accumulator.add(
        {
            **summary_part_done(0, 0, "First"),
            "item_id": "reason-a",
        }
    )
    with pytest.raises(ProtocolError):
        accumulator.add(
            {
                **summary_part_done(0, 1, "Second"),
                "item_id": "reason-b",
            }
        )


def test_terminal_heading_prefix_cannot_discard_a_received_part_body() -> None:
    accumulator = ResponseAccumulator(streaming=True)
    streamed = accumulator.add(summary_part_done(0, 0, "Heading\nBody received in the part event."))
    final = accumulator.finish(terminal(reasoning_item("reason-0", "Heading")))
    assert chunks(streamed + final) == "Heading\nBody received in the part event."


@pytest.mark.asyncio
async def test_codex_sse_summary_part_done_reaches_display_events(monkeypatch: MonkeyPatch) -> None:
    async def handle(request: web.Request) -> web.Response:
        assert request.path.endswith("/responses")
        return web.Response(
            text=sse(
                [
                    {
                        "type": "response.reasoning_summary_text.delta",
                        "output_index": 0,
                        "summary_index": 0,
                        "delta": "Heading\n",
                    },
                    summary_part_done(0, 0, "Heading\nBody."),
                    completion([reasoning_item("reason-0", "Heading\nBody."), text_item("Answer")]),
                ]
            ),
            content_type="text/event-stream",
        )

    async with endpoint(monkeypatch, handle), OpenAIProvider(credentials) as provider:
        events = [event async for event in provider.generate([Message(role="user", content="Hi")], stream=True)]

    assert not any(isinstance(event, ErrorEvent) for event in events)
    assert chunks(events) == "Heading\nBody."
    assert next(event for event in events if isinstance(event, TextDoneEvent)).text == "Answer"
