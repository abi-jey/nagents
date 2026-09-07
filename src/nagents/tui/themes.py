"""ngn semantic palettes and small, time-driven activity frames.

Configure before CSS is parsed (App.__init__ or on_load), not just on_mount.
Native backgrounds use ANSI defaults, including in modals and syntax highlighting;
we never detect RGB colors, rewrite the terminal palette, or assume white text.
SVG exports use Rich's export palette and cannot show the user's actual defaults.
"""

from __future__ import annotations

from math import isfinite
from typing import TYPE_CHECKING

from pygments.token import Token
from textual.highlight import HighlightTheme
from textual.theme import Theme

from nagents.harness.config import THEME_BACKGROUNDS
from nagents.harness.config import THEME_NAMES as THEME_NAMES

if TYPE_CHECKING:
    from textual.app import App

ACTIVITY_FPS = 8


class CodeTheme(HighlightTheme):
    """CSS-backed syntax spans follow palette/background switches without RGB assumptions."""

    STYLES: dict[tuple[str, ...], str] = {  # noqa: RUF012 - Match Textual's shared mapping annotation.
        Token.Comment: "$ngn-muted italic",
        Token.Error: "$ngn-error underline",
        Token.Keyword: "$ngn-code bold",
        Token.Operator.Word: "$ngn-code bold",
        Token.Name.Builtin: "$ngn-info",
        Token.Name.Function: "$ngn-heading",
        Token.Name.Class: "$ngn-heading bold",
        Token.Name.Tag: "$ngn-heading",
        Token.Name.Attribute: "$ngn-tool",
        Token.Name.Decorator: "$ngn-tool",
        Token.Literal.String: "$ngn-success",
        Token.Literal.Number: "$ngn-warning",
        Token.Generic.Heading: "$ngn-info bold",
        Token.Generic.Subheading: "$ngn-info bold",
        Token.Generic.Inserted: "$ngn-success",
        Token.Generic.Deleted: "$ngn-error",
        Token.Generic.Error: "$ngn-error",
        Token.Generic.Strong: "bold",
        Token.Generic.Emph: "italic",
    }


# All color keys are public CSS tokens, prefixed with "ngn-".
_PALETTES: dict[str, dict[str, str]] = {
    "terminal": {
        "background": "ansi_default",
        "panel": "ansi_default",
        "text": "ansi_default",
        "muted": "ansi_default",
        "border": "ansi_default",
        "accent": "ansi_blue",
        "user": "ansi_magenta",
        "assistant": "ansi_cyan",
        "heading": "ansi_blue",
        "tool": "ansi_magenta",
        "info": "ansi_cyan",
        "code": "ansi_green",
        "error": "ansi_red",
        "warning": "ansi_magenta",
        "success": "ansi_green",
        "hover": "ansi_default",
        "selection": "ansi_default",
        "overlay": "ansi_default",
    },
    "graphite": {
        "background": "#151820",
        "panel": "#1e2330",
        "text": "#e6eaf2",
        "muted": "#a6b0c3",
        "border": "#394357",
        "accent": "#b6a0ff",
        "user": "#85baff",
        "assistant": "#70dcca",
        "heading": "#d5bbff",
        "tool": "#edb577",
        "info": "#90cde8",
        "code": "#efabcf",
        "error": "#ff9aab",
        "warning": "#efd18c",
        "success": "#99d59b",
        "hover": "#292f40",
        "selection": "#344158",
        "overlay": "#0d1014cc",
    },
    "ocean": {
        "background": "#0c1d27",
        "panel": "#122c39",
        "text": "#e0f3f5",
        "muted": "#9bbbc7",
        "border": "#2e4b58",
        "accent": "#61d8e7",
        "user": "#acbbff",
        "assistant": "#68e0c2",
        "heading": "#8ecfff",
        "tool": "#d7a6f4",
        "info": "#79cfe8",
        "code": "#f3c58b",
        "error": "#ffa6a0",
        "warning": "#efcd8a",
        "success": "#96d9aa",
        "hover": "#1c3b4b",
        "selection": "#284b5c",
        "overlay": "#091119cc",
    },
    "ember": {
        "background": "#241917",
        "panel": "#30221e",
        "text": "#f5e8dc",
        "muted": "#c5afa1",
        "border": "#574238",
        "accent": "#f1ad75",
        "user": "#d9b2ed",
        "assistant": "#f1bf7f",
        "heading": "#f5a7a0",
        "tool": "#b5cd88",
        "info": "#9bcbd2",
        "code": "#efc990",
        "error": "#ff9ba7",
        "warning": "#edc786",
        "success": "#a7d4a0",
        "hover": "#423029",
        "selection": "#584035",
        "overlay": "#100f0dcc",
    },
}

