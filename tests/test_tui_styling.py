"""Painted surfaces, focus and compact layouts with synthetic TUI data only."""

from __future__ import annotations

import asyncio
from time import monotonic
from typing import TYPE_CHECKING

import pytest
from rich.color import ColorType
from textual.color import Color
from textual.widgets import Button
from textual.widgets import Static
from textual.widgets import TextArea

from nagents.harness.auth import DeviceAuthorization
from nagents.harness.subagents import TaskInfo
from nagents.harness.types import ApprovalRequest
from nagents.tui.dictation import DictationModal
from nagents.tui.login import DEVICE_URL
from nagents.tui.login import DeviceLoginModal
from nagents.tui.login import LoginMethodModal
from nagents.tui.screens import ApprovalModal
from nagents.tui.tasks import AgentTree
from nagents.tui.tasks import TaskScreen
from nagents.tui.themes import THEME_NAMES
from nagents.tui.widgets import Composer
from nagents.tui.widgets import ToolCard
from nagents.types import Message

from .test_tui import FakeHarness
from .test_tui import idle
from .test_tui import make_app
from .test_tui import send
from .test_tui_dictation import FakeVoice

if TYPE_CHECKING:
    from pathlib import Path

    from rich.style import Style


def assert_readable(style: Style, *, native: bool) -> None:
    """Check resolved colors, not just CSS tokens that may be tinted at paint time."""
    assert style.color is not None and style.bgcolor is not None
    if native:
        assert style.bgcolor.type is ColorType.DEFAULT
        assert style.color.type in {ColorType.DEFAULT, ColorType.STANDARD}
    else:
        luminances = []
        for color in (style.color, style.bgcolor):
            channels = [channel / 255 for channel in color.get_truecolor()]
            linear = [
                channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4 for channel in channels
            ]
            luminances.append(
                sum(channel * weight for channel, weight in zip(linear, (0.2126, 0.7152, 0.0722), strict=True))
            )
        assert (max(luminances) + 0.05) / (min(luminances) + 0.05) >= 4.5, style


