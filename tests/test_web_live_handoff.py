"""Native requests remain bound to their own ID regardless of caption timing."""

from __future__ import annotations

import asyncio
import json

import pytest

from nagents.web.live_handoff import MAX_HANDOFF_PARTS
from nagents.web.live_handoff import MAX_HANDOFF_TEXT
from nagents.web.live_handoff import LoginDelegations
from nagents.web.live_handoff import LoginHandoff
from nagents.web.live_login import normalize_event
from tests.support.hang_guard import HANG_GUARD


def notice(identifier: str, text: str, offset: object = 1000) -> dict[str, object]:
    return {
        "type": "delegation.created",
        "offset_ms": offset,
        "item": {
            "id": identifier,
            "type": "delegation",
            "target": "client",
            "content": [{"type": "input_text", "text": text}],
        },
    }


def observe(lane: LoginDelegations, event: dict[str, object]) -> None:
    lane.observe(normalize_event(event), event)


def speech(lane: LoginDelegations, text: str, start: int) -> None:
    observe(lane, {"type": "input_transcript.added", "item": {"text": text}, "start_ms": start, "end_ms": start + 10})


def test_native_handoffs_freeze_their_own_text_and_history_before_queued_work_runs() -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()
        calls: list[LoginHandoff] = []
        results: asyncio.Queue[dict[str, object]] = asyncio.Queue()

        async def backend(request: LoginHandoff) -> str:
            calls.append(request)
            if len(calls) == 1:
                entered.set()
                await release.wait()
            return "Result for " + request.identifier

        lane = LoginDelegations(backend, results.put)
        worker = asyncio.create_task(lane.run())
        try:
            async with asyncio.timeout(HANG_GUARD):
                observe(lane, notice("first", "First task"))
                await entered.wait()  # Native request does not require captions.
                speech(lane, "Earlier speech", 10)
                second = notice("second", "Second task", 2000)
                observe(lane, second)
                # Mutation or a duplicate delivery cannot replace a queued task.
                second["item"] = {
                    "id": "second",
                    "type": "delegation",
                    "target": "client",
                    "content": [{"type": "input_text", "text": "Changed duplicate"}],
                }
                observe(lane, second)
                speech(lane, "Third task spoken later", 3000)
                observe(lane, notice("third", "Third task", 4000))
                observe(lane, notice("third-extra", "Third task", 4100))
                received = [await results.get() for _ in range(3)]
                assert [result["delegation_id"] for result in received] == ["second", "third", "third-extra"]
                release.set()
                received.append(await results.get())
                # Late captions are observations, never a request to replay tools.
                speech(lane, "Second task transcript arrived last", 1500)
                observe(lane, notice("second", "Replay attempt"))
                await asyncio.sleep(0)
            assert [call.text for call in calls] == ["First task", "Second task", "Third task", "Third task"]
            assert [call.identifier for call in calls] == ["first", "second", "third", "third-extra"]
            assert calls[0].transcript == "[]"
            assert [part["text"] for part in json.loads(calls[1].transcript)] == ["Earlier speech"]
            assert [part["text"] for part in json.loads(calls[2].transcript)] == [
                "Earlier speech",
                "Third task spoken later",
            ]
            assert calls[1].offset_ms == 2000
            assert {str(result["delegation_id"]): result["content"] for result in received} == {
                call.identifier: "Result for " + call.identifier for call in calls
            }
            assert lane.pending.empty() and results.empty()
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "content",
    [
        None,
        "not a list",
        [],
        [None],
        [{}],
        [{"type": "input_text"}],
        [{"type": "input_text", "text": True}],
        [{"type": "input_text", "text": 7}],
        [{"type": "input_text", "text": " \n "}],
        [{"type": "input_text", "text": "x" * (MAX_HANDOFF_TEXT + 1)}],
        [{"type": "input_text", "text": "x" * MAX_HANDOFF_TEXT}, {"type": "input_text", "text": "y"}],
        [{"type": "input_text", "text": "x"}] * (MAX_HANDOFF_PARTS + 1),
    ],
)
def test_invalid_native_content_returns_same_id_explanation_without_reusing_prior_speech(content: object) -> None:
    async def scenario() -> None:
        calls: list[LoginHandoff] = []
        results: asyncio.Queue[dict[str, object]] = asyncio.Queue()

        async def backend(request: LoginHandoff) -> str:
            calls.append(request)
            return "Valid new result"

        lane = LoginDelegations(backend, results.put)
        worker = asyncio.create_task(lane.run())
        try:
            speech(lane, "An earlier task that must never run again", 0)
            raw: dict[str, object] = {
                "type": "delegation.created",
                "item": {"id": "invalid", "type": "delegation", "target": "client", "content": content},
            }
            observe(lane, raw)
            observe(lane, notice("invalid", "A changed duplicate must not execute"))
            async with asyncio.timeout(HANG_GUARD):
                failure = await results.get()
                assert not calls
                assert failure["delegation_id"] == "invalid"
                assert "repeat" in str(failure["content"]) and len(str(failure["content"])) < 200
                observe(lane, notice("valid", "A different valid request"))
                assert await results.get() == {"delegation_id": "valid", "content": "Valid new result"}
            assert [call.text for call in calls] == ["A different valid request"]
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(scenario())


