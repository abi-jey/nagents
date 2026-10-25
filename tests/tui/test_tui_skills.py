"""Skill discovery and invocation share the normal terminal command/run path."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from textual.widgets import Input
from textual.widgets import OptionList
from textual.widgets import Static

from nagents.harness import Harness
from nagents.harness.config import HarnessConfig
from nagents.tui import NagentsApp
from nagents.tui.commands import SlashMenu
from nagents.tui.screens import ChoiceModal
from nagents.tui.widgets import Composer
from tests.support.tui import FakeHarness
from tests.support.tui import idle
from tests.support.tui import make_app
from tests.support.tui import send

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.harness.types import HarnessEvent


def install_skill(
    workspace: Path, name: str, description: str = "Inspect evidence", *, content: str = "Skill evidence: $ARGUMENTS\n"
) -> Path:
    path = workspace / ".agents" / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n{content}")
    return path


@pytest.mark.requires_posix
def test_skills_picker_refreshes_filters_and_prefills_without_running(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = Harness(HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "state", demo=True))
        app = NagentsApp(harness)
        async with app.run_test(size=(100, 30)) as pilot:
            await idle(app, pilot)
            install_skill(tmp_path, "audit", "Inspect evidence")
            install_skill(tmp_path, "help", "Explain the project")
            await send(app, pilot, "/skills evidence")
            await idle(app, pilot)
            await pilot.pause()
            assert isinstance(app.screen, ChoiceModal)
            assert app.screen.heading == "SKILLS"
            assert app.screen.query_one(Input).value == "evidence"
            options = app.screen.query_one(OptionList)
            assert options.option_count == 1 and options.get_option_at_index(0).id == "audit"
            assert sum(name == "audit" for name, _ in app.screen.choices) == 1
            assert not any(name == "skill:audit" for name, _ in app.screen.choices)
            assert any(name == "skill:help" for name, _ in app.screen.choices)
            assert not any(name == "help" for name, _ in app.screen.choices)
            await pilot.press("enter")
            assert app.query_one(Composer).text == "/audit "
            assert app.focused is app.query_one(Composer)
            assert await harness.history() == []
            app.query_one(Composer).load_text("/")
            app.query_one(Composer).move_cursor((0, 1))
            app._refresh_completions()
            await app._discover_command_skills()
            names = [command.name for command in app.query_one(SlashMenu).matches]
            assert "audit" in names and "skill:audit" not in names and "skill:help" in names
            await send(app, pilot, "/audit inspect README.md")
            await idle(app, pilot)
            first = next(message for message in await harness.history() if message.role == "user")
            assert "Skill evidence: inspect README.md" in str(first.content)
            assert str(first.content).count("inspect README.md") == 1

    asyncio.run(scenario())


@pytest.mark.requires_posix
@pytest.mark.parametrize("template", [False, True])
def test_fresh_direct_skill_preserves_literal_arguments_and_removed_skill_fails(tmp_path: Path, template: bool) -> None:
    async def scenario() -> None:
        harness = Harness(HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "state", demo=True))
        app = NagentsApp(harness)
        async with app.run_test(size=(100, 30)) as pilot:
            await idle(app, pilot)
            assert harness.commands.get("fresh") is None
            path = install_skill(
                tmp_path, "fresh", content="Skill evidence: $ARGUMENTS\n" if template else "Skill evidence.\n"
            )
            arguments = '  "quoted path" {literal} $(no-shell)\nsecond line  '
            await send(app, pilot, "/fresh " + arguments)
            await idle(app, pilot)
            first = next(message for message in await harness.history() if message.role == "user")
            if template:
                assert str(first.content).endswith("Skill evidence: " + arguments + "\n")
            else:
                assert str(first.content).endswith("# Task Arguments\n" + arguments)
            assert app.query_one(Composer).prompt_history[-1] == "/fresh " + arguments
            path.unlink()
            before = len(await harness.history())
            await send(app, pilot, "/fresh another task")
            await idle(app, pilot)
            assert app._status_error and "Unknown command /fresh" in app._status_text
            assert len(await harness.history()) == before

    asyncio.run(scenario())


@pytest.mark.requires_posix
def test_skill_fallback_keeps_builtin_and_current_tool_permission(tmp_path: Path) -> None:
    async def scenario() -> None:
        install_skill(tmp_path, "help")
        harness = Harness(HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "state", demo=True))
        app = NagentsApp(harness)
        async with app.run_test(size=(100, 30)) as pilot:
            await idle(app, pilot)
            app.command("/help")
            await pilot.pause()
            assert await harness.history() == []
            await pilot.press("escape")
            await send(app, pilot, "/skill:help explicit task")
            await idle(app, pilot)
            assert any("Skill evidence: explicit task" in str(message.content) for message in await harness.history())
            before = len(await harness.history())
            harness.tool_settings.save({harness.config.agent: {"skill": False}}, "")
            await send(app, pilot, "/skill:help forbidden task")
            await idle(app, pilot)
            assert app._status_error and "disabled" in app._status_text.lower()
            assert len(await harness.history()) == before
            await send(app, pilot, "/skills help")
            await idle(app, pilot)
            assert not isinstance(app.screen, ChoiceModal)
            assert any("Enable its skill tool" in str(item.content) for item in app.query(Static))
            app.query_one(Composer).load_text("/")
            app.query_one(Composer).move_cursor((0, 1))
            app._refresh_completions()
            await app._discover_command_skills()
            assert all(command.source != "skill" for command in app.query_one(SlashMenu).matches)

    asyncio.run(scenario())


class DeferredHarness(FakeHarness):
    def __init__(self, workspace: Path) -> None:
        super().__init__(workspace)
        self.release = asyncio.Event()

    async def run(self, prompt: str) -> AsyncIterator[HarnessEvent]:
        if prompt == "hold":
            await self.release.wait()
        async for event in super().run(prompt):
            yield event


def test_unresolved_slash_command_queues_before_fresh_resolution_and_keeps_arguments(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = DeferredHarness(tmp_path)
        app = make_app(harness)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            await send(app, pilot, "hold")
            argument = "  exact arguments\nnext line  "
            await send(app, pilot, "/later " + argument)
            assert list(app._queued_prompts) == ["/later " + argument]
            harness.commands.register("later", "Discovered after queuing", prompt="Task: $ARGUMENTS")
            harness.release.set()
            await idle(app, pilot)
            assert harness.prompts == ["hold", "Task: " + argument]

    asyncio.run(scenario())


def test_empty_skills_picker_is_a_local_notice_and_never_calls_model(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = FakeHarness(tmp_path)
        app = make_app(harness)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            await send(app, pilot, "/skills ")
            await idle(app, pilot)
            assert any("No skills are available" in str(item.content) for item in app.query(Static))
            assert harness.prompts == []

    asyncio.run(scenario())
