"""The real session stream stays usable with a consumer waiting during startup."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from nagents.web.channel_host import ChannelHost
from tests.support import web as web_support
from tests.support.hang_guard import HANG_GUARD
from tests.support.web import LiveStream
from tests.support.web import client_app
from tests.support.web import no_guarded_workspace_io as no_guarded_workspace_io

if TYPE_CHECKING:
    from pathlib import Path


def test_output_consumer_cannot_steal_subscription_handshake(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, client, headers, harnesses):
            stream = LiveStream(app, headers, harnesses[0].session_id, "approval")
            # Register the consumer before yielding to either the socket or
            # startup tasks. Their initial snapshot must remain private.
            events: list[str] = []
            async with asyncio.timeout(HANG_GUARD):
                while True:
                    event = await stream.output.get()
                    events.append(str(event["event"]))
                    assert event["event"] != "snapshot"
                    if event["event"] == "approval":
                        response = await client.post(
                            "/api/approval",
                            headers=headers,
                            json={
                                "run_id": event["run_id"],
                                "approval_id": event["approval_id"],
                                "call_id": event["id"],
                                "decision": "allow",
                            },
                        )
                        assert response.status_code == 200
                    elif event["event"] == "run_finished":
                        assert event["status"] == "completed"
                        break
            assert "run_started" in events
            assert events.count("approval") == 2
            await stream.start_task
            await stream.disconnect()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["worker_error", "worker_complete", "socket_close", "watchdog", "outer_watchdog"])
def test_admission_reports_owner_failure_and_queue_without_payloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    async def scenario() -> None:
        release = asyncio.Event()
        waiting = asyncio.Event()

        async def worker(_host: ChannelHost) -> None:
            if failure == "worker_error":
                raise RuntimeError("SECRET-worker-exception")
            if failure != "worker_complete":
                await release.wait()

        wait = LiveStream.wait_for_admission

        async def controlled_wait(stream: LiveStream) -> None:
            waiting.set()
            if failure == "socket_close":
                for subscriber in app.state.web.bus.subscribers:
                    subscriber.close(1013)
                await stream.socket_task
            if failure == "watchdog":
                # Exercise the timeout branch directly; normal test progress
                # keeps the shared sixty-second watchdog unchanged.
                with monkeypatch.context() as local:
                    local.setattr(web_support, "HANG_GUARD", 0)
                    await wait(stream)
            else:
                await wait(stream)

        monkeypatch.setattr(ChannelHost, "worker", worker)
        monkeypatch.setattr(LiveStream, "wait_for_admission", controlled_wait)
        async with client_app(tmp_path) as (app, _, headers, harnesses):
            stream = LiveStream(app, headers, harnesses[0].session_id, "SECRET-prompt")
            try:
                if failure == "outer_watchdog":
                    await asyncio.wait_for(waiting.wait(), HANG_GUARD)
                    with pytest.raises(TimeoutError) as timeout:
                        await asyncio.wait_for(stream.start_task, 0)
                    cancelled = timeout.value.__cause__
                    assert isinstance(cancelled, asyncio.CancelledError)
                    diagnostic = "\n".join(cancelled.__notes__)
                    assert stream.start_task.cancelled()
                else:
                    with pytest.raises(AssertionError, match="Stream admission") as caught:
                        async with asyncio.timeout(HANG_GUARD):
                            await stream.start_task
                    diagnostic = str(caught.value)
                assert "queued=1,running=0,receipt=queued" in diagnostic
                assert "active_runs=0" in diagnostic
                assert "SECRET" not in diagnostic and headers["X-Ngn-Token"] not in diagnostic
                assert str(tmp_path) not in diagnostic
                if failure == "worker_error":
                    assert "inbox_worker=failed:RuntimeError" in diagnostic
                elif failure == "worker_complete":
                    assert "inbox_worker=completed" in diagnostic
                elif failure == "socket_close":
                    assert "socket=completed; close_code=1013" in diagnostic
                else:
                    assert ("cancelled by owner" if failure == "outer_watchdog" else "watchdog expired") in diagnostic
                    assert "inbox_worker=pending:worker:" in diagnostic
                    assert not stream.socket_task.done()
                    assert not app.state.web.channels.tasks[0].done()
            finally:
                release.set()
                if not stream.socket_task.done():
                    await stream.input.put({"type": "websocket.disconnect", "code": 1000})
                await asyncio.wait_for(stream.socket_task, HANG_GUARD)

    asyncio.run(scenario())
