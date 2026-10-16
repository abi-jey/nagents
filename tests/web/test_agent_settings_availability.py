"""Navigator tool availability agrees with static runtime capability gates."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from tests.support.web import client_app
from tests.support.web import no_guarded_workspace_io as no_guarded_workspace_io
from tests.web.test_agent_models import configuration
from tests.web.test_agent_settings import read

if TYPE_CHECKING:
    from pathlib import Path


def test_demo_effective_tools_match_runtime_without_enabling_denied_tools(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path, config=configuration(tmp_path)) as (_, client, headers, harnesses):
            harness = harnesses[0]
            snapshot = await read(client, headers, "agent")
            rows = {tool["name"]: tool for tool in snapshot["tools"]}
            assert not rows["shell"]["available"] and "demo" in rows["shell"]["reason"]
            assert not rows["write"]["available"]
            assert set(snapshot["effective_tools"]) == {tool.name for tool in harness.agent.tool_registry.get_all()}
            assert "shell" in snapshot["values"]["tools"]

    asyncio.run(scenario())


def test_named_reviewer_gates_do_not_leak_into_shared_defaults(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = configuration(tmp_path)
        config.profiles["mini"].mode = "reviewer"
        async with client_app(tmp_path, config=config) as (_, client, headers, harnesses):
            harness = harnesses[0]
            harness.config.demo = False
            before = harness.config.agent
            reviewer = await read(client, headers, "agent", "mini")
            rows = {tool["name"]: tool for tool in reviewer["tools"]}
            assert not rows["shell"]["available"] and "reviewer" in rows["shell"]["reason"]
            assert "shell" not in reviewer["effective_tools"] and "read_file" in reviewer["effective_tools"]
            assert harness.config.agent == before
            harness.config.agent = "mini"
            harness.config.profiles["mini"].tools = ()
            harness.config.profiles["mini"].allowed_agents = ()
            for scope in ("global", "workspace"):
                defaults = await read(client, headers, scope, "mini")
                rows = {tool["name"]: tool for tool in defaults["tools"]}
                assert rows["shell"]["available"] and rows["delegate"]["available"]
                assert "shell" in defaults["effective_tools"]
            assert harness.config.agent == "mini"

    asyncio.run(scenario())


def test_scheduler_and_static_delegation_limits_explain_unavailable_tools(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path, config=configuration(tmp_path)) as (_, client, headers, harnesses):
            harness = harnesses[0]
            harness.config.demo = False
            harness.wakeup_handler = None
            harness.config.max_subagent_depth = 0
            snapshot = await read(client, headers, "agent")
            rows = {tool["name"]: tool for tool in snapshot["tools"]}
            assert not rows["schedule_wakeup"]["available"] and "scheduler" in rows["schedule_wakeup"]["reason"]
            assert not rows["delegate"]["available"] and "depth" in rows["delegate"]["reason"]
            assert (
                "schedule_wakeup" not in snapshot["effective_tools"] and "delegate" not in snapshot["effective_tools"]
            )
            harness.config.max_subagent_depth = 2
            harness.config.profiles["mini"].allowed_agents = ()
            mini = await read(client, headers, "agent", "mini")
            rows = {tool["name"]: tool for tool in mini["tools"]}
            assert not rows["delegate"]["available"] and "eligible" in rows["delegate"]["reason"]
            assistant = await read(client, headers, "agent")
            rows = {tool["name"]: tool for tool in assistant["tools"]}
            assert rows["delegate"]["available"]
            harness.allow_subagents = False
            disabled = await read(client, headers, "agent")
            rows = {tool["name"]: tool for tool in disabled["tools"]}
            assert not rows["delegate"]["available"] and "disabled" in rows["delegate"]["reason"]

    asyncio.run(scenario())


def test_corrupt_agent_tool_policy_does_not_block_shared_defaults(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path, config=configuration(tmp_path)) as (_, client, headers, harnesses):
            policy = harnesses[0].tool_settings.path
            policy.parent.mkdir(parents=True, exist_ok=True)
            policy.write_text("invalid policy document")
            for scope in ("global", "workspace"):
                response = await client.get(f"/api/agent-settings?scope={scope}", headers=headers)
                assert response.status_code == 200, response.text
            response = await client.get("/api/agent-settings?scope=agent&agent=assistant", headers=headers)
            assert response.status_code == 422

    asyncio.run(scenario())
