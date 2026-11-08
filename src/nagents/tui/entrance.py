"""Normal-screen entrance, before Textual takes terminal ownership.

Scroll a small box into view, then redraw only its allocated interior rows.
Initialization and run_async share the caller's loop. POSIX cbreak input is scoped strictly to
this prelude; other platforms skip it rather than emulate a second TUI driver.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from codecs import getincrementaldecoder
from time import monotonic
from typing import TYPE_CHECKING

from nagents._version import framework_version

if TYPE_CHECKING:
    from collections.abc import Callable

    from .app import NagentsApp


async def _draw(read_key: Callable[[], str]) -> bool:
    size = shutil.get_terminal_size()
    # Leave a spare column to avoid autowrap, and never rewind beyond the
    # viewport on a very short terminal.
    width = min(70, size.columns - 3)
    if width < 16 or size.lines < 7:
        return True
    write = sys.stdout.write
    lines = [f"ngn {framework_version()}", "Opening workspace...", "Enter / Space: skip   Esc / Ctrl+C / Ctrl+Q: exit"]
    border = "+" + "-" * width + "+\n"
    blank = "|" + " " * width + "|\n"
    saved = False
    start = monotonic()

    async def wait_until(seconds: float) -> str:
        while monotonic() - start < seconds:
            key = read_key()
            if key:
                return key
            await asyncio.sleep(0.01)
        return read_key()

    try:
        write("\x1b[?25l\n" + border)
        sys.stdout.flush()
        # First scroll/expand in normal terminal flow. No earlier shell row is
        # addressed, including when the cursor started at the bottom margin.
        for index in range(3):
            write(blank)
            sys.stdout.flush()
            key = await wait_until((index + 1) * 0.035)
            if key:
                write(border)
                return key not in {"\x1b", "\x03", "\x11"}
        write(border + "\x1b7")
        saved = True
        sys.stdout.flush()
        # The saved cursor is below our completed box. Rewind only through its
        # three interior rows and bottom border, never into prior output.
        for index in range(3):
            key = await wait_until(0.15 + index * 0.06)
            if key:
                return key not in {"\x1b", "\x03", "\x11"}
            write("\x1b8\x1b[4A\r")
            for row, text in enumerate(lines):
                revealed = text if row <= index else ""
                write("|" + revealed[:width].ljust(width) + "|\n")
            write("\x1b8")
            sys.stdout.flush()
        key = await wait_until(0.32)
        return key not in {"\x1b", "\x03", "\x11"}
    finally:
        if saved:
            write("\x1b8")
        write("\x1b[?25h")
        sys.stdout.flush()


async def run_tui(app: NagentsApp) -> None:
    """Run prelude and Textual on one loop, with non-overlapping terminal owners."""
    if os.name != "posix" or not app.harness.config.animations or not sys.stdin.isatty() or not sys.stdout.isatty():
        await app.run_async()
        return

    import termios
    import tty

    fd = sys.stdin.fileno()
    original = termios.tcgetattr(fd)
    keys: list[str] = []
    loop = asyncio.get_running_loop()

    decode = getincrementaldecoder("utf-8")(errors="replace").decode
    typed = ""

    def read_input() -> None:
        data = os.read(fd, 1)
        text = decode(data) if data else "\x03"
        if text:
            keys.append(text)

    def read_key() -> str:
        nonlocal typed
        key = keys.pop(0) if keys else ""
        if key.isprintable() and key != " ":
            typed += key
        return key

    initialize = asyncio.create_task(
        app.harness.initialize(create_session=not (app._resume_session or app._continue_session or app._pick_session))
    )
    handed_over = False
    try:
        try:
            tty.setcbreak(fd)
            # Keep output newline translation, but receive cancellation keys
            # rather than SIGINT / software flow control.
            attributes = termios.tcgetattr(fd)
            attributes[0] &= ~termios.IXON
            attributes[3] &= ~termios.ISIG
            termios.tcsetattr(fd, termios.TCSANOW, attributes)
            loop.add_reader(fd, read_input)
            opened = await _draw(read_key)
        finally:
            loop.remove_reader(fd)
            termios.tcsetattr(fd, termios.TCSANOW, original)
        if not opened:
            app.startup_opened = False
            return
        # Textual may mount while initialization is still running, but must await
        # this task rather than start another initialization lifecycle.
        app._preinitialized = initialize
        app._complete_inline_entrance()
        app._entrance_text = typed + "".join(key for key in keys if key.isprintable())
        handed_over = True
        await app.run_async()
    finally:
        if not initialize.done():
            initialize.cancel()
        # Join initialization before closing resources, including cancellation
        # before Textual has mounted (and therefore cannot own shutdown yet).
        await asyncio.gather(initialize, return_exceptions=True)
        if not handed_over:
            await app.harness.close()
