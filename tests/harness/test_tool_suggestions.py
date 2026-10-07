"""Unknown-tool hints follow the exposed tool list without changing inventory."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.events import ToolResultEvent
from nagents.harness import Harness
from nagents.tools.executor import ToolExecutor
from nagents.tools.registry import ToolRegistry
from nagents.types import ToolCall
from tests.support.providers import collect
from tests.support.providers import setup_harness

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.harness import ApprovalRequest
    from nagents.types import Message
    from tests.support.providers import FakeProvider


@pytest.mark.asyncio
@pytest.mark.parametrize("scheduler_available", [False, True])
async def test_unknown_hint_matches_model_exposure_and_refreshes_after_policy_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scheduler_available: bool
) -> None:
    # Unknown-tool suggestions are portable and do not discover workspace files.
    monkeypatch.setattr(Harness, "load_project_instructions", lambda self: None)

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if len(provider.requests) % 2:
            yield ToolCallEvent(id="wrong", name="missing_tool", arguments={})
        else:
            yield TextDoneEvent(text="The unavailable tool was not executed.")

    harness, providers = setup_harness(tmp_path, monkeypatch, script)

    async def approve(request: ApprovalRequest) -> bool:
        raise AssertionError("Unknown and disabled calls must never request approval")

    harness.approval_handler = approve

    async def schedule(owner: str, seconds: float, reason: str) -> dict[str, str]:
        raise AssertionError("Unknown-tool suggestions must never schedule work")

    harness.wakeup_handler = schedule if scheduler_available else None
    try:
        harness.tool_settings.save({"assistant": {"read_file": False, "schedule_wakeup": False}}, "")
        inventory = harness.agent.tool_registry.names()
        definitions = ToolRegistry.get_all(harness.agent.tool_registry)
        events = await collect(harness)
        failure = next(event for event in events if isinstance(event, ToolResultEvent))
        assert failure.error is not None
        suggestions = failure.error.partition("Available tools: ")[2].split(", ")
        assert suggestions == providers[0].schemas[0]
        assert not {"read_file", "schedule_wakeup", "wake_up_in"}.intersection(suggestions)
        assert {"read_file", "schedule_wakeup", "wake_up_in"}.issubset(inventory)
        assert harness.agent.tool_registry.names() == inventory

        denied = await harness.agent.tool_executor.execute(ToolCall("disabled", "read_file", {"path": "missing"}))
        assert denied.error and "disabled for this agent" in denied.error
        harness.tool_settings.save({}, harness.tool_settings.revision)
        for callback in (harness.wakeup_handler, None, schedule):
            harness.wakeup_handler = callback
            first_request = len(providers[0].requests)
            events = await collect(harness)
            restored = next(event for event in events if isinstance(event, ToolResultEvent))
            assert restored.error is not None
            suggestions = restored.error.partition("Available tools: ")[2].split(", ")
            assert suggestions == providers[0].schemas[first_request]
            assert "read_file" in suggestions
            assert ("schedule_wakeup" in suggestions) == (callback is not None)
            assert "wake_up_in" not in suggestions
            assert suggestions == [
                name
                for name in inventory
                if name != "wake_up_in" and (callback is not None or name != "schedule_wakeup")
            ]
            assert harness.agent.tool_registry.names() == inventory
            assert all(harness.agent.tool_registry.get(tool.name) is tool for tool in definitions)
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_all_disabled_tools_remain_registered_without_being_suggested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        yield TextDoneEvent(text="Unused")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    try:
        inventory = harness.agent.tool_registry.names()
        assert inventory
        harness.tool_settings.save({"assistant": dict.fromkeys(inventory, False)}, "")
        result = await harness.agent.tool_executor.execute(ToolCall("unknown", "missing_tool", {}))
        assert result.error == "Tool 'missing_tool' does not exist. No tools are available."
        assert harness.agent.tool_registry.get_all() == []
        assert harness.agent.tool_registry.names() == inventory
        assert harness.agent.tool_registry.get("read_file") is not None
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_generic_registry_suggestions_keep_existing_identity_and_registration_behavior() -> None:
    registry = ToolRegistry()
    executor = ToolExecutor(registry)
    result = await executor.execute(ToolCall("empty", "missing", {}))
    assert result.error == "Tool 'missing' does not exist. No tools are available."

    def available() -> None:
        raise AssertionError("Generating a suggestion must not execute another tool")

    registry.register(available)
    result = await executor.execute(ToolCall("unknown", "missing", {}))
    assert result.error == "Tool 'missing' does not exist. Available tools: available"
    assert result.id == "unknown" and result.name == "missing"
    assert registry.names() == ["available"]
