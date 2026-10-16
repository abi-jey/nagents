"""Navigator behavior changes preserve routing, Live and permission ceilings."""

from __future__ import annotations

import asyncio
import copy
from typing import TYPE_CHECKING
from typing import TypedDict
from typing import cast

import pytest

from nagents.harness.agent_behavior import behavior_store
from nagents.harness.model_bindings import ModelBinding
from tests.support.web import ControlledHarness
from tests.support.web import client_app
from tests.support.web import no_guarded_workspace_io as no_guarded_workspace_io
from tests.web.test_agent_models import configuration

if TYPE_CHECKING:
    from pathlib import Path

    import httpx


class BehaviorValues(TypedDict):
    instructions: str
    tools: list[str]


class ToolRow(TypedDict):
    name: str
    available: bool
    reason: str


class BehaviorReply(TypedDict):
    revision: str
    values: BehaviorValues
    origins: dict[str, str]
    profiles: list[dict[str, str]]
    native_instructions: str
    tools: list[ToolRow]
    effective_tools: list[str]


async def read(
    client: httpx.AsyncClient, headers: dict[str, str], scope: str, agent: str = "assistant"
) -> BehaviorReply:
    response = await client.get(f"/api/agent-settings?scope={scope}&agent={agent}", headers=headers)
    assert response.status_code == 200, response.text
    return cast("BehaviorReply", response.json())


async def save(
    client: httpx.AsyncClient, headers: dict[str, str], scope: str, values: dict[str, object], agent: str = "assistant"
) -> BehaviorReply:
    before = await read(client, headers, scope, agent)
    response = await client.put(
        "/api/agent-settings",
        headers=headers,
        json={"scope": scope, "agent": agent, "revision": before["revision"], "preferences": values},
    )
    assert response.status_code == 200, response.text
    return cast("BehaviorReply", response.json())


def test_effective_defaults_empty_values_and_section_preservation(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path, config=configuration(tmp_path)) as (app, client, headers, _):
            harness = app.state.web.harness
            store = harness.provider_store.agent_models
            before = store.load("assistant")
            store.patch(
                "assistant",
                {"model": {"provider": "home", "id": "chosen"}, "live": {"voice": "sol"}},
                expected=before.revision,
            )
            await save(client, headers, "global", {"instructions": "GLOBAL_BEHAVIOR", "tools": ["read_file", "search"]})
            workspace = await save(client, headers, "workspace", {"instructions": "WORKSPACE_BEHAVIOR"})
            assert workspace["values"] == {"instructions": "WORKSPACE_BEHAVIOR", "tools": ["read_file", "search"]}
            inherited = await read(client, headers, "agent")
            assert inherited["origins"] == {"instructions": "workspace", "tools": "global"}
            assert {item["id"] for item in inherited["profiles"]} == {"assistant", "mini"}
            assert "ngn default instructions" in inherited["native_instructions"]
            emptied = await save(client, headers, "agent", {"instructions": "", "tools": []})
            assert emptied["values"] == {"instructions": "", "tools": []}
            assert emptied["origins"] == {"instructions": "agent", "tools": "agent"}
            harness.refresh_instructions()
            assert "WORKSPACE_BEHAVIOR" not in harness.agent.system_prompt
            assert harness.agent.tool_registry.get_all() == []
            restored = await save(client, headers, "agent", {})
            assert restored["values"] == inherited["values"]
            harness.refresh_instructions()
            assert "WORKSPACE_BEHAVIOR" in harness.agent.system_prompt
            assert {tool.name for tool in harness.agent.tool_registry.get_all()} <= {"read_file", "search"}
            saved = store.load("assistant")
            assert saved.binding == ModelBinding("home", "chosen") and saved.model_present
            assert saved.preferences["live"] == {"voice": "sol"}
            assert not {"instructions", "tools"} & saved.preferences.keys()

    asyncio.run(scenario())


