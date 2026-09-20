"""Cancellation-safe ownership of bounded async work (no optional dependencies)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from typing import TypeVar

if TYPE_CHECKING:
    from collections.abc import Coroutine

T = TypeVar("T")


async def join_owned(task: asyncio.Task[T]) -> T:
    """Finish owned work before propagating cancellation, including repeated cancels."""
    cancelled: tuple[asyncio.CancelledError, ...] = ()
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            cancelled = (error,)
    result = task.result()
    if cancelled:
        raise cancelled[0]
    return result


async def finish_on_cancel(operation: Coroutine[object, object, T]) -> T:
    return await join_owned(asyncio.create_task(operation))
