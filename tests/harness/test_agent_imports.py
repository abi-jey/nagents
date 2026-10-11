"""Imported definitions reach the ordinary harness without expanding permissions."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest

from nagents.cli import main
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.harness.config import AgentProfile
from nagents.harness.config import HarnessConfig
from nagents.harness.config import load_config
from nagents.harness.runtime import Harness
from nagents.harness.subagents import TaskInfo
from nagents.harness.types import TaskCompleted
from nagents.types import ToolCall
from tests.support.providers import collect
from tests.support.providers import notifications
from tests.support.providers import setup_harness

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.types import Message
    from tests.support.providers import FakeProvider


def manifest(path: Path, *, name: str = "research", description: str = "Find supporting evidence") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\nReport your evidence.\n", encoding="utf-8")
    return path


def test_discovery_trust_sources_and_scope_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home, workspace = tmp_path / "home", tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    personal = manifest(home / ".config/ngn/agents/research.md", description="Personal file")
    manifest(workspace / ".claude/agents/research.md", description="Project file")
    assert load_config(workspace).profile("research").source == str(personal)
    user_config = home / ".config/ngn/config.json"
    user_config.write_text(json.dumps({"profiles": {"research": {"description": "Personal JSON"}}}))
    assert load_config(workspace).profile("research").description == "Personal JSON"
    assert load_config(workspace, user_config, trust_project=True).profile("research").description == "Personal JSON"
    trusted = load_config(workspace, trust_project=True)
    assert trusted.profile("research").description == "Project file"
    assert any("overrides" in note and str(user_config) in note for note in trusted.agent_diagnostics)
    project_config = workspace / ".ngn/config.json"
    project_config.parent.mkdir()
    project_config.write_text(json.dumps({"profiles": {"research": {"description": "Project JSON"}}}))
    explicit = tmp_path / "explicit.json"
    explicit.write_text(json.dumps({"profiles": {"research": {"description": "Explicit JSON"}}}))
    assert load_config(workspace, trust_project=True).profile("research").description == "Project JSON"
    assert load_config(workspace, explicit, trust_project=True).profile("research").description == "Explicit JSON"


def test_explicit_foreign_model_mapping_and_invalid_definition_diagnostics(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agent = manifest(workspace / ".claude/agents/research.md")
    agent.write_text(agent.read_text().replace("description:", "model: sonnet\ndescription:"))
    config = workspace / ".ngn/config.json"
    config.parent.mkdir()
    config.write_text(
        json.dumps(
            {
                "agent_imports": [
                    {"path": "../.claude/agents/research.md", "format": "claude", "model": "selected-model"}
                ]
            }
        )
    )
    loaded = load_config(workspace, config)
    assert loaded.profile("research").model == "selected-model"
    assert loaded.profile("research").source_format == "claude"
    config.write_text("{}")
    loaded = load_config(workspace, trust_project=True)
    assert "research" not in loaded.profile_names
    assert any("model" in message and str(agent) in message for message in loaded.agent_diagnostics)


@pytest.mark.parametrize("selection", ["flag", "json", "environment"])
def test_cli_rejects_child_only_startup_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], selection: str
) -> None:
    config = tmp_path / "config.json"
    values: dict[str, object] = {"profiles": {"child": {"user_invocable": False}}}
    args = ["--workspace", str(tmp_path), "--config", str(config), "--demo"]
    if selection == "flag":
        args += ["--agent", "child"]
    elif selection == "json":
        values["agent"] = "child"
    else:
        monkeypatch.setenv("NGN_AGENT", "child")
    config.write_text(json.dumps(values))
    assert main([*args, "run", "hello"]) == 2
    assert "only for delegation" in capsys.readouterr().err


@pytest.mark.requires_posix
def test_parent_policy_applies_to_advertising_execution_and_resume(tmp_path: Path) -> None:
    async def scenario() -> None:
        root = Harness(
            HarnessConfig(
                tmp_path,
                demo=True,
                data_dir=tmp_path / "data",
                agent="lead",
                profiles={
                    "lead": AgentProfile(tools=("read_file", "find", "search", "delegate")),
                    "research": AgentProfile(tools=("read_file", "find", "search", "shell", "delegate")),
                },
            )
        )
        root.tool_settings.save({"lead": {"find": False}, "research": {"find": True}}, "")
        info = TaskInfo(
            "child", "research", profile="research", child_session_id="child-session", session_id=root.session_id
        )
        child = root.tasks._create_child("research", info)
        root.tasks._infos[info.id] = info
        root.tasks._children[info.id] = child
        resumed: Harness | None = None
        try:
            assert {tool.name for tool in child.agent.tool_registry.get_all()} == {"read_file", "search", "delegate"}
            denial = await child.agent.tool_executor.execute(
                ToolCall(id="blocked", name="find", arguments={"pattern": "*"})
            )
            assert denial.error and "disabled" in denial.error
            # Restrictions at creation remain a ceiling even after a parent switches profiles.
            await root.set_agent("assistant")
            child.refresh_tool_policy()
            assert not child.tool_enabled("shell")
            assert not child.tool_enabled("find")
            # Newly tightened ancestor settings also apply to retained children.
            root.tool_settings.save({"assistant": {"search": False}}, root.tool_settings.revision)
            await child.close()
            resumed = root.tasks._create_child("research", info, continuing=True)
            assert "search" not in {tool.name for tool in resumed.agent.tool_registry.get_all()}
            denial = await resumed.agent.tool_executor.execute(
                ToolCall(id="resume", name="search", arguments={"query": "anything"})
            )
            assert denial.error and "disabled" in denial.error
        finally:
            if resumed is not None:
                await resumed.close()
            await child.close()
            await root.close()

    asyncio.run(scenario())


@pytest.mark.requires_posix
def test_imported_profile_delegates_through_normal_lifecycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    path = manifest(workspace / ".ngn/agents/research.md")
    path.write_text(
        path.read_text().replace("description:", "tools: []\nuser_invocable: false\nmodel: child-model\ndescription:")
    )
    config = load_config(workspace, trust_project=True)

    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index:
                assert provider.model == "child-model"
                assert provider.schemas[-1] == []
                assert any("Report your evidence." in str(message.content) for message in messages)
                yield TextDoneEvent(text="Verified child evidence")
            elif notifications(messages):
                yield TextDoneEvent(text="Combined findings")
            elif len(provider.requests) == 1:
                yield ToolCallEvent(
                    id="delegate", name="delegate", arguments={"prompt": "Investigate", "agent": "research"}
                )
            else:
                yield TextDoneEvent(text="Waiting for evidence")

        harness, providers = setup_harness(workspace, monkeypatch, script)
        harness.config.profiles.update(config.profiles)
        try:
            harness.refresh_instructions()
            definition = next(tool for tool in harness.agent.tool_registry.get_all() if tool.name == "delegate")
            parameter = definition.parameters["properties"]["agent"]
            assert "Find supporting evidence" in parameter["description"]
            assert "research" in parameter["enum"]
            assert "research" not in harness.config.selectable_profile_names
            with pytest.raises(ValueError, match="only for delegation"):
                await harness.set_agent("research")
            events = await collect(harness)
            assert any(
                isinstance(event, TaskCompleted) and "Verified child evidence" in event.result for event in events
            )
            assert len(providers) == 2
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.requires_posix
def test_delegation_eligibility_is_enforced_not_only_hidden(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = Harness(
            HarnessConfig(
                tmp_path,
                demo=True,
                data_dir=tmp_path / "data",
                profiles={
                    "interactive": AgentProfile(subagent_available=False),
                    "limited": AgentProfile(allowed_agents=()),
                },
            )
        )
        try:
            harness.tasks.begin(harness.session_id, reset_budget=True)
            with pytest.raises(PermissionError, match="not available"):
                await harness.tasks.delegate("Try hidden target", "interactive")
            await harness.set_agent("limited")
            assert not harness.can_delegate
            assert "delegate" not in {tool.name for tool in harness.agent.tool_registry.get_all()}
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.requires_posix
def test_leaf_agent_can_resume_repeatedly_without_gaining_tools(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = Harness(
            HarnessConfig(
                tmp_path,
                demo=True,
                data_dir=tmp_path / "data",
                profiles={
                    "leaf": AgentProfile(tools=("read_file",), allowed_agents=()),
                },
            )
        )
        info = TaskInfo(
            "leaf-task", "leaf", profile="leaf", child_session_id="leaf-session", session_id=harness.session_id
        )
        harness.tasks._infos[info.id] = info
        try:
            for activation in range(4):
                child = harness.tasks._create_child("leaf", info, continuing=activation > 0)
                harness.tasks._children[info.id] = child
                try:
                    assert child._permission_parents == (harness,)
                    assert not child.can_delegate
                    assert {tool.name for tool in child.agent.tool_registry.get_all()} == {"read_file"}
                finally:
                    await child.close()
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.requires_posix
def test_restricted_target_schema_requires_an_available_agent(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = Harness(
            HarnessConfig(
                tmp_path,
                demo=True,
                data_dir=tmp_path / "data",
                agent="lead",
                profiles={
                    "lead": AgentProfile(allowed_agents=("research",)),
                    "research": AgentProfile(description="Check evidence"),
                },
            )
        )
        try:
            harness.refresh_instructions()
            delegate = next(tool for tool in harness.agent.tool_registry.get_all() if tool.name == "delegate")
            assert "agent" in delegate.parameters["required"]
            assert delegate.parameters["properties"]["agent"]["enum"] == ["research"]
            assert "agent='research'" in (harness.agent.system_prompt or "")
            assert "agent='assistant'" not in (harness.agent.system_prompt or "")
            await harness.set_agent("assistant")
            delegate = next(tool for tool in harness.agent.tool_registry.get_all() if tool.name == "delegate")
            assert "agent" not in delegate.parameters["required"]
        finally:
            await harness.close()

    asyncio.run(scenario())
