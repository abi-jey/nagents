"""Time-interpolated terminal effects; no prerecorded spinner-frame sequence."""

from __future__ import annotations

from math import cos
from math import exp
from math import pi
from math import sin
from typing import Literal

from rich.color import Color
from rich.style import Style
from rich.text import Text

Effect = Literal["shimmer", "field", "breathe"]
EFFECTS: tuple[Effect, ...] = ("shimmer", "field", "breathe")
RGB = tuple[int, int, int]
_DOTS = ((1, 8), (2, 16), (4, 32), (64, 128))


def shade(amount: float, ink: RGB, background: RGB, *, native: bool) -> Style:
    amount = max(0.35, min(1.0, amount))
    if native:
        return Style(bold=amount > 0.84, dim=amount < 0.55)
    rgb = tuple(round(a + (b - a) * amount) for a, b in zip(background, ink, strict=True))
    return Style(color=Color.from_rgb(*rgb))


def particle_field(elapsed: float, *, width: int = 7, height: int = 1) -> str:
    """Rasterize continuous particle positions into 2-by-4-dot braille cells."""
    cells = [[0 for _ in range(width)] for _ in range(height)]
    pixels_x, pixels_y = width * 2, height * 4
    for particle in range(12):
        phase = elapsed * (0.7 + particle * 0.017) + particle * 2.39996
        x = round((0.5 + 0.44 * sin(phase)) * (pixels_x - 1))
        y = round((0.5 + 0.42 * cos(phase * 1.37 + particle)) * (pixels_y - 1))
        cells[y // 4][x // 2] |= _DOTS[y % 4][x % 2]
    return "\n".join("".join(chr(0x2800 + value) for value in row) for row in cells)


def effect_text(
    text: str,
    elapsed: float,
    effect: Effect,
    *,
    active: bool = True,
    motion: bool = True,
    ink: RGB = (112, 220, 202),
    background: RGB = (21, 24, 32),
    native: bool = True,
) -> Text:
    result = Text()
    time = elapsed if motion else 0.0
    if not active:
        return Text(("   ·    " if effect == "field" else "") + text)
    if effect == "field":
        result.append(particle_field(time) + " ", style=shade(0.85, ink, background, native=native))
        result.append(text)
    elif effect == "shimmer":
        position = (time * 10) % (len(text) + 14) - 7
        for index, character in enumerate(text):
            light = 0.6 + 0.4 * exp(-(((index - position) / 2.5) ** 2))
            result.append(character, shade(light, ink, background, native=native))
    else:
        light = 0.68 + 0.25 * (0.5 + 0.5 * sin(time * 2 * pi / 1.8))
        result.append(text, shade(light, ink, background, native=native))
    return result
