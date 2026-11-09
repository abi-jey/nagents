"""Continuous, foreground-only activity fields for the terminal client."""

from __future__ import annotations

from dataclasses import dataclass
from math import cos
from math import exp
from math import pi
from math import sin
from typing import Literal

from rich.color import Color
from rich.style import Style
from rich.text import Text

RGB = tuple[int, int, int]
ACTIVITY_FPS = 30


def duration(seconds: float) -> str:
    value = max(0, int(seconds))
    hours, remainder = divmod(value, 3600)
    minutes, seconds_value = divmod(remainder, 60)
    return f"{hours}:{minutes:02}:{seconds_value:02}" if hours else f"{minutes:02}:{seconds_value:02}"


StateName = Literal[
    "starting",
    "idle",
    "working",
    "tool",
    "delegating",
    "delegated",
    "returned",
    "approval",
    "sleeping",
    "waking",
    "responding",
    "done",
    "error",
]
Stops = tuple[RGB, RGB, RGB]
_BITS = ((1, 8), (2, 16), (4, 32), (64, 128))


@dataclass(frozen=True)
class StateStyle:
    label: str
    detail: str
    stops: Stops
    speed: float = 1.0
    reach: float = 0.9
    flow: float = 0.0
    wave: float = 0.2


STATES: dict[StateName, StateStyle] = {
    "starting": StateStyle(
        "Getting ready", "Opening the workspace", ((120, 156, 196), (156, 184, 222), (130, 199, 198)), 0.5, 0.28
    ),
    "idle": StateStyle(
        "Ready", "Waiting for your message", ((126, 151, 171), (158, 179, 196), (132, 173, 177)), 0.16, 0.45, 0, 0.08
    ),
    "working": StateStyle(
        "Working", "Processing the request", ((104, 175, 222), (160, 146, 228), (119, 204, 193)), 1.0, 0.9
    ),
    "tool": StateStyle(
        "Using a tool",
        "Executing a tool",
        ((211, 155, 101), (229, 189, 119), (216, 149, 154)),
        1.5,
        0.85,
        0.65,
        0.3,
    ),
    "delegating": StateStyle(
        "Delegating",
        "Starting delegated work",
        ((144, 144, 220), (193, 144, 214), (127, 186, 225)),
        1.35,
        1.0,
        0.95,
        0.35,
    ),
    "delegated": StateStyle(
        "Delegated work",
        "Waiting for delegated work",
        ((136, 146, 215), (166, 151, 216), (116, 186, 198)),
        0.65,
        0.82,
        0.35,
        0.13,
    ),
    "returned": StateStyle(
        "Findings received",
        "Delegation result available",
        ((210, 189, 118), (134, 204, 166), (112, 192, 202)),
        1.1,
        0.7,
        -0.95,
        0.3,
    ),
    "approval": StateStyle(
        "Waiting for approval",
        "An operation needs approval",
        ((186, 155, 110), (221, 184, 112), (200, 158, 128)),
        0.17,
        0.48,
        0,
        0.08,
    ),
    "sleeping": StateStyle(
        "Waiting for wake-up",
        "Scheduled continuation pending",
        ((112, 143, 196), (140, 162, 204), (122, 170, 194)),
        0.1,
        0.6,
        0,
        0.06,
    ),
    "waking": StateStyle(
        "Waking up",
        "Wake-up received · resuming the next turn",
        ((127, 165, 221), (123, 206, 197), (167, 215, 161)),
        1.45,
        0.96,
        -0.35,
        0.4,
    ),
    "responding": StateStyle(
        "Responding",
        "Writing up the findings",
        ((105, 185, 166), (132, 212, 181), (142, 197, 216)),
        0.85,
        0.8,
        0.2,
        0.3,
    ),
    "done": StateStyle(
        "Complete",
        "Work complete",
        ((115, 172, 145), (148, 204, 163), (132, 191, 180)),
        0.09,
        0.28,
        0,
        0.03,
    ),
    "error": StateStyle(
        "Needs attention",
        "Check the reported error",
        ((194, 126, 150), (224, 146, 144), (193, 141, 182)),
        0.3,
        0.55,
        0,
        0.12,
    ),
}


