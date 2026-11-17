"""Invocation-local active time, excluding approval queues and decisions."""

import asyncio
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_current: ContextVar["ToolClock | None"] = ContextVar("tool_clock", default=None)


class ToolClock:
    def __init__(self, owner: object, call_id: str) -> None:
        self.owner = owner
        self.call_id = call_id
        self.task = asyncio.current_task()
        self.started = time.monotonic()
        self.waited = 0.0
        self.depth = 0
        self.paused_at = 0.0

    def elapsed(self) -> float:
        end = self.paused_at if self.depth else time.monotonic()
        return max(0.0, end - self.started - self.waited)

    @contextmanager
    def pause(self) -> Iterator[None]:
        if not self.depth:
            self.paused_at = time.monotonic()
        self.depth += 1
        try:
            yield
        finally:
            self.depth -= 1
            if not self.depth:
                self.waited += time.monotonic() - self.paused_at


@contextmanager
def invocation(owner: object, call_id: str) -> Iterator[ToolClock]:
    clock = ToolClock(owner, call_id)
    token = _current.set(clock)
    try:
        yield clock
    finally:
        _current.reset(token)


def current_clock(owner: object) -> ToolClock | None:
    clock = _current.get()
    # Contexts copied into background tasks must not pause their parent's clock.
    if clock is not None and clock.owner is owner and clock.task is asyncio.current_task():
        return clock
    return None
