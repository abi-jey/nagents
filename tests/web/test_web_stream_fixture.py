"""The real session stream stays usable with a consumer waiting during startup."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

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
