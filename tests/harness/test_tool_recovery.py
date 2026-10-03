"""Regressions for malformed calls observed during normal web harness runs."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from nagents.events import DoneEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.events import ToolResultEvent
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
async def test_misnamed_file_read_call_returns_actionable_schema_and_recovers_in_normal_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "fixture.txt").write_text("Expected file content.")

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if len(provider.requests) == 1:
            yield ToolCallEvent(id="wrong-tool", name="shell", arguments={"path": "fixture.txt", "limit": 20})
        elif len(provider.requests) == 2:
            feedback = str(messages[-1].content)
            assert "Invalid arguments for shell" in feedback
            assert "command: string (required)" in feedback
            assert "No tool was executed" in feedback
            yield ToolCallEvent(id="correct-tool", name="read_file", arguments={"path": "fixture.txt", "limit": 20})
        else:
            assert "Expected file content." in str(messages[-1].content)
            yield TextDoneEvent(text="Read the file successfully.")

    harness, providers = setup_harness(tmp_path, monkeypatch, script)
    approvals: list[str] = []

    async def approve(request: ApprovalRequest) -> bool:
        approvals.append(request.tool)
        return False

    harness.approval_handler = approve
    try:
        events = await collect(harness, "Read fixture.txt.")
        outcomes = [event for event in events if isinstance(event, ToolResultEvent)]
        assert outcomes[0].name == "shell" and outcomes[0].error
        assert outcomes[1].name == "read_file" and not outcomes[1].error
        assert approvals == []
        assert any(isinstance(event, DoneEvent) for event in events)
        assert len(providers[0].requests) == 3
        tools = {tool.name: tool.parameters for tool in harness.agent.tool_registry.get_all()}
        assert set(tools["shell"]["properties"]) == {"command", "timeout"}
        assert set(tools["read_file"]["properties"]) == {"path", "start_line", "limit"}
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_unknown_tools_never_prompt_for_approval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        yield TextDoneEvent(text="Unused")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)

    async def approve(request: ApprovalRequest) -> bool:
        raise AssertionError("An unknown tool must not ask the user for approval")

    harness.approval_handler = approve
    try:
        await harness.initialize()
        result = await harness.agent.tool_executor.execute(ToolCall("unknown", "missing", {}))
        assert result.error and "does not exist" in result.error and "read_file" in result.error
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_tool_contracts_expose_current_limits_to_root_and_native_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nagents.web.settings import SettingsValues

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        yield TextDoneEvent(text="Unused")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    try:
        await harness.initialize()
        # Exercise the same refresh used when the UI changes configured limits.
        values = SettingsValues.current(harness).model_copy(
            update={"shell_timeout": 1.25, "max_file_bytes": 4096, "max_output": 2048}
        )
        values.apply(harness)
        child = harness.tasks._create_child("assistant")
        try:
            for owner in (harness, child):
                definitions = {tool.name: tool for tool in owner.agent.tool_registry.get_all()}
                shell = definitions["shell"]
                read = definitions["read_file"]
                assert "1.25 seconds" in shell.description
                assert "2048 bytes" in shell.description
                assert (
                    "0 for the configured default of 1.25" in shell.parameters["properties"]["timeout"]["description"]
                )
                assert "4096 bytes" in read.description and "even when requesting a line slice" in read.description
                assert "1 through 1000" in read.parameters["properties"]["limit"]["description"]

            # The advertised limits agree with actual execution, before approval.
            (tmp_path / "large.txt").write_text("x" * 4097)
            timeout = await harness.agent.tool_executor.execute(
                ToolCall("long", "shell", {"command": "true", "timeout": 2})
            )
            oversized = await harness.agent.tool_executor.execute(
                ToolCall("big", "read_file", {"path": "large.txt", "limit": 1})
            )
            assert timeout.error and "1.25" in timeout.error
            assert "No command was started" in timeout.error
            assert oversized.error and "4096 byte limit" in oversized.error
            assert "requesting fewer lines will not reduce its size" in oversized.error
        finally:
            await child.close()
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_limit_refresh_preserves_custom_replacement_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        yield TextDoneEvent(text="Unused")

    async def custom_shell(source: str) -> str:
        return source

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    try:
        await harness.initialize()
        definition = harness.agent.register_tool(custom_shell, name="shell", description="Custom contract")
        harness.refresh_instructions()
        assert definition.description == "Custom contract"
        assert set(definition.parameters["properties"]) == {"source"}
        # A custom registry contract can also keep the native callable while
        # intentionally omitting its optional timeout parameter.
        definition = harness.agent.tool_registry.register(
            harness.tools.shell,
            description="Command-only contract",
            parameters={"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
        )
        harness.refresh_instructions()
        assert definition.description == "Command-only contract"
        assert definition.parameters == {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        }
    finally:
        await harness.close()
