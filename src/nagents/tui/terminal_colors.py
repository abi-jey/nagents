"""Frame read-only terminal color replies before Textual interprets keyboard input.

Unknown terminals keep their native defaults. We never set terminal colors or
query personal terminal configuration. Bracketed paste remains literal input.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from textual.message import Message

QUERY_COLORS = "\x1b]10;?\x1b\\\x1b]11;?\x1b\\"
_PASTE_START = "\x1b[200~"
_PASTE_END = "\x1b[201~"
_PREFIXES = ("\x1b]10;", "\x1b]11;", _PASTE_START)
_REPLY = re.compile(r"\x1b\](10|11);rgb:([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})(?:\x07|\x1b\\)")


@dataclass(frozen=True)
class TerminalColors:
    foreground: str = ""
    background: str = ""


class TerminalColorReported(Message):
    def __init__(self, colors: TerminalColors) -> None:
        super().__init__()
        self.colors = colors


class ColorReplies:
    """Bounded, incremental OSC framing; all unrelated input passes through."""

    def __init__(self) -> None:
        self._pending = ""
        self._since = 0.0
        self._pasting = False
        self.colors = TerminalColors()

    def feed(self, text: str, now: float) -> str:
        data = self._pending + text
        self._pending = ""
        output: list[str] = []
        while data:
            if self._pasting:
                end = data.find(_PASTE_END)
                if end >= 0:
                    output.append(data[: end + len(_PASTE_END)])
                    data = data[end + len(_PASTE_END) :]
                    self._pasting = False
                    continue
                # Hold only a possible split paste terminator, never pasted text.
                keep = next((size for size in range(len(_PASTE_END) - 1, 0, -1) if data.endswith(_PASTE_END[:size])), 0)
                output.append(data[:-keep] if keep else data)
                data = data[-keep:] if keep else ""
                break
            escape = data.find("\x1b")
            if escape < 0:
                output.append(data)
                data = ""
                break
            output.append(data[:escape])
            data = data[escape:]
            if data.startswith(_PASTE_START):
                output.append(_PASTE_START)
                data = data[len(_PASTE_START) :]
                self._pasting = True
                continue
            if data.startswith(_PREFIXES[:2]):
                terminator = re.search(r"\x07|\x1b\\", data)
                if terminator is None and len(data) < 96:
                    break
                if terminator is not None:
                    reply = data[: terminator.end()]
                    match = _REPLY.fullmatch(reply)
                    if match:
                        rgb = tuple(
                            round(int(channel, 16) * 255 / (16 ** len(channel) - 1)) for channel in match.groups()[1:]
                        )
                        color = "#" + "".join(f"{channel:02x}" for channel in rgb)
                        self.colors = TerminalColors(
                            color if match[1] == "10" else self.colors.foreground,
                            color if match[1] == "11" else self.colors.background,
                        )
                        data = data[terminator.end() :]
                        continue
            elif any(prefix.startswith(data) for prefix in _PREFIXES):
                break
            output.append(data[0])
            data = data[1:]
        if data:
            self._pending = data
            self._since = now
        return "".join(output)

    def expire(self, now: float) -> str:
        """Release incomplete input promptly (including a standalone Escape)."""
        if self._pending and now - self._since >= 0.05:
            pending, self._pending = self._pending, ""
            return pending
        return ""