# RGB pastels require our painted surfaces. With an unknown native background,
# use the user's ANSI colors instead, retaining each palette's role families.
_NATIVE_ACCENTS = {
    "terminal": ("blue", "magenta", "cyan", "blue", "magenta", "cyan", "green"),
    "graphite": ("magenta", "blue", "cyan", "magenta", "green", "cyan", "magenta"),
    "ocean": ("cyan", "blue", "green", "blue", "magenta", "cyan", "magenta"),
    "ember": ("red", "magenta", "green", "red", "green", "cyan", "blue"),
}

_FRAMES: dict[str, tuple[str, ...]] = {
    "terminal": (" | ", " / ", " - ", " \\ "),
    "graphite": (".  ", ".. ", "...", " ..", "  .", " . "),
    "ocean": ("~  ", " ~ ", "  ~", " ~ "),
    "ember": (" . ", " o ", " O ", " o "),
}


def configure_theme(app: App[None], name: str, *, background: str = "auto") -> None:
    """Register/select a preset, exporting ngn colors and focus/selection styles.

    Only the supplied app's theme registry and selection are changed. Runtime
    switching is supported; initial setup must precede parsing ngn's CSS tokens.
    auto: native for terminal, painted otherwise. terminal: native for all names.
    theme: painted for all names; terminal uses graphite's dark surface fallback.
    """
    if name not in THEME_NAMES:
        raise ValueError(f"Unknown theme; choose from: {', '.join(THEME_NAMES)}")
    if background not in THEME_BACKGROUNDS:
        raise ValueError(f"Unknown theme background; choose from: {', '.join(THEME_BACKGROUNDS)}")
    native = background == "terminal" or (background == "auto" and name == "terminal")
    palette = _PALETTES["terminal" if native else "graphite" if name == "terminal" else name].copy()
    if native:
        palette.update(
            (key, f"ansi_{color}")
            for key, color in zip(
                ("accent", "user", "assistant", "heading", "tool", "info", "code"), _NATIVE_ACCENTS[name], strict=True
            )
        )
    elif name == "terminal":
        palette.update(accent="#85baff", user="#efabcf", heading="#85baff", tool="#d5bbff", code="#99d59b")
    focus_style = "bold reverse" if native else "bold"
    variables = {f"ngn-{key}": value for key, value in palette.items()}
    variables.update(
        {
            "ngn-focus-style": focus_style,
            "ngn-selection-style": "reverse" if native else "none",
            "ansi-background": palette["background"],
            "ansi-foreground": palette["text"],
            "text": palette["text"],
            "text-muted": palette["muted"],
            "text-disabled": palette["muted"],
            "text-primary": palette["heading"],
            "text-secondary": palette["info"],
            "text-accent": palette["code"],
            "text-success": palette["success"],
            "text-warning": palette["warning"],
            "text-error": palette["error"],
            "foreground-muted": palette["muted"],
            "surface": palette["panel"],
            "panel": palette["panel"],
            "border": palette["accent"],
            "border-blurred": palette["border"],
            "block-cursor-foreground": palette["text"],
            "block-cursor-background": palette["selection"],
            "block-cursor-text-style": focus_style,
            "block-cursor-blurred-foreground": palette["text"],
            "block-cursor-blurred-background": palette["panel"],
            "block-hover-background": palette["hover"],
            "input-cursor-foreground": palette["text"] if native else palette["background"],
            "input-cursor-background": palette["background"] if native else palette["accent"],
            "input-cursor-text-style": "reverse" if native else "none",
            "input-selection-background": palette["selection"],
            "input-selection-foreground": palette["text"],
            "button-focus-text-style": focus_style,
            "scrollbar": palette["border"],
            "scrollbar-hover": palette["accent"],
            "scrollbar-active": palette["accent"],
            "scrollbar-background": palette["background"],
            "scrollbar-background-hover": palette["background"],
            "scrollbar-background-active": palette["background"],
            "scrollbar-corner-color": palette["background"],
            "link-color": palette["accent"],
            "link-color-hover": palette["text"],
            "link-background-hover": palette["hover"],
            "screen-selection-background": palette["selection"],
            "screen-selection-foreground": palette["text"],
        }
    )
    app.register_theme(
        Theme(
            name=name,
            primary=palette["accent"],
            secondary=palette["info"],
            accent=palette["code"],
            warning=palette["warning"],
            error=palette["error"],
            success=palette["success"],
            background=palette["background"],
            foreground=palette["text"],
            surface=palette["panel"],
            panel=palette["panel"],
            ansi=native,
            variables=variables,
        )
    )
    app.theme = name
    # Registering a different background for the active name doesn't change the
    # theme reactive, so explicitly invalidate CSS in that case too.
    app.refresh_css()


def activity_frame(name: str, elapsed: float) -> str:
    """Return a three-column ASCII frame; update at 8 FPS only while busy/enabled."""
    if name not in THEME_NAMES:
        raise ValueError(f"Unknown theme; choose from: {', '.join(THEME_NAMES)}")
    frames = _FRAMES[name]
    elapsed = max(0.0, elapsed) if isfinite(elapsed) else 0.0
    period = len(frames) / ACTIVITY_FPS
    return frames[int((elapsed % period) * ACTIVITY_FPS)]