@pytest.mark.parametrize("name", THEME_NAMES)
@pytest.mark.parametrize("background", ["terminal", "theme"])
@pytest.mark.parametrize("size", [(60, 20), (132, 38)])
def test_surfaces_focus_and_modal_layouts(tmp_path: Path, name: str, background: str, size: tuple[int, int]) -> None:
    async def scenario() -> None:
        backend = FakeHarness(tmp_path)
        backend.config.theme = name
        backend.config.theme_background = background
        backend.config.animations = False
        backend.config.demo = False
        tasks = [TaskInfo("one", "calm finch", status="completed")]
        backend.tasks.list = lambda: tasks
        backend.messages = [
            Message(role="user", content="Inspect this workspace."),
            Message(role="assistant", content="## Next step\n\nRead `src/` before editing."),
        ]
        app = make_app(backend)
        native = background == "terminal"
        async with app.run_test(size=size) as pilot:
            await idle(app, pilot)
            variables = app.get_css_variables()
            panel = Color.parse(variables["ngn-panel"])
            border = Color.parse(variables["ngn-border"])
            accent = Color.parse(variables["ngn-accent"])

            def check_visible(*selectors: str) -> None:
                assert app.screen.max_scroll_x == app.screen.max_scroll_y == 0
                for selector in selectors:
                    widget = app.screen.query_one(selector)
                    region = widget.region
                    assert region.width > 0 and region.height > 0, selector
                    assert 0 <= region.x < region.right <= size[0], (selector, region)
                    assert 0 <= region.y < region.bottom <= size[1], (selector, region)
                    assert_readable(widget.visual_style.rich_style, native=native)

            check_visible("#composer", "#status")
            assert app.query_one(Composer).styles.border_top[1] == accent
            for selector in (".user", ".assistant", "#rail"):
                assert app.query_one(selector).styles.border_left[0] == ""
            assert app.query_one(".user").styles.background == panel
            if size[0] > 110:
                tree = app.query_one(AgentTree)
                tree.focus()
                tree.move_cursor(tree.task_nodes["one"])
                await pilot.pause()
                check_visible("#rail", "#agent-tree")
                assert tree.styles.background.a == 0
                row = tree.render_line(tree.task_nodes["one"].line - int(tree.scroll_y))
                selected = [segment for segment in row if "calm finch" in segment.text]
                assert selected
                for segment in selected:
                    assert segment.style is not None
                    assert_readable(segment.style, native=native)
                    assert bool(segment.style.reverse) is native
                title = app.query_one("#rail-details > CollapsibleTitle")
                title.focus()
                await pilot.pause()
                assert bool(title.styles.text_style.reverse) is native
                assert app.query_one("#rail-details").styles.background_tint.a == 0

            for state, token in (
                ("running", "tool"),
                ("complete", "success"),
                ("failed", "error"),
                ("cancelled", "warning"),
            ):
                card = ToolCard(state, "read_file", {"path": "example.py"})
                if state == "complete":
                    card.finish("Read successfully", None, 50)
                elif state == "failed":
                    card.finish(None, "Could not read file", 50)
                elif state == "cancelled":
                    card.cancel()
                await app._add(card)
                card.collapsed = False
                card.query_one("CollapsibleTitle").focus()
                await pilot.pause()
                assert card.styles.background_tint.a == 0
                assert card.styles.border_left[1] == Color.parse(variables[f"ngn-{token}"])
                for selector in ("CollapsibleTitle", ".tool-arguments", ".tool-output"):
                    assert_readable(card.query_one(selector).visual_style.rich_style, native=native)
                assert card.query_one(".tool-output").visual_style.rich_style.bgcolor == panel.rich_color
                await card.remove()

            await send(app, pilot, "wait")
            await send(app, pilot, "Inspect tests next.")
            await pilot.pause()
            check_visible("#queue-status", "#composer", "#status")
            assert backend.prompts == ["wait"]
            assert "1 queued (waiting)" in str(app.query_one("#queue-status", Static).content)
            assert app.query_one("#queue-status").styles.background.a == 0
            await pilot.press("escape")
            await idle(app, pilot)
            assert "paused" in str(app.query_one("#queue-status", Static).content)

            approval = ApprovalModal(ApprovalRequest("preview", "edit_file", "Review the exact proposal.", {}))
            await app.push_screen(approval)
            await pilot.pause()
            check_visible(".dialog-title", "#deny", "#allow")
            assert approval.focused is approval.query_one("#deny")
            for selector in ("#deny", "#allow"):
                button = approval.query_one(selector, Button)
                button.focus()
                await pilot.pause()
                assert bool(button.styles.text_style.reverse) is native
                assert button.styles.background_tint.a == 0
                assert_readable(button.visual_style.rich_style, native=native)
            await approval.dismiss(False)

            login = LoginMethodModal()
            await app.push_screen(login)
            await pilot.pause()
            check_visible("#login-chatgpt", "#login-api-key", "#login-choice-cancel")
            assert login.focused is login.query_one("#login-choice-cancel")
            await login.dismiss()

            device = DeviceLoginModal(lambda: None)
            await app.push_screen(device)
            device.show_code(DeviceAuthorization(DEVICE_URL, "TEST-1234", "synthetic", 1, monotonic() + 300))
            await pilot.pause()
            check_visible("#device-code", "#device-time", "#device-browser", "#device-copy", "#device-cancel")
            assert "Expires in" in str(device.query_one("#device-time", Static).content)
            await device.dismiss()

            task = TaskScreen(
                lambda: tasks,
                backend.task_history,
                parents=lambda: {},
            )
            dictation = DictationModal(backend.config, service=FakeVoice(backend.config))
            for modal, dialog_id, editor_id, buttons in (
                (task, "#task-dialog", "#task-followup", ("#task-close", "#task-send")),
                (dictation, "#dictation-dialog", "#dictation-preview", ("#dictation-primary", "#dictation-cancel")),
            ):
                await app.push_screen(modal)
                if modal is dictation:
                    modal.add_class("preview")
                await pilot.pause()
                assert modal.query_one(dialog_id).styles.border_top[1] == border
                editor = modal.query_one(editor_id, TextArea)
                assert editor.styles.border_top[1] == border
                editor.load_text("An editable follow-up or transcript.")
                editor.focus()
                editor.action_select_all()
                await pilot.pause()
                check_visible(editor_id, *buttons)
                assert editor.styles.border_top[1] == accent
                assert not editor.cursor_blink
                for component in ("text-area--cursor", "text-area--selection"):
                    style = editor.get_component_rich_style(component)
                    assert_readable(style, native=native)
                    assert bool(style.reverse) is native
                if modal is dictation:
                    await dictation.close()
                await modal.dismiss()

    asyncio.run(scenario())
