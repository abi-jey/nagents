"""Workspace tool grants through the terminal approval surface."""

from __future__ import annotations

import asyncio
import sqlite3
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from textual.widgets import Button
from textual.widgets import Static

from nagents.harness import Harness
from nagents.harness.config import HarnessConfig
from nagents.harness.types import ApprovalRequest
from nagents.tui import NagentsApp
from nagents.tui.screens import ApprovalModal
from nagents.web.execution.approvals import ToolApprovals
from tests.support.config import connection
from tests.support.hang_guard import HANG_GUARD
from tests.support.tui import idle

if TYPE_CHECKING:
    from pathlib import Path

    from textual.pilot import Pilot


async def example(value: str = "") -> str:
    """Return a harmless test value."""
    return value


async def replacement(value: str = "") -> str:
    """A replacement definition needs its own approval."""
    return "new:" + value


async def pending(app: NagentsApp, pilot: Pilot[None], *, mandatory: bool = False) -> asyncio.Task[bool]:
    task = asyncio.create_task(
        app.request_approval(ApprovalRequest("call", "example", "Review", {}, mandatory=mandatory))
    )
    async with asyncio.timeout(HANG_GUARD):
        while not isinstance(app.screen, ApprovalModal) or not app.screen.query("#deny"):
            await pilot.pause(0.01)
    await pilot.pause()
    return task


@pytest.mark.parametrize("decision", ["allow", "always", "deny"])
def test_workspace_decisions_and_saved_grant_reuse(tmp_path: Path, decision: str) -> None:
    async def scenario() -> None:
        harness = Harness(HarnessConfig(tmp_path, data_dir=tmp_path / "state", providers=connection(auth="api-key")))
        app = NagentsApp(harness)
        async with app.run_test(size=(80, 24)) as pilot:
            await idle(app, pilot)
            harness.agent.register_tool(example)
            task = await pending(app, pilot)
            assert app.screen.focused is app.screen.query_one("#deny", Button)
            assert app.screen.query_one("#always").region.bottom <= 24
            assert await pilot.click(f"#{decision}")
            assert await task is (decision != "deny")
            approvals = app._tool_approvals
            assert approvals is not None
            binding = approvals.requested_binding(harness, ApprovalRequest("call", "example", "", {}))
            assert binding is not None
            assert approvals.allowed(binding) is (decision == "always")
            if decision == "always":
                # A fresh store instance also reads the grant (not an in-memory UI flag).
                app._tool_approvals = ToolApprovals(approvals.path, tmp_path)
                assert await app.request_approval(ApprovalRequest("next", "example", "", {"value": "other"}))
                mandatory = await pending(app, pilot, mandatory=True)
                assert not app.screen.query("#always")
                await pilot.press("escape")
                assert not await mandatory
                harness.agent.register_tool(replacement, name="example")
            again = await pending(app, pilot)
            await pilot.press("escape")
            assert not await again

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["changed", "database", "closed", "cancelled"])
def test_always_revalidates_and_fails_closed(tmp_path: Path, failure: str) -> None:
    async def scenario() -> None:
        harness = Harness(HarnessConfig(tmp_path, data_dir=tmp_path / "state", providers=connection(auth="api-key")))
        app = NagentsApp(harness)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            harness.agent.register_tool(example)
            task = await pending(app, pilot)
            approvals = app._tool_approvals
            assert approvals is not None
            binding = approvals.requested_binding(harness, ApprovalRequest("call", "example", "", {}))
            assert binding is not None
            if failure == "changed":
                harness.agent.register_tool(replacement, name="example")
            elif failure == "closed":
                harness._closed = True
            elif failure == "cancelled":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                await pilot.pause()
            with patch.object(approvals, "grant", side_effect=sqlite3.OperationalError("unavailable")) as grant:
                if failure != "cancelled":
                    assert await pilot.click("#always")
                    assert not await task
                assert grant.call_count == (1 if failure == "database" else 0)
            assert not approvals.allowed(binding)

    asyncio.run(scenario())


@pytest.mark.parametrize("size", [(80, 24), (48, 18)])
def test_ephemeral_scope_and_direct_modal_decisions(tmp_path: Path, size: tuple[int, int]) -> None:
    async def scenario() -> None:
        from tests.support.tui import FakeHarness
        from tests.support.tui import make_app

        app = make_app(FakeHarness(tmp_path))
        async with app.run_test(size=size) as pilot:
            await idle(app, pilot)
            answers: list[bool | str | None] = []
            modal = ApprovalModal(
                ApprovalRequest("call", "example", "Review", {}), workspace=str(tmp_path), persistent=False
            )
            await app.push_screen(modal, answers.append)
            await pilot.pause()
            assert "asks again after it is reloaded" in str(modal.query_one(".approval-scope", Static).content)
            assert str(tmp_path) in str(modal.query_one(".approval-scope", Static).content)
            assert modal.focused is modal.query_one("#deny")
            for button in modal.query(Button):
                assert button.region.bottom <= size[1]
                assert button.region.right <= size[0]
            assert await pilot.click("#always")
            await pilot.pause()
            assert answers == ["always"]

    asyncio.run(scenario())
