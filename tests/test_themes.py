"""Theme/config contracts with isolated storage and no live harness or network."""

from __future__ import annotations

import asyncio
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from typing import cast
from xml.etree import ElementTree

import pytest
from rich.color import ColorType
from rich.console import Console
from textual.app import App
from textual.color import Color
from textual.containers import Horizontal
from textual.containers import Vertical
from textual.containers import VerticalScroll
from textual.content import Content
from textual.widgets import Button
from textual.widgets import Input
from textual.widgets import Label
from textual.widgets import Static
from textual.widgets import TextArea

from nagents import cli
from nagents.harness.config import THEME_BACKGROUNDS
from nagents.harness.config import THEME_NAMES
from nagents.harness.config import HarnessConfig
from nagents.harness.config import load_config
from nagents.harness.types import ApprovalRequest
from nagents.tui import themes
from nagents.tui.login import DeviceLoginModal
from nagents.tui.login import LoginMethodModal
from nagents.tui.screens import ApprovalModal
from nagents.tui.screens import ChoiceModal
from nagents.tui.widgets import CodeFence
from nagents.tui.widgets import ToolCard
from nagents.tui.widgets import Turn

if TYPE_CHECKING:
    from textual.app import ComposeResult

    from nagents.harness import Harness

CSS_PATH = Path(themes.__file__).with_name("app.tcss")


class ThemePreview(App[None]):
    """Exercise the shared stylesheet without depending on NagentsApp callbacks."""

    def __init__(self, name: str, background: str = "auto") -> None:
        super().__init__(css_path=CSS_PATH)
        themes.configure_theme(self, name, background=background)

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Static("ngn", id="brand", markup=False)
            yield Static("workspace", id="workspace", markup=False)
            yield Static("local preview", id="model", markup=False)
            yield Static("build", id="profile", markup=False)
        yield Static("THEME PREVIEW / no network", id="mode", markup=False)
        with Horizontal(id="main"):
            with VerticalScroll(id="conversation"):
                yield Static("Body inherits the selected foreground.", id="body", markup=False)
                yield Turn("user", "Inspect this workspace.")
                yield Turn(
                    "assistant",
                    "## A clear next step\n\nInspect `src/` before changing it.\n\n"
                    "```python\n# Explain before editing\ndef greet(name):\n    return 'hello', 42\n```",
                )
                card = ToolCard("preview", "read_file", {"path": "src/example.py", "limit": 42})
                card.finish({"diff": "--- a/example.py\n+++ b/example.py\n@@ -1 +1 @@\n-old\n+new"}, None, 120)
                card.collapsed = False
                yield card
                yield Static("Error: example failure, not success", classes="notice error", markup=False)
            with Vertical(id="rail"):
                yield Static("WORKSPACE", classes="section-label", markup=False)
                yield Static("No backend is connected.", id="rail-config", markup=False)
        with Vertical(id="compose-area"):
            yield Static("MESSAGE", id="compose-label", markup=False)
            yield TextArea(id="composer", placeholder="Ask anything, or / for commands", highlight_cursor_line=False)
            yield Static("Enter send   Ctrl+P commands   Esc cancel", id="shortcuts", markup=False)
            yield Static(f"{themes.activity_frame(self.theme, 0)} Preview only", id="status", markup=False)

    def on_mount(self) -> None:
        self.query_one("#rail").display = self.size.width > 110
        self.query_one(TextArea).focus()


@pytest.fixture(autouse=True)
def isolated_theme_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NGN_THEME", raising=False)
    monkeypatch.delenv("NGN_THEME_BACKGROUND", raising=False)
    monkeypatch.delenv("NGN_ANIMATIONS", raising=False)


