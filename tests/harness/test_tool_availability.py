"""Advertisement follows capabilities without replacing the execution boundary."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from nagents.harness import Harness
from nagents.harness.config import AgentProfile
from nagents.harness.config import HarnessConfig
from nagents.tools.registry import ToolRegistry
from nagents.types import ToolCall
from tests.support.config import connection

if TYPE_CHECKING:
    from pathlib import Path

    from nagents.harness.types import ApprovalRequest


def names(harness: Harness) -> set[str]:
    return {tool.name for tool in harness.agent.tool_registry.get_all()}


@pytest.mark.parametrize("read_only", [False, True])
def test_inherited_mode_depth_and_root_scheduler_are_resolved_for_each_request(tmp_path: Path, read_only: bool) -> None:
    async def scenario() -> None:
        root = Harness(
            HarnessConfig(
                tmp_path,
                data_dir=tmp_path / "data",
                providers=connection(auth="api-key"),
                read_only=read_only,
                profiles={"reviewer": AgentProfile(mode="reviewer")},
            )
        )
        child = root.tasks._create_child("assistant")
        original = {tool.name: tool for tool in ToolRegistry.get_all(child.agent.tool_registry)}

        async def scheduler(owner: str, seconds: float, reason: str) -> dict[str, str]:
            raise AssertionError("This fixture only inspects availability")

        def custom() -> str:
            raise AssertionError("An ordinary child must not execute custom tools")

        child.agent.register_tool(custom)
        child.agent.register_tool(custom, name="read_file")
        try:
            assert "delegate" in names(child)
            assert not {"schedule_wakeup", "wake_up_in", "custom", "read_file"} & names(child)
            assert ("shell" in names(child)) is not read_only
            root.wakeup_handler = scheduler
            assert "schedule_wakeup" in names(child)
            assert "wake_up_in" not in names(child)
            assert child.agent.tool_registry.get("wake_up_in") is original["wake_up_in"]
            child.wakeup_handler = None  # Scheduling is owned by the root client, not this child.
            assert "schedule_wakeup" in names(child)
            root.config.max_subagent_depth = 1
            assert "delegate" not in names(child)
            denied = await child.agent.tool_executor.execute(ToolCall("depth", "delegate", {"prompt": "no child"}))
            assert denied.error and "depth limit" in denied.error
            root.config.max_subagent_depth = 2
            assert "delegate" in names(child)
            root.allow_subagents = False
            assert "delegate" not in names(child)
            root.allow_subagents = True
            await root.set_agent("reviewer")
            assert not {"edit", "write", "shell", "custom", "read_file"} & names(child)
            denied = await child.agent.tool_executor.execute(
                ToolCall("write", "write", {"path": "no", "content": "no"})
            )
            assert denied.error and "Read-only" in denied.error
            await root.set_agent("assistant")
            assert ("shell" in names(child)) is not read_only
            root.wakeup_handler = None
            assert not {"schedule_wakeup", "wake_up_in"} & names(child)
            assert all(
                child.agent.tool_registry.get(name) is tool for name, tool in original.items() if name != "read_file"
            )
        finally:
            await child.close()
            await root.close()

    asyncio.run(scenario())


def test_visibility_never_approves_custom_tools_or_weakens_alias_and_reviewer_denials(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = Harness(
            HarnessConfig(tmp_path, data_dir=tmp_path / "data", profiles={"reviewer": AgentProfile(mode="reviewer")})
        )
        approvals: list[str] = []
        effects: list[str] = []

        def custom() -> str:
            effects.append("executed")
            return "done"

        async def deny(request: ApprovalRequest) -> bool:
            approvals.append(request.tool)
            return False

        harness.approval_handler = deny
        harness.agent.register_tool(custom, name="read_file")
        try:
            assert "read_file" in names(harness)
            denied = await harness.agent.tool_executor.execute(ToolCall("custom", "read_file", {}))
            assert denied.error and approvals == ["read_file"] and not effects
            await harness.set_agent("reviewer")
            assert "read_file" not in names(harness)
            denied = await harness.agent.tool_executor.execute(ToolCall("reviewer", "read_file", {}))
            assert denied.error and "Read-only" in denied.error and approvals == ["read_file"]
            for alias in ("schedule_wakeup", "wake_up_in"):
                assert alias not in names(harness)
                unavailable = await harness.agent.tool_executor.execute(
                    ToolCall(alias, alias, {"seconds": 1, "reason": "test"})
                )
                assert unavailable.error and "unavailable" in unavailable.error
            harness.tool_settings.save({"reviewer": {"schedule_wakeup": False}}, "")
            denied = await harness.agent.tool_executor.execute(
                ToolCall("alias", "wake_up_in", {"seconds": 1, "reason": "test"})
            )
            assert denied.error and "disabled" in denied.error
            assert not effects and approvals == ["read_file"]
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_demo_catalog_keeps_unavailable_definitions(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = Harness(HarnessConfig(tmp_path, data_dir=tmp_path / "data", demo=True, max_subagent_depth=0))
        try:
            raw = {tool.name for tool in ToolRegistry.get_all(harness.agent.tool_registry)}
            assert {"edit", "write", "shell", "delegate", "schedule_wakeup", "wake_up_in"} <= raw
            assert not {"edit", "write", "shell", "delegate", "schedule_wakeup", "wake_up_in"} & names(harness)
            assert {"read_file", "list_files", "demo_preview"} <= names(harness)
            harness.config.max_subagent_depth = 2
            harness.refresh_instructions()
            assert "delegate" in names(harness)
            assert "end this model turn" in str(harness.agent.system_prompt)
        finally:
            await harness.close()

    asyncio.run(scenario())
