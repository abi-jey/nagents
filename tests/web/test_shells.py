"""Authenticated shell controls remain usable without a running model."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from nagents.web.http.routing import RoutingStore
from tests.support.web import client_app

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.requires_posix


def test_shell_routes_auth_ownership_stop_and_private_download(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, client, headers, harnesses):
            harness = harnesses[0]
            session = harness.session_id
            path = f"/api/sessions/{session}/shells"
            assert (await client.get(path)).status_code == 403
            assert (await client.get(path, headers={**headers, "Sec-Fetch-Site": "cross-site"})).status_code == 403
            assert (await client.get(path, headers=headers)).json() == {"shells": []}

            async def approve(request: object) -> bool:
                return True

            harness.config.demo = False
            harness.approval_handler = approve
            token = harness.tools.call_id.set("test-call")
            try:
                receipt = await harness.shells.execute("printf private-output; sleep 30", background=True)
            finally:
                harness.tools.call_id.reset(token)
            shell = harness.shells.controller.get(str(receipt["shell_id"]))
            async with asyncio.timeout(3):
                while not shell.output_path or not shell.output_path.read_bytes():
                    await asyncio.sleep(0.01)
            listed = (await client.get(path, headers=headers)).json()["shells"]
            assert [item["shell_id"] for item in listed] == [shell.shell_id]
            output = await client.get(f"{path}/{shell.shell_id}/output", headers=headers)
            assert output.status_code == 200 and output.content == b"private-output"
            assert output.headers["content-type"] == "application/octet-stream"
            assert "attachment" in output.headers["content-disposition"]
            assert (
                await client.get(f"{path}/{shell.shell_id}/output?path=/etc/passwd", headers=headers)
            ).status_code == 400
            second = await app.state.web.channels.store._transaction(lambda db: RoutingStore.new_root(db, "Other"))
            foreign = f"/api/sessions/{second}/shells/{shell.shell_id}"
            assert (await client.get(foreign, headers=headers)).status_code == 404
            assert (await client.get(f"{foreign}/output", headers=headers)).status_code == 404
            assert (await client.post(f"{foreign}/stop", json={}, headers=headers)).status_code == 404
            stopped = await client.post(f"{path}/{shell.shell_id}/stop", json={}, headers=headers)
            assert stopped.status_code == 200
            assert stopped.json()["state"] == "cancelled"
            assert stopped.json()["reason"] == "user_stop"
            assert not await harness.shells.has_pending()
            assert (await client.post(f"{path}/{shell.shell_id}/stop", json={}, headers=headers)).json()[
                "reason"
            ] == "user_stop"
            assert (await client.get(path, headers=headers)).json()["shells"][0]["state"] == "cancelled"

    asyncio.run(scenario())


@pytest.mark.parametrize("blocker", ["mutation", "inbox"])
def test_shell_wake_retried_when_host_blocker_releases(tmp_path: Path, blocker: str) -> None:
    async def scenario() -> None:
        from unittest.mock import AsyncMock
        from unittest.mock import patch

        from nagents.web.execution.service import WebState

        async with client_app(tmp_path) as (app, _client, _headers, harnesses):
            state = app.state.web
            harness = harnesses[0]
            session = harness.session_id
            state.executions.owners[session] = harness
            claimed = asyncio.create_task(asyncio.Event().wait())
            resumed = asyncio.Event()

            async def resume(*args: object, **kwargs: object) -> None:
                resumed.set()

            try:
                if blocker == "mutation":
                    state.mutating = True
                else:
                    state.channels.work_tasks[session] = claimed
                with patch.object(WebState, "_resume_shells", AsyncMock(side_effect=resume)) as activation:
                    assert harness.shells.on_work_pending is not None
                    await harness.shells.on_work_pending(session)
                    await asyncio.sleep(0)
                    activation.assert_not_called()
                    assert session in state._shell_pending
                    state.mutating = False
                    state.channels.work_tasks.pop(session, None)
                    state.changed()
                    await asyncio.wait_for(resumed.wait(), 2)
                    activation.assert_awaited_once()
                    assert session not in state._shell_pending
                    run = state.run_for(session)
                    assert run is not None
                    await run.task
                    state.executions.release(run)
            finally:
                state.mutating = False
                state.channels.work_tasks.pop(session, None)
                claimed.cancel()
                await asyncio.gather(claimed, return_exceptions=True)

    asyncio.run(scenario())
