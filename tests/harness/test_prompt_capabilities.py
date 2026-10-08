"""Generated orchestration guidance follows effective native tool contracts."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from nagents.events import TextDoneEvent
from tests.support.providers import collect
from tests.support.providers import setup_harness

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.types import Message
    from tests.support.providers import FakeProvider


async def scheduler(owner: str, seconds: float, reason: str) -> dict[str, str]:
    raise AssertionError("Only advertisement is inspected")


@pytest.mark.requires_posix
@pytest.mark.asyncio
async def test_generated_prompt_refreshes_between_runs_without_overwriting_authored_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        yield TextDoneEvent(text="Done")

    harness, providers = setup_harness(tmp_path, monkeypatch, script)
    try:
        harness.allow_subagents = False
        await collect(harness)
        initial = str(providers[0].requests[-1][0].content)
        assert "do not promise delayed self-follow-up" in initial
        assert "Scheduling acknowledges immediately" not in initial
        assert "use delegate(" not in initial
        assert "cannot delegate" in initial
        assert "host approval policy" in initial and "Read-only profiles" in initial
        harness.allow_subagents = True
        harness.wakeup_handler = scheduler
        await collect(harness)
        enabled = str(providers[0].requests[-1][0].content)
        assert "Scheduling acknowledges immediately" in enabled
        assert "use delegate(" in enabled
        assert "schedule_wakeup" in providers[0].schemas[-1]
        harness.tool_settings.save({"assistant": {"delegate": False, "schedule_wakeup": False}}, "")
        await collect(harness)
        disabled = str(providers[0].requests[-1][0].content)
        assert "Scheduling acknowledges immediately" not in disabled
        assert "use delegate(" not in disabled
        harness.agent.system_prompt = "Authored run context"
        await collect(harness)
        assert providers[0].requests[-1][0].content == "Authored run context"
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_children_use_root_scheduler_depth_and_workspace_restrictions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        yield TextDoneEvent(text="Done")

    root, _ = setup_harness(tmp_path, monkeypatch, script)
    child = root.tasks._create_child("assistant")
    try:
        root.wakeup_handler = scheduler
        child.refresh_instructions()
        assert "Scheduling acknowledges immediately" in str(child.agent.system_prompt)
        assert "use delegate(" in str(child.agent.system_prompt)
        root.config.max_subagent_depth = child.subagent_depth
        child.refresh_instructions()
        assert "use delegate(" not in str(child.agent.system_prompt)
        root.tool_settings.save({"assistant": {"schedule_wakeup": False}}, "")
        child.refresh_instructions()
        assert "Scheduling acknowledges immediately" not in str(child.agent.system_prompt)
    finally:
        await child.close()
        await root.close()


@pytest.mark.asyncio
async def test_custom_overrides_do_not_inherit_native_lifecycle_guidance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        yield TextDoneEvent(text="Done")

    def custom() -> str:
        return "Custom contract"

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    try:
        harness.wakeup_handler = scheduler
        harness.agent.register_tool(custom, name="schedule_wakeup")
        harness.refresh_instructions()
        # The still-advertised native alias retains its lifecycle contract.
        assert "Scheduling acknowledges immediately" in str(harness.agent.system_prompt)
        harness.agent.register_tool(custom, name="wake_up_in")
        harness.agent.register_tool(custom, name="delegate")
        harness.refresh_instructions()
        prompt = str(harness.agent.system_prompt)
        assert "Scheduling acknowledges immediately" not in prompt
        assert "use delegate(" not in prompt
        assert "Choose only tools advertised" in prompt
    finally:
        await harness.close()


@pytest.mark.requires_posix
@pytest.mark.asyncio
async def test_invalid_tool_settings_fail_at_initialization_and_remain_visible_between_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        yield TextDoneEvent(text="Done")

    settings = tmp_path / ".ngn" / "tools.yaml"
    settings.parent.mkdir()
    settings.write_text("version: invalid\nagents: {}\n")
    harness, providers = setup_harness(tmp_path, monkeypatch, script)
    try:
        with pytest.raises(ValueError, match="version"):
            await harness.initialize()
        assert "Harness initialization failed: ValueError" in harness.diagnostics[-1]
        settings.write_text("version: 1\nagents: {}\n")
        with pytest.raises(RuntimeError, match="Harness initialization failed"):
            await harness.initialize()
        assert not providers[0].requests
    finally:
        await harness.close()

    harness, providers = setup_harness(tmp_path, monkeypatch, script)
    try:
        await collect(harness)
        settings.write_text("version: invalid\nagents: {}\n")
        with pytest.raises(ValueError, match="version"):
            await collect(harness)
        assert len(providers[0].requests) == 1
    finally:
        await harness.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("replace_canonical", [False, True])
async def test_native_delegation_alias_uses_advertised_name_and_respects_depth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replace_canonical: bool
) -> None:
    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        yield TextDoneEvent(text="Done")

    def custom() -> str:
        return "Custom contract"

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    try:
        harness.agent.register_tool(harness.tasks.delegate, name="dispatch_child")
        if replace_canonical:
            harness.agent.register_tool(custom, name="delegate")
        else:
            harness.refresh_instructions()
            assert "use delegate(" in str(harness.agent.system_prompt)
            harness.tool_settings.save({"assistant": {"delegate": False}}, "")
        harness.refresh_instructions()
        prompt = str(harness.agent.system_prompt)
        assert "use dispatch_child(prompt, agent='assistant')" in prompt
        assert "use delegate(" not in prompt
        harness.allow_subagents = False
        harness.refresh_instructions()
        assert "use dispatch_child(" not in str(harness.agent.system_prompt)
        assert "cannot delegate" in str(harness.agent.system_prompt)
    finally:
        await harness.close()
