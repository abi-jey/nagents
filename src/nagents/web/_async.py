"""Web cancellation boundary: also shield AnyIO's level-triggered disconnects."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from typing import TypeVar

import anyio

from nagents._async import join_owned as join_asyncio

if TYPE_CHECKING:
    from collections.abc import Coroutine

T = TypeVar("T")


async def join_owned(task: asyncio.Task[T]) -> T:
    with anyio.CancelScope(shield=True):
        await join_asyncio(task)
    return task.result()


async def finish_on_cancel(operation: Coroutine[object, object, T]) -> T:
    return await join_owned(asyncio.create_task(operation))
