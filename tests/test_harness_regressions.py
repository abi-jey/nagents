"""Approval identity regressions, without provider calls or workspace changes."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from nagents.harness import Harness
from nagents.harness import HarnessConfig
from nagents.types import ToolCall

if TYPE_CHECKING:
    from pathlib import Path

    from nagents.harness import ApprovalRequest


@pytest.mark.parametrize("name", ["custom", "read_file"])
@pytest.mark.parametrize("mutation", ["register", "function", "schema", "remove"])
def test_custom_tool_changed_during_approval_fails_closed(tmp_path: Path, name: str, mutation: str) -> None:
    async def scenario() -> None:
        harness = Harness(HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "state", auth="api-key"))
        approving, release = asyncio.Event(), asyncio.Event()
        calls: list[str] = []

        async def original(path: str) -> str:
            calls.append("original")
            return path

        async def replacement(path: str) -> str:
            calls.append("replacement")
            return path

        async def approve(request: ApprovalRequest) -> bool:
            assert request.tool == name and request.arguments == {"path": "fixture.txt"}
            approving.set()
            await release.wait()
            return True

        harness.approval_handler = approve
        definition = harness.agent.register_tool(original, name=name)
        task = asyncio.create_task(harness.agent.tool_executor.execute(ToolCall("call", name, {"path": "fixture.txt"})))
        try:
            await asyncio.wait_for(approving.wait(), 3)
            if mutation == "register":
                harness.agent.register_tool(replacement, name=name)
            elif mutation == "function":
                definition.func = replacement
            elif mutation == "schema":
                definition.parameters["properties"]["path"]["type"] = "integer"
            else:
                harness.agent.tool_registry.clear()
            release.set()
            result = await asyncio.wait_for(task, 3)
            assert result.error and "changed during approval" in result.error
            assert result.id == "call" and result.name == name
            assert not calls
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("allowed", [False, True])
def test_stable_builtin_replacement_requires_its_own_approval(tmp_path: Path, allowed: bool) -> None:
    async def scenario() -> None:
        harness = Harness(HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "state", auth="api-key"))
        calls: list[str] = []
        approvals: list[ApprovalRequest] = []

        async def replacement(path: str) -> str:
            calls.append(path)
            return "custom result"

        async def approve(request: ApprovalRequest) -> bool:
            approvals.append(request)
            request.arguments["path"] = "not the approved argument"
            return allowed

        harness.agent.register_tool(replacement, name="read_file")
        harness.approval_handler = approve
        try:
            result = await harness.agent.tool_executor.execute(ToolCall("call", "read_file", {"path": "fixture.txt"}))
            assert len(approvals) == 1 and approvals[0].id == "call"
            assert "Custom tool" in approvals[0].description
            assert calls == (["fixture.txt"] if allowed else [])
            if allowed:
                assert result.result == "custom result" and not result.error
            else:
                assert result.error and "Approval denied" in result.error
        finally:
            await harness.close()

    asyncio.run(scenario())
