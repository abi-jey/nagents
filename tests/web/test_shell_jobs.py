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
            path = f"/api/sessions/{session}/shell-jobs"
            assert (await client.get(path)).status_code == 403
            assert (await client.get(path, headers={**headers, "Sec-Fetch-Site": "cross-site"})).status_code == 403
            assert (await client.get(path, headers=headers)).json() == {"jobs": []}

            async def approve(request: object) -> bool:
                return True

            harness.config.demo = False
            harness.approval_handler = approve
            token = harness.tools.call_id.set("test-call")
            try:
                receipt = await harness.shell_jobs.execute("printf private-output; sleep 30", background=True)
            finally:
                harness.tools.call_id.reset(token)
            job = harness.shell_jobs.controller.get(str(receipt["job_id"]))
            async with asyncio.timeout(3):
                while not job.output_path or not job.output_path.read_bytes():
                    await asyncio.sleep(0.01)
            listed = (await client.get(path, headers=headers)).json()["jobs"]
            assert [item["job_id"] for item in listed] == [job.job_id]
            output = await client.get(f"{path}/{job.job_id}/output", headers=headers)
            assert output.status_code == 200 and output.content == b"private-output"
            assert output.headers["content-type"] == "application/octet-stream"
            assert "attachment" in output.headers["content-disposition"]
            assert (
                await client.get(f"{path}/{job.job_id}/output?path=/etc/passwd", headers=headers)
            ).status_code == 400
            second = await app.state.web.channels.store._transaction(lambda db: RoutingStore.new_root(db, "Other"))
            foreign = f"/api/sessions/{second}/shell-jobs/{job.job_id}"
            assert (await client.get(foreign, headers=headers)).status_code == 404
            assert (await client.get(f"{foreign}/output", headers=headers)).status_code == 404
            assert (await client.post(f"{foreign}/stop", json={}, headers=headers)).status_code == 404
            stopped = await client.post(f"{path}/{job.job_id}/stop", json={}, headers=headers)
            assert stopped.status_code == 200
            assert stopped.json()["state"] == "cancelled"
            assert stopped.json()["reason"] == "user_stop"
            assert not await harness.shell_jobs.has_pending()
            assert (await client.post(f"{path}/{job.job_id}/stop", json={}, headers=headers)).json()[
                "reason"
            ] == "user_stop"
            assert (await client.get(path, headers=headers)).json()["jobs"][0]["state"] == "cancelled"

    asyncio.run(scenario())