def test_defaults_and_shared_theme_names(tmp_path: Path) -> None:
    config = load_config(tmp_path)
    assert THEME_NAMES == ("terminal", "graphite", "ocean", "ember")
    assert themes.THEME_NAMES is THEME_NAMES
    assert config.theme == "terminal"
    assert config.animations is True
    with pytest.raises(ValueError, match="theme must be"):
        HarnessConfig(workspace=tmp_path, theme="yellow")
    with pytest.raises(ValueError, match="animations must be a boolean"):
        HarnessConfig(workspace=tmp_path, animations=cast("bool", 1))


@pytest.mark.parametrize("name", THEME_NAMES)
@pytest.mark.parametrize("enabled", [True, False])
def test_theme_toml(tmp_path: Path, name: str, enabled: bool) -> None:
    path = tmp_path / "theme.toml"
    path.write_text(f'theme = "{name}"\nanimations = {str(enabled).lower()}\n', encoding="utf-8")
    config = load_config(tmp_path, path)
    assert config.theme == name
    assert config.animations is enabled


@pytest.mark.parametrize(
    "text", ['theme = "yellow"', "theme = 1", 'animations = "false"', "animations = 0", "animations = []"]
)
def test_invalid_theme_toml(tmp_path: Path, text: str) -> None:
    path = tmp_path / "theme.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=r"theme|animations"):
        load_config(tmp_path, path)


@pytest.mark.parametrize(
    "value,expected", [("true", True), ("TRUE", True), ("1", True), ("false", False), ("False", False), ("0", False)]
)
def test_theme_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str, expected: bool) -> None:
    monkeypatch.setenv("NGN_THEME", "ocean")
    monkeypatch.setenv("NGN_ANIMATIONS", value)
    config = load_config(tmp_path)
    assert config.theme == "ocean"
    assert config.animations is expected


@pytest.mark.parametrize(
    "variable,value", [("NGN_THEME", "unknown"), ("NGN_ANIMATIONS", "yes"), ("NGN_ANIMATIONS", "")]
)
def test_invalid_theme_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variable: str, value: str) -> None:
    monkeypatch.setenv(variable, value)
    with pytest.raises(ValueError):
        load_config(tmp_path)


