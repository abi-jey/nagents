"""Workspace switches affect advertised and executable tools; compaction stays in-context."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from nagents import Agent
from nagents.events import CompactionDoneEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.events import ToolResultEvent
from nagents.extensions import AgentPlugin
from nagents.extensions import CompactionResult
from nagents.harness import Harness
from nagents.harness.config import AgentProfile
from nagents.harness.config import HarnessConfig
from nagents.harness.tool_config import WorkspaceTools
from nagents.session import SessionManager
from nagents.types import Message
from tests.harness.test_harness import ScriptedProvider
from tests.support.web import client_app
from tests.support.web import no_guarded_workspace_io as no_guarded_workspace_io

if TYPE_CHECKING:
    from pathlib import Path

    from nagents.events import Event
    from nagents.extensions import CompactionRequest
    from nagents.extensions import RunContext
    from nagents.harness.types import ApprovalRequest


def test_workspace_tool_file_roundtrip_conflicts_and_validation(tmp_path: Path) -> None:
    policy = WorkspaceTools(tmp_path)
    policy.save({"build": {"shell": False, "schedule_wakeup": False}, "reviewer": {"read_file": True}}, "")
    loaded = WorkspaceTools(tmp_path)
    loaded.load()
    assert loaded.agents == policy.agents
    assert not loaded.enabled("build", "shell")
    assert not loaded.enabled("build", "wake_up_in")
    assert loaded.enabled("reviewer", "read_file")
    with pytest.raises(FileExistsError):
        loaded.save({}, "")
    for invalid in (
        "version: 1\nagents: {}\nagents: {}\n",
        "version: 1\nagents: {build: {shell: 'false'}}\n",
        "version: 1\nagents: &a {build: *a}\n",
        "version: 1\nagents: {}\nextra: true\n",
    ):
        with pytest.raises(ValueError):
            WorkspaceTools.parse(invalid)


def test_disabled_tools_hidden_and_denied_but_still_editable_in_catalog(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = HarnessConfig(
            tmp_path,
            data_dir=tmp_path / "data",
            demo=True,
            profiles={"reviewer": AgentProfile(mode="reviewer")},
        )
        async with client_app(tmp_path, config=config) as (_, client, headers, harnesses):
            first = (await client.get("/api/tools", headers=headers)).json()
            assert {item["id"] for item in first["profiles"]} == {"assistant", "reviewer"}
            assert "compact_history" in {tool["name"] for tool in first["tools"]}
            response = await client.post(
                "/api/tools",
                headers=headers,
                json={
                    "revision": first["revision"],
                    "agents": {"assistant": {"read_file": False, "schedule_wakeup": False}},
                },
            )
            assert response.status_code == 200, response.text
            assert (tmp_path / ".ngn" / "tools.yaml").is_file()
            harness = harnesses[0]
            assert "read_file" not in {tool.name for tool in harness.agent.tool_registry.get_all()}
            assert "wake_up_in" not in {tool.name for tool in harness.agent.tool_registry.get_all()}
            assert "read_file" in {tool["name"] for tool in response.json()["tools"]}
            from nagents.types import ToolCall

            denied = await harness.agent.tool_executor.execute(
                ToolCall(id="blocked", name="read_file", arguments={"path": "missing.txt"})
            )
            assert denied.error and "disabled" in denied.error
            assert "missing" not in denied.error
            conflict = await client.post(
                "/api/tools", headers=headers, json={"revision": first["revision"], "agents": {}}
            )
            assert conflict.status_code == 409
            await harness.set_agent("reviewer")
            assert "read_file" in {tool.name for tool in harness.agent.tool_registry.get_all()}
        async with client_app(tmp_path) as (_, client, headers, harnesses):
            assert "read_file" not in {tool.name for tool in harnesses[0].agent.tool_registry.get_all()}
            stored = (await client.get("/api/tools", headers=headers)).json()
            enabled = await client.post(
                "/api/tools", headers=headers, json={"revision": stored["revision"], "agents": {}}
            )
            assert enabled.status_code == 200
            assert "read_file" in {tool.name for tool in harnesses[0].agent.tool_registry.get_all()}

    asyncio.run(scenario())


def test_compact_history_runs_after_tool_results_and_before_next_model_round(tmp_path: Path) -> None:
    requests: list[CompactionRequest] = []

    class Strategy:
        async def should_compact(self, request: CompactionRequest) -> bool:
            return False

        async def compact(self, request: CompactionRequest) -> CompactionResult:
            requests.append(request)
            assert request.force
            assert {message.tool_call_id for message in request.messages if message.role == "tool"} == {
                "compact-one",
                "compact-two",
            }
            return CompactionResult([Message(role="compaction_summary", content="Retained facts")], "Retained facts")

    async def scenario() -> None:
        harness = Harness(HarnessConfig(tmp_path, data_dir=tmp_path / "data", demo=True))
        provider = ScriptedProvider(
            [
                [
                    ToolCallEvent(id="compact-one", name="compact_history", arguments={}),
                    ToolCallEvent(id="compact-two", name="compact_history", arguments={}),
                ],
                [TextDoneEvent(text="Continued with compacted context")],
            ]
        )
        harness.agent.provider = provider
        harness.agent.compaction_strategy = Strategy()
        try:
            with (
                patch.object(harness.tools, "instructions", return_value=""),
                patch.object(harness.tools, "discover_skills"),
            ):
                events = [event async for event in harness.run("Compact this conversation")]
            assert len(requests) == 1
            done = next(index for index, event in enumerate(events) if isinstance(event, CompactionDoneEvent))
            results = [index for index, event in enumerate(events) if isinstance(event, ToolResultEvent)]
            assert len(results) == 2 and max(results) < done
            assert any(message.content == "Retained facts" for message in provider.requests[-1])
            assert not harness.agent._session_compaction_requests
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_tool_disabled_during_approval_never_executes_and_disappears_from_next_request(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = Harness(HarnessConfig(tmp_path, data_dir=tmp_path / "data", auth="api-key"))
        provider = ScriptedProvider(
            [[ToolCallEvent(id="custom", name="custom_tool", arguments={})], [TextDoneEvent(text="disabled")]]
        )
        harness.agent.provider = provider
        called = False

        async def custom_tool() -> str:
            nonlocal called
            called = True
            return "must not execute"

        async def approve(request: ApprovalRequest) -> bool:
            assert request.tool == "custom_tool"
            harness.tool_settings.save({harness.config.agent: {"custom_tool": False}}, "")
            return True

        harness.agent.register_tool(custom_tool)
        harness.approval_handler = approve
        try:
            with (
                patch.object(harness.tools, "instructions", return_value=""),
                patch.object(harness.tools, "discover_skills"),
            ):
                events = [event async for event in harness.run("try the tool")]
            assert not called
            assert any(
                isinstance(event, ToolResultEvent) and event.error and "disabled" in event.error for event in events
            )
            assert "custom_tool" in {tool.name for tool in provider.schemas[0]}
            assert "custom_tool" not in {tool.name for tool in provider.schemas[-1]}
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_scoped_compaction_request_does_not_survive_cancelled_run(tmp_path: Path) -> None:
    class CancelAfterTool(AgentPlugin):
        async def on_event(self, context: RunContext, event: Event) -> None:
            if isinstance(event, ToolResultEvent):
                raise asyncio.CancelledError

    async def scenario() -> None:
        provider = ScriptedProvider(
            [[ToolCallEvent(id="request", name="request_compact", arguments={})], [TextDoneEvent(text="next session")]]
        )
        agent = Agent(provider, SessionManager(tmp_path / "sessions.db"), plugins=[CancelAfterTool()])

        def request_compact() -> str:
            agent.trigger_compaction("first")
            return "requested"

        agent.register_tool(request_compact)
        try:
            with pytest.raises(asyncio.CancelledError):
                _ = [event async for event in agent.run("start", session_id="first")]
            assert not agent._session_compaction_requests
            events = [event async for event in agent.run("continue", session_id="second")]
            assert not any(isinstance(event, CompactionDoneEvent) for event in events)
        finally:
            await agent.close()

    asyncio.run(scenario())