@dataclass
class Spring:
    """An exact damped spring retains its velocity when its destination changes."""

    value: float
    target: float
    velocity: float = 0.0
    frequency: float = 6.5

    def integral(self, seconds: float) -> float:
        decay = exp(-self.frequency * seconds)
        distance = self.value - self.target
        correction = self.velocity + self.frequency * distance
        return (
            self.target * seconds
            + distance * (1 - decay) / self.frequency
            + correction * ((1 - decay) / self.frequency**2 - seconds * decay / self.frequency)
        )

    def advance(self, seconds: float) -> None:
        distance = self.value - self.target
        correction = self.velocity + self.frequency * distance
        decay = exp(-self.frequency * seconds)
        self.value = self.target + (distance + correction * seconds) * decay
        self.velocity = (self.velocity - self.frequency * correction * seconds) * decay


def channels(style: StateStyle) -> tuple[float, ...]:
    return (
        *(float(channel) for color in style.stops for channel in color),
        style.speed,
        style.reach,
        style.flow,
        style.wave,
    )


class StateField:
    """Keep particle phase and momentum through arbitrary, interruptible state transitions."""

    def __init__(self, state: StateName = "starting", *, offset: float = 0.0) -> None:
        self.state = state
        self.phase = offset
        self.springs = tuple(Spring(value, value) for value in channels(STATES[state]))

    def select(self, state: StateName, *, immediate: bool = False) -> None:
        self.state = state
        for spring, target in zip(self.springs, channels(STATES[state]), strict=True):
            spring.target = target
            if immediate:
                spring.value, spring.velocity = target, 0.0

    def advance(self, seconds: float) -> None:
        seconds = max(0.0, seconds)
        self.phase += self.springs[9].integral(seconds)
        for spring in self.springs:
            spring.advance(seconds)

    @property
    def stops(self) -> Stops:
        values = [max(0, min(255, round(spring.value))) for spring in self.springs[:9]]
        return (values[0], values[1], values[2]), (values[3], values[4], values[5]), (values[6], values[7], values[8])

    def points(self, width: int = 7, height: int = 1, *, aperture: float = 1.0) -> tuple[tuple[int, int], ...]:
        aperture = max(0.0, min(1.0, aperture))
        reach, flow, wave = (spring.value for spring in self.springs[10:])
        positions = []
        for particle in range(12):
            angle = self.phase * (0.7 + particle * 0.017) + particle * 2.39996
            orbit = sin(angle)
            # Hand-off / return add opposite travelling waves without restarting or replacing the orbit.
            travelling = sin(angle * 0.73 + particle * 0.35) * flow * 0.2
            x = 0.5 + aperture * (reach * 0.44 * orbit + travelling)
            y = 0.5 + aperture * (reach * 0.42 * cos(angle * 1.37 + particle) + wave * 0.1 * sin(angle * 2))
            positions.append(
                (
                    round(max(0, min(1, x)) * (width * 2 - 1)),
                    round(max(0, min(1, y)) * (height * 4 - 1)),
                )
            )
        return tuple(positions)

    def render(self, *, width: int = 7, height: int = 1, gradient: bool = True, aperture: float = 1.0) -> Text:
        cells = [[0 for _ in range(width)] for _ in range(height)]
        for x, y in self.points(width, height, aperture=aperture):
            cells[y // 4][x // 2] |= _BITS[y % 4][x % 2]
        stops = self.stops
        result = Text()
        for row, line in enumerate(cells):
            if row:
                result.append("\n")
            for column, bits in enumerate(line):
                amount = 0.5 + 0.5 * sin(column / max(1, width - 1) * pi + self.phase * 0.35) if gradient else 0.5
                segment = min(1, int(amount * 2))
                start, end = stops[segment : segment + 2]
                mix = amount * 2 - segment
                color = (
                    round(start[0] + (end[0] - start[0]) * mix),
                    round(start[1] + (end[1] - start[1]) * mix),
                    round(start[2] + (end[2] - start[2]) * mix),
                )
                # Foreground only: no guessed terminal background, alpha blend, or painted rectangle.
                result.append(chr(0x2800 + bits), Style(color=Color.from_rgb(*color)))
        return result
