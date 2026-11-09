"""A foreground-only sweep over rendered terminal cells; widget geometry stays fixed."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.color import Color as RichColor
from rich.segment import Segment
from rich.style import Style
from textual.filter import LineFilter

if TYPE_CHECKING:
    from textual.color import Color

    from .activity import RGB

MOTION_STYLE = Style(meta={"ngn_motion": True})


class Sweep(LineFilter):
    """Reveal actual rendered content, including Markdown, without replacing widgets."""

    def __init__(
        self,
        amount: float = 1.0,
        *,
        accent: RGB = (125, 200, 193),
        enabled: bool = False,
        frame: bool = False,
    ) -> None:
        super().__init__(enabled=enabled)
        # Textual caches filtered strips by filter identity. Each frame is a
        # distinct immutable snapshot, never a mutation of a cached filter.
        self._amount = amount
        self._frame = frame
        self._accent = RichColor.from_rgb(*accent)

    @property
    def amount(self) -> float:
        return self._amount

    @property
    def accent(self) -> RichColor:
        return self._accent

    def apply(self, segments: list[Segment], background: Color) -> list[Segment]:
        if self.amount >= 1:
            return segments
        total = sum(segment.cell_length for segment in segments)
        cutoff = round(total * max(0, self.amount))
        result: list[Segment] = []
        column = 0
        for segment in segments:
            if segment.control:
                result.append(segment)
                continue
            count = segment.cell_length
            border = (
                self._frame
                and bool(segment.text.strip())
                and all(char.isspace() or char in "╭╮╰╯─│" for char in segment.text)
            )
            if border or (segment.style and segment.style.meta.get("ngn_motion") is True):
                result.append(segment)
            else:
                shown, hidden = segment.split_cells(max(0, min(count, cutoff - column)))
                if shown.text:
                    if column + shown.cell_length == cutoff and cutoff < total:
                        prefix, tip = shown.split_cells(max(0, shown.cell_length - 2))
                        result.append(prefix)
                        result.append(Segment(tip.text, (tip.style or Style()) + Style(color=self.accent)))
                    else:
                        result.append(shown)
                if hidden.text:
                    surface = (
                        segment.style.bgcolor if segment.style and segment.style.bgcolor else background.rich_color
                    )
                    result.append(Segment(" " * hidden.cell_length, Style(bgcolor=surface)))
            column += count
        return result
