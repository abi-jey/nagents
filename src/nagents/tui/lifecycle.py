"""Foreground-only entrance and exit studies; application geometry stays fixed."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Literal

from rich.color import Color
from rich.style import Style
from rich.text import Text

if TYPE_CHECKING:
    from .activity import RGB

Transition = Literal["sweep", "stagger", "quiet"]
TRANSITIONS: tuple[Transition, ...] = ("sweep", "stagger", "quiet")
Phase = Literal["intro", "opening", "ready", "closing", "closed"]


def ease(value: float) -> float:
    value = max(0.0, min(1.0, value))
    return value * value * (3 - 2 * value)


@dataclass
class Lifecycle:
    transition: Transition = "sweep"
    phase: Phase = "opening"
    since: float = 0.0
    replay: bool = False
    origin_visibility: float = 0.0
    origin_aperture: float = 0.0
    intro_enabled: bool = False

    @property
    def seconds(self) -> float:
        if self.phase == "intro":
            return 1.2 if self.transition == "quiet" else 1.8
        if self.transition == "quiet":
            return 0.4 if self.phase == "opening" else 0.25
        return 1.0 if self.phase == "opening" else 0.65

    def progress(self, now: float) -> float:
        if self.phase in {"ready", "closed"}:
            return 1.0
        return max(0.0, min(1.0, (now - self.since) / self.seconds))

    def aperture(self, now: float) -> float:
        """Open the same moving particles first, then gather them last on exit."""
        progress = self.progress(now)
        if self.phase == "closed":
            return 0.0
        if self.phase == "intro":
            return self.origin_aperture + (1 - self.origin_aperture) * ease(progress * 2)
        if self.phase == "opening":
            return self.origin_aperture + (1 - self.origin_aperture) * ease(progress * 1.6)
        if self.phase == "closing":
            return self.origin_aperture * (1 - ease((progress - 0.25) / 0.75))
        return 1.0

    def visibility(self, now: float) -> float:
        if self.phase in {"intro", "closed"}:
            return 0.0
        if self.phase == "ready":
            return 1.0
        destination = 1.0 if self.phase == "opening" else 0.0
        return self.origin_visibility + (destination - self.origin_visibility) * ease(self.progress(now))

    def begin(self, phase: Phase, now: float, *, replay: bool = False) -> None:
        self.origin_visibility, self.origin_aperture = self.visibility(now), self.aperture(now)
        self.phase, self.since, self.replay = phase, now, replay

    def amount(self, now: float, order: float) -> float:
        if self.phase == "ready":
            return 1.0
        visibility = self.visibility(now)
        if self.transition == "quiet":
            return float(visibility >= 0.24)
        if self.transition == "stagger":
            return float(visibility >= 0.16 + order * 0.64)
        return ease((visibility - order * 0.24 - 0.12) / 0.64)

    def text(self, content: Text, now: float, *, order: float, accent: RGB) -> Text:
        if self.phase == "ready":
            return content
        result = Text()
        lines = content.split("\n", allow_blank=True)
        for index, line in enumerate(lines):
            if index:
                result.append("\n")
            amount = self.amount(now, min(1.0, order + index * 0.035))
            visible = round(len(line) * amount)
            part = line[:visible]
            if self.transition == "sweep" and 0 < visible < len(line):
                part.stylize(Style(color=Color.from_rgb(*accent)), max(0, visible - 2), visible)
            result.append_text(part)
        return result

    def advance(self, now: float) -> bool:
        """Return true only when the app should really exit, not when replaying."""
        if self.phase in {"ready", "closed"} or self.progress(now) < 1:
            return False
        if self.phase == "intro":
            self.begin("opening", now)
        elif self.phase == "opening":
            self.phase = "ready"
        elif self.replay:
            self.begin("intro" if self.intro_enabled else "opening", now)
        else:
            self.phase = "closed"
            return True
        return False
