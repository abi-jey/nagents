"""Host tool changes made while asynchronous discovery is in flight survive."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from nagents.events import TextDoneEvent
from nagents.extensions import ModelRequest
from nagents.extensions import RunContext
from nagents.types import Message
from nagents.web.tool_approvals import ToolApprovals
from tests.harness.test_resource_reload import isolate as isolate
from tests.harness.test_resource_reload import plugin
from tests.support.hang_guard import HANG_GUARD
from tests.support.providers import setup_harness

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from tests.support.providers import FakeProvider


async def host_tool(value: str = "") -> str:
    return "host:" + value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,remove", [("write", False), ("new_host_tool", False), ("write", True), ("current_session", False)]
)
async def test_host_registration_during_reload_preserves_definition_and_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, remove: bool
) -> None:
    async def script(_provider: FakeProvider, _messages: list[Message]) -> AsyncIterator[Event]:
        yield TextDoneEvent(text="done")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    extension = tmp_path / "extension.py"
    plugin(extension, "one")
    harness.config.plugins = (str(extension) + ":setup",)
    await harness.resources.reload(initial=True)
    approvals = ToolApprovals(tmp_path / "approvals.db", tmp_path)
    approvals.initialize()
    original = approvals.binding(harness, name)
    if original is not None:
        approvals.grant(original)
    entered, release = asyncio.Event(), asyncio.Event()
    refresh = harness.agent.refresh_skills

    async def gated_refresh() -> None:
        entered.set()
        await release.wait()
        await refresh()

    monkeypatch.setattr(harness.agent, "refresh_skills", gated_refresh)
    reload = asyncio.create_task(harness.resources.reload())
    try:
        await asyncio.wait_for(entered.wait(), HANG_GUARD)
        if remove:
            harness.agent.tool_registry.unregister(name)
            replacement = None
        else:
            replacement = harness.agent.register_tool(host_tool, name=name)
        release.set()
        await asyncio.wait_for(reload, HANG_GUARD)
        assert harness.agent.tool_registry.get(name) is replacement
        binding = approvals.binding(harness, name)
        if remove:
            assert binding is None
        else:
            assert binding is not None and binding != original
            assert not approvals.allowed(binding)
            assert name not in harness.resources.current.owned_names
        # An ordinary subsequent reload must retain the new host baseline.
        harness.config.plugins = ()
        await harness.resources.reload()
        assert harness.agent.tool_registry.get(name) is replacement
    finally:
        release.set()
        await asyncio.gather(reload, return_exceptions=True)
        await harness.close()


@pytest.mark.asyncio
async def test_host_change_invalidates_pinned_tool_without_invalidating_unchanged_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def script(_provider: FakeProvider, _messages: list[Message]) -> AsyncIterator[Event]:
        yield TextDoneEvent(text="done")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    extension = tmp_path / "extension.py"
    extension.write_text("""def changed():
    return 'old'
def untouched():
    return 'same'
def setup(harness):
    harness.agent.register_tool(changed)
    harness.agent.register_tool(untouched)
""")
    harness.config.plugins = (str(extension) + ":setup",)
    context = RunContext(harness.agent, harness.session_id, "test")
    try:
        await harness.resources.before_run(context, Message(role="user", content="test"))
        await harness.resources.before_model(context, ModelRequest([], harness.agent.tool_registry.get_all(), None))
        approvals = ToolApprovals(tmp_path / "approvals.db", tmp_path)
        approvals.initialize()
        original, unaffected = approvals.binding(harness, "changed"), approvals.binding(harness, "untouched")
        assert original is not None and unaffected is not None
        approvals.grant(original)
        entered, release = asyncio.Event(), asyncio.Event()
        refresh = harness.agent.refresh_skills

        async def gated_refresh() -> None:
            entered.set()
            await release.wait()
            await refresh()

        monkeypatch.setattr(harness.agent, "refresh_skills", gated_refresh)
        reload = asyncio.create_task(harness.resources.reload())
        try:
            await asyncio.wait_for(entered.wait(), HANG_GUARD)
            replacement = harness.agent.register_tool(host_tool, name="changed")
            release.set()
            await asyncio.wait_for(reload, HANG_GUARD)
            assert approvals.binding(harness, "changed") is None
            assert approvals.binding(harness, "untouched") == unaffected
            await harness.resources.after_run(context)
            assert harness.agent.tool_registry.get("changed") is replacement
            binding = approvals.binding(harness, "changed")
            assert binding is not None and binding != original and not approvals.allowed(binding)
        finally:
            release.set()
            await asyncio.gather(reload, return_exceptions=True)
    finally:
        await harness.close()
