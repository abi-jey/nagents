"""Saved grant reads cannot block media or outlive their authority."""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING

import pytest

from nagents.web.tool_approvals import ToolApprovals
from nagents.web.tool_approvals import ToolBinding
from tests.support.hang_guard import HANG_GUARD
from tests.support.web import client_app
from tests.support.web import no_guarded_workspace_io as no_guarded_workspace_io
from tests.web.test_web_tool_approvals import config_for
from tests.web.test_web_tool_approvals import decide
from tests.web.test_web_tool_approvals import example
from tests.web.test_web_tool_approvals import finish
from tests.web.test_web_tool_approvals import real_stream
from tests.web.test_web_tool_approvals import replacement

if TYPE_CHECKING:
    from pathlib import Path


class LookupGate:
    def __init__(self, store: ToolApprovals) -> None:
        self.original = store.allowed
        self.loop = asyncio.get_running_loop()
        self.entered = asyncio.Event()
        self.release = threading.Event()
        self.thread = 0

    def __call__(self, binding: ToolBinding) -> bool:
        result = self.original(binding)
        self.thread = threading.get_ident()
        self.loop.call_soon_threadsafe(self.entered.set)
        if not self.release.wait(HANG_GUARD):
            raise TimeoutError("Fixture grant-read gate was not released")
        return result


@pytest.mark.parametrize("interruption", ["none", "revoke", "cancel"])
def test_grant_read_keeps_loop_responsive_and_joins_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interruption: str
) -> None:
    async def scenario() -> None:
        store = ToolApprovals(tmp_path / "approvals.db", tmp_path)
        store.initialize()
        binding = ToolBinding("fixture", "fixture-binding", True)
        store.grant(binding)
        gate = LookupGate(store)
        monkeypatch.setattr(store, "allowed", gate)
        check = asyncio.create_task(store.check_allowed(binding))
        try:
            await asyncio.wait_for(gate.entered.wait(), HANG_GUARD)
            assert gate.thread != threading.get_ident()
            assert not check.done(), "the event loop ran while the DB worker remained blocked"
            if interruption == "revoke":
                row = store.snapshot()[0]
                assert store.revoke("fixture", str(row["revision"]))
            elif interruption == "cancel":
                check.cancel()
                await asyncio.sleep(0)
                assert not check.done(), "cancellation must join the outstanding DB worker"
            gate.release.set()
            if interruption == "cancel":
                with pytest.raises(asyncio.CancelledError):
                    await check
            else:
                assert await check is (interruption == "none")
        finally:
            gate.release.set()
            await asyncio.gather(check, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["definition", "revoke"])
def test_inflight_saved_grant_cannot_authorize_changed_tool_or_revoked_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path, config=config_for(tmp_path)) as (app, client, headers, harnesses):
            harness = harnesses[0]
            harness.agent.register_tool(example)
            store = app.state.web.tool_approvals
            binding = store.binding(harness, "example")
            assert binding is not None
            store.grant(binding)
            gate = LookupGate(store)
            monkeypatch.setattr(store, "allowed", gate)
            stream = real_stream(app, headers, harness, ["example"])
            try:
                await asyncio.wait_for(gate.entered.wait(), HANG_GUARD)
                assert app.state.web.active is not None and not app.state.web.active.task.done()
                if change == "definition":
                    definition = harness.agent.tool_registry.get("example")
                    assert definition is not None
                    definition.func = replacement
                else:
                    row = store.snapshot()[0]
                    assert store.revoke("example", str(row["revision"]))
                gate.release.set()
                if change == "revoke":
                    pending = await stream.event("approval")
                    assert (await decide(client, headers, pending, "deny")).status_code == 200
                result = await stream.event("tool_result")
                assert "Approval denied" in str(result["error"])
                await finish(stream)
            finally:
                gate.release.set()

    asyncio.run(scenario())
