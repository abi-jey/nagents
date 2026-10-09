"""Immutable client requests and transport-neutral Live update callbacks."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ClientDelegationRequest:
    """A notice's original identity and context, captured before backend work.

    Public Live supplies transcript context, while ChatGPT login can also supply
    an explicit native request in ``text``. Neither is trusted instructions.
    """

    identifier: str
    text: str = ""
    transcript: str = "[]"
    offset_ms: float | None = None


ClientDelegationHandler = Callable[[ClientDelegationRequest], Awaitable[str]]
ClientDelegationObserver = Callable[[ClientDelegationRequest], None]
LiveAppendKind = Literal["thinking", "commentary", "instructions"]
# Return the actual wire event type after sending, or "" if detached/unsupported.
# This is a transport write receipt, never proof of provider injection/playback.
LiveAppend = Callable[[LiveAppendKind, str, str], Awaitable[str]]


async def delegation_workers(
    worker: Callable[[], Coroutine[object, object, None]], concurrency: int, on_stop: Callable[[], None]
) -> None:
    """Own a fixed worker pool; cancellation or a failed send stops every lane."""
    tasks = [asyncio.create_task(worker(), name="live-client-delegation") for _ in range(concurrency)]
    aggregate = asyncio.gather(*tasks)
    try:
        # Mark the transport closed before cancellation reaches a handler that
        # catches CancelledError and returns one last result during cleanup.
        await asyncio.shield(aggregate)
    finally:
        on_stop()
        aggregate.cancel()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(aggregate, return_exceptions=True)