def test_native_text_joining_is_exact_and_history_has_an_independent_bound() -> None:
    async def backend(request: LoginHandoff) -> str:
        raise AssertionError("This test inspects queued requests only")

    async def send(event: dict[str, object]) -> None:
        raise AssertionError("No worker was started")

    lane = LoginDelegations(backend, send)
    speech(lane, "old" * 4000, 0)
    speech(lane, "recent", 50)
    observe(
        lane,
        {
            "type": "delegation.created",
            "item": {
                "id": "exact",
                "type": "delegation",
                "target": "client",
                "content": [
                    {"type": "input_text", "text": " very"},
                    {"type": "output_text", "text": "not request text"},
                    {"type": "input_text", "text": " very good "},
                ],
            },
        },
    )
    request = lane.pending.get_nowait()
    assert request.text == " very very good "
    assert json.loads(request.transcript) == [{"speaker": "user", "text": "recent", "start_ms": 50, "end_ms": 60}]
    assert request.offset_ms is None
    observe(lane, notice("maximum", "界" * MAX_HANDOFF_TEXT))
    assert lane.pending.get_nowait().text == "界" * MAX_HANDOFF_TEXT


def test_native_snapshot_orders_late_fragments_by_timing_without_merging_or_changing_text() -> None:
    async def backend(request: LoginHandoff) -> str:
        raise AssertionError("No worker was started")

    async def send(event: dict[str, object]) -> None:
        raise AssertionError("No worker was started")

    lane = LoginDelegations(backend, send)
    speech(lane, " Thursday,", 200)
    speech(lane, "Book Friday.", 100)
    speech(lane, " not Friday.", 200)
    observe(lane, notice("corrected", "Check the corrected date"))
    request = lane.pending.get_nowait()
    assert [part["text"] for part in json.loads(request.transcript)] == [
        "Book Friday.",
        " Thursday,",
        " not Friday.",
    ]
    assert request.text == "Check the corrected date"


@pytest.mark.parametrize(
    "offset,expected",
    [
        (0, 0),
        (2.5, 2.5),
        (None, None),
        (True, None),
        (-1, None),
        (float("nan"), None),
        (float("inf"), None),
        ("5", None),
    ],
)
def test_native_offset_is_preserved_only_as_valid_observed_chronology(offset: object, expected: object) -> None:
    event = normalize_event(notice("offset", "A request", offset))
    assert event.get("offset_ms") == expected
    assert ("offset_ms" in event) == (expected is not None)


def test_native_pending_requests_remain_bounded_and_duplicates_do_not_consume_slots() -> None:
    async def backend(request: LoginHandoff) -> str:
        raise AssertionError("No worker was started")

    async def send(event: dict[str, object]) -> None:
        raise AssertionError("No worker was started")

    lane = LoginDelegations(backend, send)
    for index in range(32):
        observe(lane, notice(str(index), "Queued request"))
        observe(lane, notice(str(index), "Repeated notice"))
    assert lane.pending.qsize() == 32
    with pytest.raises(RuntimeError, match="queue is full"):
        observe(lane, notice("overflow", "Cannot queue this request"))
    assert lane.pending.qsize() == 32
