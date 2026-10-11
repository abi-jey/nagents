"""Stopping a run remains cancellation when an owned cleanup also fails."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING
from typing import cast

import pytest

from nagents.events import TextChunkEvent
from nagents.web.live_bridge import MainAgentBridge
from tests.support.hang_guard import HANG_GUARD
from tests.support.web import ControlledHarness
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
    async with client_app(tmp_path) as (app, client, headers, _), asyncio.timeout(HANG_GUARD):
        state = cast("WebState", app.state.web)
        reports: list[dict[str, object]] = []
        bridge = MainAgentBridge(state, state.selected_session_id, report=reports.append)
        if voice:
            spoken = asyncio.create_task(bridge.handle(json.dumps([{"speaker": "user", "text": "Work"}])))
        else:
            chat = asyncio.create_task(
                client.post(
                    "/api/run", headers=headers, json={"session_id": state.selected_session_id, "prompt": "Work"}
                )
            )
        await entered.wait()
        run = state.active
        assert run is not None
        if cancel:
            stopped = await client.post("/api/cancel", headers=headers, json={"run_id": run.id})
            assert stopped.status_code == 200
        else:
            release.set()
        if voice:
            result = await spoken
            assert private_error not in result + repr(reports)
            status = reports[-1]["status"]
        else:
            response = await chat
            assert private_error not in response.text
            records = [json.loads(line) for line in response.text.splitlines() if line]
            status = next(record["status"] for record in records if record["event"] == "run_finished")
        assert cleaned.is_set() and run.task.done() and state.active is None
        assert status == run.outcome == ("cancelled" if cancel else "failed")
        assert ("Run producer failed during cancellation:" in caplog.text) is cancel
        assert private_error not in caplog.text
