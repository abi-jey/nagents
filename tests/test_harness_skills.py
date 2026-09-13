"""Live, guarded Harness skills exercised through real Agent/provider boundaries."""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

import pytest

from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.harness import Harness
from nagents.harness import HarnessConfig
from nagents.harness import load_config
from nagents.skills import Skill
from tests.test_harness import ScriptedProvider
from tests.test_harness import approve

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.requires_posix


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> HarnessConfig:
    for name in tuple(os.environ):
        if name.startswith("NGN_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return HarnessConfig(workspace=workspace, data_dir=tmp_path / "data", auth="api-key", model="fake-model")


def skill_file(config: HarnessConfig, name: str, body: str, *, description: str = "Fixture skill") -> Path:
    folder = config.workspace / ".agents/skills" / name
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "SKILL.md"
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n{body}")
    return path


def test_bundled_skills_and_workspace_override_removal(config: HarnessConfig) -> None:
    async def scenario() -> None:
        harness = Harness(config)
        try:
            await harness.initialize()
            assert {"ngn-customize", "ngn-channels"} <= harness.agent.skills.keys()
            bundled = await harness.tools.skill("ngn-customize")
            path = skill_file(config, "ngn-customize", "WORKSPACE_OVERRIDE_MARKER")
            override = await harness.tools.skill("ngn-customize")
            assert "WORKSPACE_OVERRIDE_MARKER" in str(override["content"])
            assert override["location"] != bundled["location"]
            path.unlink()
            restored = await harness.tools.skill("ngn-customize")
            assert restored["location"] == bundled["location"]
            assert restored["content"] == bundled["content"]
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_tool_created_skill_is_listed_and_loaded_in_same_run(config: HarnessConfig) -> None:
    folder = config.workspace / ".agents/skills/fresh"
    folder.mkdir(parents=True)
    body = "---\nname: fresh\ndescription: Fresh in-run discovery\n---\nFRESH_SKILL_BODY\n"
    provider = ScriptedProvider(
        [
            [
                ToolCallEvent(
                    id="create-skill",
                    name="write",
                    arguments={"path": ".agents/skills/fresh/SKILL.md", "content": body},
                )
            ],
            [ToolCallEvent(id="load-skill", name="skill", arguments={"name": "fresh"})],
            [TextDoneEvent(text="done")],
        ]
    )

    async def scenario() -> None:
        harness = Harness(config)
        harness.agent.provider = provider
        harness.approval_handler = approve
        try:
            await harness.initialize()
            assert "fresh" not in harness.agent.skills
            _ = [event async for event in harness.run("Create and use the fixture skill")]
            assert len(provider.requests) == 3
            assert "Fresh in-run discovery" in str(provider.requests[1])
            assert any(
                message.role == "tool" and "FRESH_SKILL_BODY" in str(message.content)
                for message in provider.requests[2]
            )
            assert harness.commands.get("skill:fresh") is not None
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_explicit_message_reference_refreshes_added_renamed_and_removed_skills(config: HarnessConfig) -> None:
    provider = ScriptedProvider(
        [[TextDoneEvent(text="first")], [TextDoneEvent(text="second")], [TextDoneEvent(text="third")]]
    )

    async def scenario() -> None:
        harness = Harness(config)
        harness.agent.provider = provider
        try:
            await harness.initialize()
            path = skill_file(config, "fresh", "INITIAL_SKILL_BODY")
            _ = [event async for event in harness.run("Use $fresh for this task")]
            assert any(
                message.role == "user" and "INITIAL_SKILL_BODY" in str(message.content)
                for message in provider.requests[0]
            )
            assert not any(
                message.role in {"system", "developer"} and "INITIAL_SKILL_BODY" in str(message.content)
                for message in provider.requests[0]
            )
            path.write_text("---\nname: renamed\ndescription: Renamed skill\n---\nUPDATED_SKILL_BODY\n")
            _ = [event async for event in harness.run("Use $renamed now")]
            assert "fresh" not in harness.agent.skills and "renamed" in harness.agent.skills
            assert "UPDATED_SKILL_BODY" in str(provider.requests[1])
            path.unlink()
            _ = [event async for event in harness.run("Continue normally")]
            assert "renamed" not in harness.agent.skills and "renamed" not in harness.tools.skills
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_skill_budget_replaces_line_and_normal_output_caps(config: HarnessConfig) -> None:
    config.max_output = 1024
    skill_file(config, "many-lines", "short line\n" * 1600 + "FINAL_LINE_MARKER")
    skill_file(config, "unicode", "界😀" * 10000)

    async def scenario() -> None:
        harness = Harness(config)
        try:
            await harness.initialize()
            lines = await harness.tools.skill("many-lines")
            assert "FINAL_LINE_MARKER" in str(lines["content"])
            assert lines["truncated"] is False
            limited = await harness.tools.skill("unicode")
            assert limited["truncated"] is True
            assert isinstance(limited["content"], str)
            assert len(limited["content"].encode("utf-8")) <= 40000
            assert limited["token_limit"] == 10000
            assert isinstance(limited["estimated_tokens"], int) and limited["estimated_tokens"] <= 10000
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_partial_invalid_file_does_not_stale_or_break_live_catalog(config: HarnessConfig) -> None:
    path = skill_file(config, "partial", "valid body")

    async def scenario() -> None:
        harness = Harness(config)
        try:
            await harness.initialize()
            assert "partial" in harness.agent.skills
            path.write_text("---\nname:")
            await harness.agent.refresh_skills()
            assert "partial" not in harness.agent.skills
            assert harness.tools.skill_diagnostics
            assert "ngn-customize" in harness.agent.skills
            path.write_text("Recovered content without frontmatter")
            result = await harness.commands.execute("skill:partial", "Run recovered skill")
            assert "Recovered content" in result.prompt
            assert not harness.tools.skill_diagnostics
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_dynamic_skill_discovery_never_reads_symlink_or_credential_tree(config: HarnessConfig, tmp_path: Path) -> None:
    path = skill_file(config, "fixture", "safe body")
    outside = tmp_path / "outside.md"
    outside.write_text("OUTSIDE_PRIVATE_MARKER")
    hidden = skill_file(config, "credentials", "CREDENTIAL_TREE_MARKER")
    assert hidden.exists()

    async def scenario() -> None:
        harness = Harness(config)
        try:
            await harness.initialize()
            assert "credentials" not in harness.agent.skills
            path.unlink()
            path.symlink_to(outside)
            await harness.agent.refresh_skills()
            assert "fixture" not in harness.agent.skills
            with pytest.raises(ValueError, match="Unknown skill"):
                await harness.tools.skill("fixture")
            assert outside.read_text() == "OUTSIDE_PRIVATE_MARKER"
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_skill_token_budget_is_trusted_configurable(config: HarnessConfig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NGN_SKILL_TOKEN_LIMIT", "12345")
    assert load_config(config.workspace).skill_token_limit == 12345
    for value in (0, -1, True, 100001):
        with pytest.raises(ValueError, match="skill_token_limit"):
            HarnessConfig(workspace=config.workspace, skill_token_limit=value)


def test_replacing_agent_discoverer_updates_harness_commands_and_description(config: HarnessConfig) -> None:
    descriptor = Skill("memory-skill", "Dynamic memory source", "memory:guide")

    class MemorySkills:
        async def discover(self) -> tuple[Skill, ...]:
            return (descriptor,)

        async def load(self, skill: Skill) -> str:
            assert skill == descriptor
            return "MEMORY_CONTEXT_MARKER"

    async def scenario() -> None:
        harness = Harness(config)
        try:
            await harness.initialize()
            assert harness.commands.get("skill:ngn-customize") is not None
            harness.agent.skill_discoverer = MemorySkills()
            await harness.agent.refresh_skills()
            assert harness.commands.get("skill:memory-skill") is not None
            assert harness.commands.get("skill:ngn-customize") is None
            assert "Dynamic memory source" in harness.describe()
            result = await harness.commands.execute("skill:memory-skill", "Use the custom source")
            assert "MEMORY_CONTEXT_MARKER" in result.prompt
            harness.agent.skill_discoverer = None
            assert harness.commands.get("skill:memory-skill") is None
        finally:
            await harness.close()

    asyncio.run(scenario())