def test_theme_config_precedence_and_project_trust(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NGN_THEME", "ember")
    monkeypatch.setenv("NGN_ANIMATIONS", "false")
    user_dir = tmp_path / "user-config" / "ngn"
    user_dir.mkdir(parents=True)
    (user_dir / "config.toml").write_text('theme = "graphite"\nanimations = true\n', encoding="utf-8")
    project_dir = tmp_path / ".ngn"
    project_dir.mkdir()
    (project_dir / "config.toml").write_text('theme = "ocean"\nanimations = false\n', encoding="utf-8")
    with pytest.warns(UserWarning, match="untrusted project"):
        config = load_config(tmp_path)
    assert (config.theme, config.animations) == ("graphite", True)
    config = load_config(tmp_path, trust_project=True)
    assert (config.theme, config.animations) == ("ocean", False)
    explicit = tmp_path / "selected.toml"
    explicit.write_text('theme = "terminal"\nanimations = true\n', encoding="utf-8")
    config = load_config(tmp_path, explicit, trust_project=True)
    assert (config.theme, config.animations) == ("terminal", True)


@pytest.mark.parametrize("args", [[], ["doctor"], ["run", "hello"]])
def test_cli_theme_defaults_are_suppressed(args: list[str]) -> None:
    options = cli._parser().parse_args(args)
    assert not hasattr(options, "theme")
    assert not hasattr(options, "animations")


@pytest.mark.parametrize(
    "args,expected",
    [
        (["--theme", "graphite", "--no-animations", "run", "hello"], "graphite"),
        (["run", "--theme", "ocean", "--no-animations", "hello"], "ocean"),
        (["--theme", "graphite", "--no-animations", "run", "--theme", "ember", "hello"], "ember"),
        (["--no-animations", "doctor", "--theme", "terminal"], "terminal"),
    ],
)
def test_cli_theme_options_before_and_after_subcommands(args: list[str], expected: str) -> None:
    options = cli._parser().parse_args(args)
    assert options.theme == expected
    assert options.animations is False


def test_cli_theme_invalid_choice(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        cli._parser().parse_args(["--theme", "yellow"])
    assert error.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_cli_passes_theme_overrides_to_harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[HarnessConfig] = []

    class PreviewHarness:
        async def initialize(self) -> None:
            pass

        def describe(self) -> str:
            return "Configuration captured; no credentials or network."

        async def close(self) -> None:
            pass

    def create_harness(config: HarnessConfig) -> Harness:
        captured.append(config)
        return cast("Harness", PreviewHarness())

    monkeypatch.setattr(cli, "Harness", create_harness)
    monkeypatch.setenv("NGN_THEME", "ocean")
    monkeypatch.setenv("NGN_ANIMATIONS", "true")
    assert cli.main(["-C", str(tmp_path), "--theme", "graphite", "doctor", "--no-animations", "--auth", "api-key"]) == 0
    assert (captured[0].theme, captured[0].animations, captured[0].auth) == ("graphite", False, "api-key")


def test_cli_and_config_do_not_import_textual() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import nagents.cli; from nagents.harness.config import THEME_NAMES; assert 'textual' not in sys.modules; assert 'rich' not in sys.modules; assert THEME_NAMES[0] == 'terminal'",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("name", THEME_NAMES)
@pytest.mark.parametrize("background", THEME_BACKGROUNDS)
def test_theme_tokens_and_no_global_palette_changes(name: str, background: str) -> None:
    app: App[None] = App()
    dark_palette, light_palette = app.ansi_theme_dark, app.ansi_theme_light
    themes.configure_theme(app, name, background=background)
    native = background == "terminal" or (background == "auto" and name == "terminal")
    assert app.theme == name
    assert app.native_ansi_color is native
    assert app.ansi_color is None
    assert app.ansi_theme_dark is dark_palette
    assert app.ansi_theme_light is light_palette
    variables = app.get_css_variables()
    css = CSS_PATH.read_text(encoding="utf-8") + CSS_PATH.with_name("commands.tcss").read_text(encoding="utf-8")
    references = set(re.findall(r"\$(ngn-[a-z-]+)", css))
    assert references <= variables.keys()
    for key in (
        "background",
        "panel",
        "accent",
        "text",
        "muted",
        "border",
        "error",
        "overlay",
        "hover",
        "selection",
        "success",
        "warning",
        "user",
        "assistant",
        "heading",
        "tool",
        "info",
        "code",
    ):
        Color.parse(variables[f"ngn-{key}"])
    assert not re.search(r"#[0-9a-fA-F]{6}\b", CSS_PATH.read_text(encoding="utf-8"))
    if native:
        assert "yellow" not in " ".join(variables[key] for key in variables if key.startswith("ngn-"))
        for key in ("background", "panel", "overlay", "text", "muted", "hover", "selection"):
            assert variables[f"ngn-{key}"] == "ansi_default"
        assert variables["ngn-selection-style"] == "reverse"
    elif name in {"graphite", "ocean"}:
        accent = Color.parse(variables["ngn-accent"])
        assert accent.b >= accent.r
    themes.configure_theme(app, name, background=background)
    assert app.theme == name


@pytest.mark.parametrize("name", THEME_NAMES)
@pytest.mark.parametrize("background", THEME_BACKGROUNDS)
@pytest.mark.parametrize("size", [(60, 20), (80, 24), (132, 38)])
def test_theme_css_renders_native_defaults_and_modal_states(name: str, background: str, size: tuple[int, int]) -> None:
    async def scenario() -> None:
        app = ThemePreview(name, background)
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            variables = app.get_css_variables()
            assert app.screen.styles.background == Color.parse(variables["ngn-background"])
            assert app.query_one("#composer").region.bottom <= size[1] - 2
            assert app.query_one("#rail").display is (size[0] > 110)
            panel = Color.parse(variables["ngn-panel"])
            assert app.query_one("#rail").styles.background == panel
            assert app.query_one(".user").styles.background == panel
            for selector in ("#rail", ".user", ".assistant"):
                assert app.query_one(selector).styles.border_left[0] == ""
            assert app.query_one("#composer").styles.border_top[1] == Color.parse(variables["ngn-accent"])
            if app.native_ansi_color:
                body_style = app.query_one("#body").visual_style.rich_style
                assert body_style.color is not None and body_style.color.type is ColorType.DEFAULT
                assert body_style.bgcolor is not None and body_style.bgcolor.type is ColorType.DEFAULT
                console = Console(force_terminal=True, color_system="truecolor")
                with console.capture() as output:
                    console.print("native defaults", style=body_style, end="")
                assert "\x1b[39;49m" in output.get()
                assert app.query_one("#composer").styles.color.ansi == -1
                assert app.query_one("#composer").styles.background.ansi == -1
            assert ElementTree.fromstring(app.export_screenshot()).tag.endswith("svg")

            approval = ApprovalModal(
                ApprovalRequest(
                    "preview",
                    "edit_file",
                    "A preview, not an executed action.",
                    {"path": "example.py"},
                    "--- a/example.py\n+++ b/example.py\n-old\n+new",
                )
            )
            await app.push_screen(approval)
            await pilot.pause()
            assert approval.styles.background == Color.parse(variables["ngn-overlay"])
            assert approval.query_one(".dialog").styles.background == Color.parse(variables["ngn-panel"])
            assert approval.focused is approval.query_one("#deny", Button)
            assert approval.query_one("#deny").region.bottom <= size[1]
            await approval.dismiss(False)

            choices = ChoiceModal("COMMANDS", [("new", "New session"), ("help", "Help")])
            await app.push_screen(choices)
            await pilot.pause()
            assert choices.query_one(Input).styles.background == Color.parse(variables["ngn-background"])
            await choices.dismiss()

            login = LoginMethodModal()
            await app.push_screen(login)
            await pilot.pause()
            assert login.query_one(".dialog").styles.background == Color.parse(variables["ngn-panel"])
            await login.dismiss()

            device = DeviceLoginModal(lambda: None)
            await app.push_screen(device)
            await pilot.pause()
            assert device.query_one("#device-status", Static).styles.color == Color.parse(variables["ngn-accent"])
            assert ElementTree.fromstring(app.export_screenshot()).tag.endswith("svg")
            await device.dismiss()

    asyncio.run(scenario())


def test_theme_switching_updates_existing_css() -> None:
    async def scenario() -> None:
        app = ThemePreview("terminal")
        async with app.run_test() as pilot:
            for name in ("graphite", "ocean", "ember", "terminal"):
                for background in ("auto", "theme", "terminal"):
                    themes.configure_theme(app, name, background=background)
                    await pilot.pause()
                    assert app.screen.styles.background == Color.parse(app.get_css_variables()["ngn-background"])
                    assert app.native_ansi_color is (
                        background == "terminal" or (name == "terminal" and background == "auto")
                    )
            assert app.screen.styles.background.ansi == -1

    asyncio.run(scenario())


@pytest.mark.parametrize("name", THEME_NAMES)
def test_painted_semantic_text_has_contrast_on_all_surfaces(name: str) -> None:
    app: App[None] = App()
    themes.configure_theme(app, name, background="theme")
    variables = app.get_css_variables()

    def luminance(value: str) -> float:
        channels = [channel / 255 for channel in Color.parse(value).rgb]
        linear = [channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4 for channel in channels]
        return sum(channel * weight for channel, weight in zip(linear, (0.2126, 0.7152, 0.0722), strict=True))

    for surface in ("background", "panel", "hover", "selection"):
        for token in (
            "text",
            "muted",
            "accent",
            "user",
            "assistant",
            "heading",
            "tool",
            "info",
            "code",
            "error",
            "success",
            "warning",
        ):
            light = luminance(variables[f"ngn-{token}"])
            dark = luminance(variables[f"ngn-{surface}"])
            assert (light + 0.05) / (dark + 0.05) >= 4.5, (name, token, surface)


@pytest.mark.parametrize("name", THEME_NAMES)
@pytest.mark.parametrize("background", ["theme", "terminal"])
def test_semantic_colors_reach_real_widgets_and_syntax(name: str, background: str) -> None:
    async def scenario() -> None:
        app = ThemePreview(name, background)
        async with app.run_test(size=(132, 45)) as pilot:
            await pilot.pause()
            variables = app.get_css_variables()
            for selector, token in (
                (".user .turn-label", "user"),
                (".assistant .turn-label", "assistant"),
                ("MarkdownH2", "heading"),
                ("#model", "info"),
                ("#compose-label", "user"),
                (".tool-card.complete > CollapsibleTitle", "success"),
                (".notice.error", "error"),
            ):
                assert app.query_one(selector).styles.color == Color.parse(variables[f"ngn-{token}"])
            code = app.query_one(CodeFence).query_one(Label).content
            card = app.query_one(ToolCard)
            assert isinstance(code, Content)
            assert isinstance(card.arguments.content, Content)
            assert isinstance(card.diff.content, Content)
            assert any("$ngn-code" in str(span.style) for span in code.spans)
            assert any("$ngn-success" in str(span.style) for span in card.arguments.content.spans)
            diff_styles = {str(span.style) for span in card.diff.content.spans}
            assert {"$ngn-success", "$ngn-error", "$ngn-info bold"} <= diff_styles
            # Export resolves the CSS spans: colors must be painted, not just defined.
            svg = app.export_screenshot()
            if background == "theme":
                for token in ("user", "assistant", "heading", "info", "code", "success", "error", "warning"):
                    assert variables[f"ngn-{token}"] in svg
            else:
                assert all(
                    value.startswith("ansi_")
                    for key, value in variables.items()
                    if key.startswith("ngn-") and not key.endswith("style")
                )

    asyncio.run(scenario())


def test_configuring_in_on_load_precedes_custom_css_parsing() -> None:
    class LoadThemeApp(App[None]):
        def on_load(self) -> None:
            themes.configure_theme(self, "terminal")

        def compose(self) -> ComposeResult:
            yield Static("Native defaults", id="body", markup=False)

    async def scenario() -> None:
        app = LoadThemeApp(css_path=CSS_PATH)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.screen.styles.background.ansi == -1
            assert app.screen.styles.color.ansi == -1

    asyncio.run(scenario())


@pytest.mark.parametrize("name", THEME_NAMES)
def test_activity_frames_are_fixed_width_and_time_driven(name: str) -> None:
    frames = [themes.activity_frame(name, tick / themes.ACTIVITY_FPS) for tick in range(20)]
    assert len(set(frames)) > 1
    assert all(frame.isascii() and len(frame) == 3 and not any(ord(char) < 32 for char in frame) for frame in frames)
    assert themes.activity_frame(name, 0) != themes.activity_frame(name, 1 / themes.ACTIVITY_FPS)
    assert themes.activity_frame(name, 0) == themes.activity_frame(name, 0.01)
    for elapsed in (-1.0, float("inf"), float("nan"), 1e308):
        assert len(themes.activity_frame(name, elapsed)) == 3
    assert themes.activity_frame(name, -1) == themes.activity_frame(name, 0)


def test_unknown_theme_does_not_mutate_app() -> None:
    app: App[None] = App()
    before = app.theme
    with pytest.raises(ValueError, match="Unknown theme"):
        themes.configure_theme(app, "unknown")
    assert app.theme == before
    with pytest.raises(ValueError, match="Unknown theme background"):
        themes.configure_theme(app, "terminal", background="unknown")
    assert app.theme == before
    with pytest.raises(ValueError, match="Unknown theme"):
        themes.activity_frame("unknown", 0)
