"""Command discovery and execution in the actual terminal composer."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from textual.events import Key
from textual.events import Paste
from textual.widgets import OptionList
from textual.widgets import Static

from nagents.harness import Harness
from nagents.harness import HarnessConfig
from nagents.harness.commands import CommandResult
from nagents.tui import NagentsApp
from nagents.tui.commands import SlashMenu
from nagents.tui.screens import ChoiceModal
from nagents.tui.screens import DetailModal
from nagents.tui.themes import configure_theme
from nagents.tui.widgets import Composer
from tests.test_tui import FakeHarness
from tests.test_tui import idle
from tests.test_tui import make_app

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("size", [(60, 20), (80, 24), (132, 38)])
def test_slash_opens_immediately_and_preserves_composer(tmp_path: Path, size: tuple[int, int]) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        app = make_app(backend)
        async with app.run_test(size=size) as pilot:
            await idle(app, pilot)
            await pilot.press("/")
            menu = app.query_one(SlashMenu)
            composer = app.query_one(Composer)
            assert menu.display
            selected = menu.selected()
            assert selected is not None and selected.name == "help"
            assert len(menu.matches) >= 12
            assert app.focused is composer
            assert composer.region.bottom <= size[1] - 2
            assert menu.region.bottom <= composer.region.y
            assert backend.prompts == []
            await pilot.press("l", "o")
            assert [command.name for command in menu.matches[:2]] == ["login", "logout"]
            await pilot.press("down")
            selected = menu.selected()
            assert selected is not None and selected.name == "logout"
            await pilot.press("up")
            selected = menu.selected()
            assert selected is not None and selected.name == "login"
            assert composer.text == "/lo"

    asyncio.run(scenario())


def test_tab_completes_without_executing_and_keeps_arguments(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        backend.config.tab_action = "complete"
        app = make_app(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            await pilot.press("/", "m", "o", "d", "tab")
            composer = app.query_one(Composer)
            assert composer.text == "/model "
            assert not app.query_one(SlashMenu).display
            assert backend.config.model == "gpt-4.1"
            await pilot.press(*"custom/model", "enter")
            await idle(app, pilot)
            assert backend.config.model == "custom/model"
            assert backend.prompts == []

    asyncio.run(scenario())


def test_enter_selects_command_and_escape_only_closes_menu(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        app = make_app(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            await pilot.press("/", "h", "e", "escape")
            assert app.query_one(Composer).text == "/he"
            assert not app.query_one(SlashMenu).display
            await pilot.press("l", "p")
            assert app.query_one(SlashMenu).display
            await pilot.press("enter")
            assert app.query_one(Composer).text == "/help "
            await pilot.press("enter")
            assert isinstance(app.screen, DetailModal)
            assert not app.query_one(SlashMenu).display
            assert "[harness]" in app.screen.text
            assert backend.prompts == []

    asyncio.run(scenario())


def test_mouse_selects_a_prefill_without_running_it(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        app = make_app(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            await pilot.press("/", "h", "e", "l")
            await pilot.click("#slash-options", offset=(3, 0))
            assert app.query_one(Composer).text == "/help "
            assert not isinstance(app.screen, DetailModal)
            assert backend.prompts == []
            await pilot.press("enter")
            assert isinstance(app.screen, DetailModal)

    asyncio.run(scenario())


@pytest.mark.parametrize("target", ["app", "composer"])
@pytest.mark.parametrize("execute", [False, True])
def test_rapid_slash_arrows_and_enter_stay_in_input_order(tmp_path: Path, target: str, execute: bool) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        app = make_app(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            composer = app.query_one(Composer)
            composer.remember("Do not recall history while choosing a command")
            receiver = app if target == "app" else composer
            # No Pilot pauses between keys: this is one terminal input burst.
            keys = ["/", "down", "down", "up", "enter"]
            if execute:
                keys.append("enter")
            for key in keys:
                receiver.post_message(Key(key, key if len(key) == 1 else None))
            await idle(app, pilot)
            # Backend idleness does not drain Changed messages from the input burst.
            await pilot.pause()
            assert composer.text == ("" if execute else "/new ")
            assert backend.new_count == int(execute)
            assert not app.query_one(SlashMenu).display
            assert app.focused is composer
            assert backend.prompts == []

    asyncio.run(scenario())


def test_rapid_filter_and_arrows_do_not_move_text_cursor(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = make_app(FakeHarness(tmp_path))
        async with app.run_test() as pilot:
            await idle(app, pilot)
            composer = app.query_one(Composer)
            for key in ["/", "l", "o", "down", "up", "down"]:
                app.post_message(Key(key, key if len(key) == 1 else None))
            await pilot.pause()
            assert composer.text == "/lo"
            assert composer.cursor_location == (0, 3)
            selected = app.query_one(SlashMenu).selected()
            assert selected is not None and selected.name == "logout"
            assert app.focused is composer

    asyncio.run(scenario())


@pytest.mark.parametrize("key", ["enter", "tab", "shift+tab"])
def test_rapid_prefill_arguments_and_execute(tmp_path: Path, key: str) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        backend.config.tab_action = "complete"
        app = make_app(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            for value in [*"/mod", key, *"custom/model", "enter"]:
                app.post_message(Key(value, value if len(value) == 1 else None))
            await idle(app, pilot)
            assert backend.config.model == "custom/model"
            assert app.query_one(Composer).text == ""
            assert backend.prompts == []

    asyncio.run(scenario())


def test_arrow_scroll_wrap_and_mouse_selection_with_composer_focused(tmp_path: Path) -> None:
    async def scenario() -> None:
        app = make_app(FakeHarness(tmp_path))
        async with app.run_test(size=(60, 20)) as pilot:
            await idle(app, pilot)
            await pilot.press("/")
            menu = app.query_one(SlashMenu)
            composer = app.query_one(Composer)
            options = menu.query_one(OptionList)
            await pilot.press("up")
            assert menu.selected() == menu.matches[-1]
            assert options.scroll_y > 0
            await pilot.press("down")
            assert menu.selected() == menu.matches[0]
            assert options.scroll_y == 0
            for _ in range(9):
                app.post_message(Key("down", None))
            await pilot.pause()
            assert menu.selected() == menu.matches[9]
            assert composer.cursor_location == (0, 1)
            assert app.focused is composer
            await pilot.press("up", "up", "up", "up", "up", "up", "up", "up", "up")
            await pilot.click("#slash-options", offset=(3, 1))
            assert composer.text == "/new "
            assert app.focused is composer

    asyncio.run(scenario())


@pytest.mark.parametrize("name", ["terminal", "graphite", "ocean", "ember"])
def test_native_selected_command_is_visibly_reversed_without_list_focus(tmp_path: Path, name: str) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        backend.config.theme = name
        backend.config.theme_background = "terminal"
        app = make_app(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            await pilot.press("/", "down")
            options = app.query_one("#slash-options", OptionList)
            assert app.focused is app.query_one(Composer)
            assert options.get_component_styles("option-list--option-highlighted").text_style.reverse
            segments = list(options.render_line(1))
            assert all(
                segment.style is not None and segment.style.reverse for segment in segments if segment.text.strip()
            )
            for segment in segments:
                assert segment.style is not None and segment.style.color is not None
                assert segment.style.color.is_default
                assert segment.style.bgcolor is not None and segment.style.bgcolor.is_default
            configure_theme(app, name, background="theme")
            await pilot.pause()
            assert not any(segment.style and segment.style.reverse for segment in options.render_line(1))

    asyncio.run(scenario())


def test_unknown_commands_and_embedded_paths_never_autorun(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        app = make_app(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            await pilot.press(*"/does-not-exist")
            menu = app.query_one(SlashMenu)
            assert menu.display and not menu.matches
            await pilot.press("enter")
            assert "Unknown command" in str(app.query_one("#status", Static).content)
            assert backend.prompts == []
            composer = app.query_one(Composer)
            composer.load_text("Read /tmp/example")
            composer.move_cursor((0, len(composer.text)))
            await pilot.pause()
            assert not menu.display
            composer.clear()
            composer.post_message(Paste("/help\nnot another command"))
            await pilot.pause()
            assert not menu.display
            assert backend.prompts == []

    asyncio.run(scenario())


def test_live_registry_plugin_source_and_callback(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        app = make_app(backend)
        calls: list[str] = []

        async def report(harness: Harness, arguments: str) -> CommandResult:
            calls.append(arguments)
            return CommandResult(message="Plugin report is ready.")

        async with app.run_test() as pilot:
            await idle(app, pilot)
            await pilot.press("/", "r", "e", "p")
            assert not app.query_one(SlashMenu).matches
            with backend.commands.plugin_source("/private/location/reports.py:setup"):
                backend.commands.register("report", "Build a local report", handler=report)
            await pilot.pause(0.2)
            menu = app.query_one(SlashMenu)
            selected = menu.selected()
            assert selected is not None and selected.source == "plugin:reports.py"
            await pilot.press("enter", "enter")
            await idle(app, pilot)
            assert calls == [""]
            assert backend.prompts == []
            assert any(
                "Plugin report is ready." in str(widget.content) for widget in app.query(".notice").results(Static)
            )
            await pilot.press("ctrl+p")
            assert isinstance(app.screen, ChoiceModal)
            assert any(key == "report" and "plugin:reports.py" in label for key, label in app.screen.choices)

    asyncio.run(scenario())


def test_required_arguments_and_prompt_command_follow_normal_run(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        backend.commands.register(
            "review",
            "Review a named target",
            prompt="Review $ARGUMENTS",
            argument_hint="<target>",
            requires_arguments=True,
        )
        app = make_app(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            await pilot.press("/", "r", "e", "v", "enter")
            assert app.query_one(Composer).text == "/review "
            assert backend.prompts == []
            await pilot.press(*"README.md", "enter")
            await idle(app, pilot)
            assert backend.prompts == ["Review README.md"]
            assert "/review README.md" in str(app.query_one(".user Static:last-child", Static).content)

    asyncio.run(scenario())


@pytest.mark.requires_posix
def test_discovered_skill_is_invocable_from_composer(tmp_path: Path) -> None:
    directory = tmp_path / ".agents/skills/audit"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: audit\ndescription: Inspect evidence without changing files\n---\nKeep evidence separate from guesses.\n"
    )

    async def scenario() -> None:
        harness = Harness(HarnessConfig(workspace=tmp_path, demo=True))
        app = NagentsApp(harness)
        async with app.run_test(size=(100, 30)) as pilot:
            await idle(app, pilot)
            await pilot.press(*"/skill:audit", "enter")
            assert app.query_one(Composer).text == "/skill:audit "
            await pilot.press(*"Check the docs", "enter")
            await idle(app, pilot)
            history = await harness.history()
            first = next(message for message in history if message.role == "user")
            assert "Keep evidence separate from guesses" in str(first.content)
            assert "Check the docs" in str(first.content)

    asyncio.run(scenario())


def test_busy_agent_keeps_draft_when_command_cannot_run(tmp_path: Path) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        app = make_app(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            await pilot.press(*"wait", "enter")
            await pilot.pause(0.05)
            await pilot.press("/", "n", "e", "w", "enter", "enter")
            assert backend.new_count == 0
            assert app.query_one(Composer).text == "/new "
            await pilot.press("escape")
            await idle(app, pilot)
            assert backend.cancelled

    asyncio.run(scenario())


@pytest.mark.parametrize("animated", [False, True])
def test_activity_only_animates_while_busy(tmp_path: Path, animated: bool) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        backend.config.animations = animated
        app = make_app(backend)
        async with app.run_test() as pilot:
            await idle(app, pilot)
            assert str(app.query_one("#status", Static).content) == "Ready"
            await pilot.press(*"wait", "enter")
            await pilot.pause(0.2)
            app._animate_activity()
            status = str(app.query_one("#status", Static).content)
            assert status.startswith("Working") is not animated
            await pilot.press("escape")
            await idle(app, pilot)
            assert "ready for your next message" in str(app.query_one("#status", Static).content)

    asyncio.run(scenario())
