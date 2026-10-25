"""Cooperative SQLite writer admission, independent of SQLite's busy timeout."""

from __future__ import annotations

import asyncio
import os
from contextlib import AsyncExitStack
from contextlib import asynccontextmanager
from dataclasses import dataclass
from dataclasses import field
from threading import RLock
from typing import TYPE_CHECKING
from typing import TypeVar
from weakref import WeakKeyDictionary

from ._async import finish_on_cancel

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from collections.abc import Awaitable
    from collections.abc import Callable
    from collections.abc import Iterable
    from pathlib import Path

T = TypeVar("T")


@dataclass
class _Gate:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0
    owners: set[asyncio.Task[object]] = field(default_factory=set)


_gates: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, _Gate]] = WeakKeyDictionary()
_registry = RLock()


@asynccontextmanager
async def database_write(path: Path) -> AsyncIterator[None]:
    """Admit one FIFO writer per loop/path; waiting cancellation owns no SQL.

    Callers must finish their transaction and close its connection before
    leaving this scope. Read-only connections need no admission.
    """
    loop = asyncio.get_running_loop()
    owner = asyncio.current_task()
    assert owner is not None
    key = os.path.normcase(str(path.resolve()))
    with _registry:
        gates = _gates.setdefault(loop, {})
        gate = gates.setdefault(key, _Gate())
        if owner in gate.owners:
            raise RuntimeError("SQLite write admission cannot be reentered for the same database")
        gate.users += 1
    acquired = False
    try:
        await gate.lock.acquire()
        acquired = True
        gate.owners.add(owner)
        yield
    finally:
        if acquired:
            gate.owners.remove(owner)
            gate.lock.release()
        with _registry:
            gate.users -= 1
            if not gate.users:
                del gates[key]
            if not gates:
                del _gates[loop]


async def write_database(path: Path, operation: Callable[[], Awaitable[T]]) -> T:
    """Acquire cancellably, then own accepted work through commit and close."""
    async with database_write(path):

        async def owned() -> T:
            return await operation()

        return await finish_on_cancel(owned())


@asynccontextmanager
async def database_writes(paths: Iterable[Path]) -> AsyncIterator[None]:
    """Reserve attached databases in canonical order, without nested aliases."""
    unique = {os.path.normcase(str(path.resolve())): path for path in paths}
    async with AsyncExitStack() as stack:
        for key in sorted(unique):
            await stack.enter_async_context(database_write(unique[key]))
        yield