def test_authored_instructions_restore_and_tools_cannot_bypass_restrictions(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = configuration(tmp_path)
        config.profiles["mini"].tools = ("read_file", "search")
        config.profiles["mini"].denied_tools = ("shell",)
        async with client_app(tmp_path, config=config) as (app, client, headers, _):
            harness = app.state.web.harness
            harness.tool_settings.save({"mini": {"search": False}}, harness.tool_settings.revision)
            initial = await read(client, headers, "agent", "mini")
            assert initial["origins"]["instructions"] == "definition"
            assert initial["origins"]["tools"] == "definition"
            changed = await save(
                client, headers, "agent", {"instructions": "CUSTOM", "tools": ["read_file", "search", "shell"]}, "mini"
            )
            blocked = {item["name"]: item["reason"] for item in changed["tools"] if not item["available"]}
            assert "workspace tool" in blocked["search"]
            assert blocked["shell"]
            assert changed["effective_tools"] == ["read_file"]
            harness.config.agent = "mini"
            harness.refresh_tool_policy()
            assert harness.tool_enabled("read_file")
            assert not harness.tool_enabled("shell") and not harness.tool_enabled("search")
            restored = await save(client, headers, "agent", {}, "mini")
            assert restored["values"]["instructions"] == initial["values"]["instructions"]
            assert restored["origins"]["instructions"] == "definition"

    asyncio.run(scenario())


def test_child_retains_parent_tool_ceiling_after_preferences_are_relaxed(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path, config=configuration(tmp_path)) as (app, client, headers, _):
            parent = app.state.web.harness
            await save(client, headers, "global", {"tools": ["read_file"]})
            parent.refresh_tool_policy()
            child = ControlledHarness(copy.deepcopy(parent.config))
            try:
                child._is_subagent = True
                child.inherit_tool_permissions(parent)
                await save(client, headers, "global", {})
                child.refresh_tool_policy()
                assert parent.tool_enabled("search")
                assert child.tool_enabled("read_file")
                assert not child.tool_enabled("search")
            finally:
                await child.close()

    asyncio.run(scenario())


def test_changed_parent_or_sibling_section_rejects_stale_save(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path, config=configuration(tmp_path)) as (app, client, headers, _):
            before = await read(client, headers, "agent")
            await save(client, headers, "global", {"instructions": "NEW_GLOBAL"})
            denied = await client.put(
                "/api/agent-settings",
                headers=headers,
                json={
                    "scope": "agent",
                    "agent": "assistant",
                    "revision": before["revision"],
                    "preferences": {"instructions": "STALE"},
                },
            )
            assert denied.status_code == 409
            before = await read(client, headers, "agent")
            store = app.state.web.harness.provider_store.agent_models
            store.patch("assistant", {"live": {"voice": "sol"}}, expected=store.load("assistant").revision)
            denied = await client.put(
                "/api/agent-settings",
                headers=headers,
                json={
                    "scope": "agent",
                    "agent": "assistant",
                    "revision": before["revision"],
                    "preferences": {"tools": []},
                },
            )
            assert denied.status_code == 409
            assert "tools" not in store.load("assistant").preferences
            assert "instructions" not in store.load("assistant").preferences
            assert (
                behavior_store(app.state.web.harness.provider_store, "global")
                .load("defaults")
                .preferences["instructions"]
                == "NEW_GLOBAL"
            )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "query",
    [
        "scope=agent&scope=global",
        "scope=agent&agent=assistant&token=secret",
        "agent=../bad",
        "scope=invalid",
        "agent=assistant&agent=mini",
    ],
)
def test_query_validation_keeps_identity_bounded(tmp_path: Path, query: str) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path, config=configuration(tmp_path)) as (_, client, headers, _):
            response = await client.get(f"/api/agent-settings?{query}", headers=headers)
            assert response.status_code == 400
            response = await client.get("/api/agent-settings?scope=agent&agent=assistant")
            assert response.status_code == 403

    asyncio.run(scenario())
