"""In-memory auth state for portable protocol/consumer tests, never a secure store.

The production filesystem boundary remains POSIX-only. These doubles replace
tokens, revisions, and both ownership locks together; installing only token mocks
would still enter the real private store. Every replaced filesystem boundary is
also trapped so an incomplete fixture fails even on a POSIX test host.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextlib import contextmanager
from dataclasses import replace
from typing import TYPE_CHECKING
from typing import NoReturn

from nagents.provider.device_auth import OpenAIAuthError
from nagents.provider.device_auth import _StoreState

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from collections.abc import Iterator

    import pytest

    from nagents.provider.device_auth import OpenAIAuth
    from nagents.provider.device_auth import _Tokens
    from nagents.provider.openai import CodexCredentials


class UnexpectedPrivateAuthIO(AssertionError):
    """A portable auth unit fixture escaped into the real private filesystem."""


class MemoryAuthOwnership:
    """Cooperative task ownership only: not a substitute for POSIX flock."""

    def __init__(self) -> None:
        self.held = False
        self._released = asyncio.Event()
        self._released.set()

    @contextmanager
    def try_acquire(self) -> Iterator[None]:
        if self.held:
            raise OpenAIAuthError("In-memory auth ownership is busy.")
        self.held = True
        self._released.clear()
        try:
            yield
        finally:
            self.held = False
            self._released.set()

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[None]:
        while self.held:
            await self._released.wait()
        # No await between the ownership check and synchronous acquisition.
        with self.try_acquire():
            yield


class MemoryAuthStore:
    def __init__(self, tokens: _Tokens | None = None) -> None:
        self.tokens = replace(tokens) if tokens is not None else None
        self.metadata = _StoreState()
        self.store_lock = MemoryAuthOwnership()
        self.refresh_lock = MemoryAuthOwnership()
        self.private_accesses = 0

    def load(self) -> _Tokens | None:
        return replace(self.tokens) if self.tokens is not None else None

    def save(self, tokens: _Tokens) -> None:
        self.tokens = replace(tokens)

    def state(self) -> _StoreState:
        return self.metadata

    def save_state(self, state: _StoreState) -> None:
        self.metadata = state

    def unlink(self) -> None:
        self.tokens = None

    def _reject_private_io(self, create: bool = False) -> NoReturn:
        self.private_accesses += 1
        raise UnexpectedPrivateAuthIO("In-memory auth fixture attempted protected filesystem access.")

    def reject_private_io(self, auth: OpenAIAuth, monkeypatch: pytest.MonkeyPatch) -> None:
        for boundary in (auth, auth._state_store, auth._store_lock, auth._refresh_lock):
            monkeypatch.setattr(boundary, "_directory", self._reject_private_io)

    def install(self, auth: OpenAIAuth, monkeypatch: pytest.MonkeyPatch) -> None:
        self.reject_private_io(auth, monkeypatch)
        monkeypatch.setattr(auth, "_load", self.load)
        monkeypatch.setattr(auth, "_save", self.save)
        monkeypatch.setattr(auth, "_state", self.state)
        monkeypatch.setattr(auth, "_save_state", self.save_state)
        monkeypatch.setattr(auth, "_unlink", self.unlink)
        monkeypatch.setattr(auth, "_store_lock", self.store_lock)
        monkeypatch.setattr(auth, "_refresh_lock", self.refresh_lock)


@asynccontextmanager
async def owned_credentials(
    auth: OpenAIAuth, ready: asyncio.Event
) -> AsyncIterator[tuple[asyncio.TaskGroup, asyncio.Task[CodexCredentials]]]:
    """Own refresh and readiness together; early failure interrupts the consumer.

    TaskGroup cancels/joins children on failure or cancellation, including before
    the exchange signals readiness. Normal completion without readiness is also
    an error, not a reason to wait silently until the caller's watchdog expires.
    """
    async with asyncio.TaskGroup() as tasks:
        owner = tasks.create_task(auth.credentials())
        waiting = tasks.create_task(ready.wait())
        try:
            await asyncio.wait((owner, waiting), return_when=asyncio.FIRST_COMPLETED)
            if owner.done():
                owner.result()
                if not ready.is_set():
                    raise AssertionError("Credential owner completed before readiness.")
            await waiting
            yield tasks, owner
        finally:
            waiting.cancel()
