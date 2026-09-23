"""Offline aiohttp tests for the local appointment desk app (no model generation)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import examples.live._support as support_module
import examples.live.appointment_desk.app as app_module
from aiohttp.test_utils import TestClient
from aiohttp.test_utils import TestServer
from examples.live.appointment_desk.app import AppointmentApp

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

SLOT = "2030-08-06 14:00 UTC"
OTHER_SLOT = "2030-08-07 14:00 UTC"


def _forbidden(*args: object, **kwargs: object) -> object:
    raise AssertionError("OpenAIProvider must not be constructed for state/selection/approve")


def test_state_selection_approve_and_authorization(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_module, "OpenAIProvider", _forbidden)
    monkeypatch.setattr(support_module, "OpenAIProvider", _forbidden)

    async def scenario() -> None:
        application = AppointmentApp(tmp_path / "desk.db", port=0)
        server = TestServer(application.app)
        client = TestClient(server)
        await client.start_server()
        assert server.port is not None
        application.port = server.port
        base = f"http://127.0.0.1:{server.port}"
        token = application.token
        headers = {"X-Demo-Token": token, "Origin": base}
        try:
            # Origin/token enforcement.
            assert (await client.get("/state")).status == 403
            assert (await client.get("/state", headers={"X-Demo-Token": "wrong"})).status == 403
            assert (await client.post("/selection", json={"slot": SLOT}, headers={"X-Demo-Token": token})).status == 403

            # Revision 1 proposal can be approved.
            proposal = await client.post("/selection", json={"slot": SLOT}, headers=headers)
            assert proposal.status == 200
            assert (await proposal.json())["proposal"]["revision"] == 1
            approved = await client.post("/approve", json={"revision": 1}, headers=headers)
            assert approved.status == 200 and (await approved.json()) == {"approved": True}
            state = await (await client.get("/state", headers={"X-Demo-Token": token})).json()
            assert state["desk"]["draft"]["approved"] == 1

            # A new selection replaces the proposal and clears approval.
            changed = await client.post("/selection", json={"slot": OTHER_SLOT}, headers=headers)
            assert (await changed.json())["proposal"]["revision"] == 2
            state = await (await client.get("/state", headers={"X-Demo-Token": token})).json()
            assert state["desk"]["draft"]["approved"] == 0

            # Stale approval is rejected with a conflict.
            stale = await client.post("/approve", json={"revision": 1}, headers=headers)
            assert stale.status == 409
            missing = await client.post("/approve", json={"revision": 99}, headers=headers)
            assert missing.status == 409
        finally:
            await client.close()

    asyncio.run(scenario())
