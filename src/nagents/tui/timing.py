"""Use owned asyncio timer waits rather than native worker-thread waits on Windows."""

import asyncio
import sys
from importlib import import_module


def configure_timer_sleep() -> None:
    """Avoid Textual's native wait-handle cancellation race on Windows.

    Textual 8.2.8 closes timer/event handles in a coroutine's finally block,
    although its executor thread may still be inside WaitForMultipleObjects.
    Standard asyncio sleep has no detached thread or native handles to close.
    It trades sub-millisecond precision for safe cancellation and loop shutdown.
    POSIX timing remains unchanged.
    """
    if sys.platform == "win32":
        timer = import_module("textual.timer")
        timer.sleep = asyncio.sleep
