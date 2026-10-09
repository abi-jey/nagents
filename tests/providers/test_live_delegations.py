"""Concurrent Live work stays bounded, correlated, and owned by its connection."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from nagents.live.runtime import ClientDelegations
from nagents.types import ImageContent
from nagents.types import TextContent
from nagents.web.live_handoff import LoginDelegations
from tests.support.hang_guard import HANG_GUARD

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable

    from nagents.live.delegation import ClientDelegationHandler
    from nagents.live.delegation import ClientDelegationObserver
    from nagents.live.delegation import ClientDelegationRequest
    from nagents.types import ContentPart

Lane = ClientDelegations | LoginDelegations
Payload = dict[str, object]


def lane_for(
    native: bool,
    handler: ClientDelegationHandler,
    send: Callable[[Payload], Awaitable[None]],
    observer: ClientDelegationObserver,
) -> Lane:
    async def unused(context: str, identifier: str) -> str:
        raise AssertionError("Typed handler must receive the original request")

    if native:
        return LoginDelegations(handler, send, observer=observer, concurrency=2)
    return ClientDelegations(unused, send, handler=handler, observer=observer, concurrency=2)


def notice(lane: Lane, identifier: str) -> None:
    event: Payload = {
        "type": "session.delegation.created",
        "delegation": {"id": identifier, "target": "client"},
        "offset_ms": 12,
    }
    if isinstance(lane, LoginDelegations):
        lane.observe(
            event,
            {
                "offset_ms": 12,
                "item": {"type": "delegation", "content": [{"type": "input_text", "text": identifier}]},
            },
        )
    else:
        lane.observe(event)


def speech(lane: Lane, text: str) -> None:
    event: Payload = {"type": "session.input_transcript.delta", "delta": text, "start_ms": 0, "end_ms": 10}
    if isinstance(lane, LoginDelegations):
        lane.observe(event, {})
    else:
        lane.observe(event)


@pytest.mark.parametrize("native", [False, True])
def test_saturated_workers_observe_every_request_immediately_with_frozen_context(native: bool) -> None:
    async def scenario() -> None:
        observed: list[ClientDelegationRequest] = []
        called: list[ClientDelegationRequest] = []
        first, second, both = asyncio.Event(), asyncio.Event(), asyncio.Event()
        results: asyncio.Queue[Payload] = asyncio.Queue()

        async def backend(request: ClientDelegationRequest) -> str:
            called.append(request)
            if len(called) == 2:
                both.set()
            if request.identifier == "first":
                await first.wait()
            elif request.identifier == "second":
                await second.wait()
            return request.identifier

        lane = lane_for(native, backend, results.put, observed.append)
        speech(lane, "Original request")
        notice(lane, "first")
        notice(lane, "second")
        worker = asyncio.create_task(lane.run())
        try:
            async with asyncio.timeout(HANG_GUARD):
                await both.wait()
                speech(lane, "Queued correction")
                notice(lane, "third")
                notice(lane, "third")
                speech(lane, "Later unrelated speech")
                assert [request.identifier for request in observed] == ["first", "second", "third"]
                assert len(called) == 2 and lane.pending.qsize() == 1
                assert "Queued correction" in observed[2].transcript
                assert "Later unrelated" not in observed[2].transcript
                second.set()
                assert (await results.get())["delegation_id"] == "second"
                assert (await results.get())["delegation_id"] == "third"
                assert not first.is_set()
                assert called == observed
                assert all(request.offset_ms == 12 for request in called)
                first.set()
                assert (await results.get())["delegation_id"] == "first"
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("send_failure", [False, True])
def test_worker_shutdown_cancels_every_handler_without_sending_cleanup_results(
    native: bool, send_failure: bool
) -> None:
    async def scenario() -> None:
        second_started = asyncio.Event()
        cancelled = asyncio.Event()
        results: list[Payload] = []

        async def backend(request: ClientDelegationRequest) -> str:
            if request.identifier == "first" and send_failure:
                await second_started.wait()
                return "Cannot send"
            second_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                return "Do not speak after close"
            raise AssertionError("Unreachable")

        async def send(event: Payload) -> None:
            results.append(event)
            if send_failure:
                raise OSError("Transport failed")

        lane = lane_for(native, backend, send, lambda request: None)
        notice(lane, "first")
        notice(lane, "second")
        worker = asyncio.create_task(lane.run())
        try:
            async with asyncio.timeout(HANG_GUARD):
                await second_started.wait()
                if send_failure:
                    with pytest.raises(OSError, match="Transport failed"):
                        await worker
                else:
                    worker.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await worker
            assert cancelled.is_set() and lane.closed
            assert [event["content"] for event in results] == (["Cannot send"] if send_failure else [])
            notice(lane, "after-close")
            assert "after-close" not in lane.seen
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(scenario())


def test_typed_handler_rejects_application_input_before_admission_instead_of_discarding_it() -> None:
    async def scenario() -> None:
        observed: list[ClientDelegationRequest] = []

        async def handler(request: ClientDelegationRequest) -> str:
            raise AssertionError("Unsupported input must not invoke a voice handler")

        async def send(event: Payload) -> None:
            raise AssertionError("Unsupported input must not send a voice update")

        lane = lane_for(False, handler, send, observed.append)
        assert isinstance(lane, ClientDelegations)
        submissions: tuple[list[ContentPart], ...] = (
            [TextContent(text="New typed request")],
            [ImageContent(base64_data="YQ==", media_type="image/png")],
        )
        for content in submissions:
            with pytest.raises(ValueError, match="route application input"):
                await lane.submit(content)
        assert not observed and not lane.inputs and not lane.requests and lane.pending.empty()

    asyncio.run(scenario())
