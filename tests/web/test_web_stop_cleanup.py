"""Stopping a run remains cancellation when an owned cleanup also fails."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING
from typing import cast

import pytest

from nagents.events import TextChunkEvent
from nagents.live.delegation import ClientDelegationRequest
from nagents.web.live_bridge import MainAgentBridge
from tests.support.hang_guard import HANG_GUARD
from tests.support.web import ControlledHarness
from tests.support.web import LiveStream
from tests.support.web import client_app

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from nagents.harness.types import HarnessEvent
    from nagents.types import ContentPart
    from nagents.web.service import WebState


@pytest.mark.asyncio
@pytest.mark.parametrize("voice", [False, True])
@pytest.mark.parametrize("failure", [RuntimeError, TimeoutError])
@pytest.mark.parametrize("cancel", [False, True])
async def test_owned_run_cleanup_error_preserves_stop_without_hiding_other_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    voice: bool,
    failure: type[Exception],
    cancel: bool,
) -> None:
    entered, release, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()
    private_error = "synthetic-private-cleanup-error"

    async def fail_during_cleanup(
        harness: ControlledHarness, prompt: str | list[ContentPart]
    ) -> AsyncGenerator[HarnessEvent, None]:
        yield TextChunkEvent(chunk="Working")
        entered.set()
        try:
            await release.wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()
            raise failure(private_error)

    monkeypatch.setattr(ControlledHarness, "run", fail_during_cleanup)
    async with (
        client_app(tmp_path) as (app, client, headers, _),
        asyncio.timeout(HANG_GUARD),
        AsyncExitStack() as cleanup,
    ):
        state = cast("WebState", app.state.web)
        reports: list[dict[str, object]] = []
        bridge = MainAgentBridge(state, state.selected_session_id, report=reports.append)
        if voice:

            async def append(kind: str, content: str, identifier: str) -> str:
                return f"session.{kind}.append"

            bridge.attach(append)
            cleanup.push_async_callback(bridge.close)
            await bridge.handle_request(ClientDelegationRequest("cleanup-request", "Work"))
        else:
            stream = LiveStream(app, headers, state.selected_session_id, "Work")
        await entered.wait()
        run = state.active
        assert run is not None
        if cancel:
            stopped = await client.post("/api/cancel", headers=headers, json={"run_id": run.id})
            assert stopped.status_code == 200
        else:
            release.set()
        if voice:
            while not any(report.get("status") in {"completed", "failed", "cancelled"} for report in reports):
                await asyncio.sleep(0.001)
            assert private_error not in repr(reports)
            status = next(report["status"] for report in reversed(reports) if "status" in report)
        else:
            finished = await stream.event("run_finished")
            status = str(finished["status"])
            await stream.disconnect()
        await state.channels.join_claim_cleanup(run.session_id)
        assert cleaned.is_set() and run.task.done() and state.active is None
        assert status == run.outcome == ("cancelled" if cancel else "failed")
        assert ("Run producer failed during cancellation:" in caplog.text) is cancel
        assert private_error not in caplog.text
