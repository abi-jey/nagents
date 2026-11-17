"""Portable auth protocol/ownership fixtures do not implement secure file storage."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from time import time
from typing import TYPE_CHECKING

import pytest

from nagents.provider.device_auth import OpenAIAuth
from nagents.provider.device_auth import OpenAIAuthError
from nagents.provider.device_auth import _Tokens
from tests.support.device_auth import MemoryAuthStore
from tests.support.device_auth import UnexpectedPrivateAuthIO
from tests.support.device_auth import owned_credentials
from tests.support.hang_guard import HANG_GUARD

if TYPE_CHECKING:
    from pathlib import Path


def test_memory_revisions_and_ownership_coalesce_real_auth_callbacks_without_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        path = tmp_path / "never-created" / "openai.json"
        primary, borrowed = OpenAIAuth(path), OpenAIAuth(path)
        memory = MemoryAuthStore(_Tokens("expired-fixture", "refresh-fixture", "identity-fixture", 0))
        memory.install(primary, monkeypatch)
        memory.install(borrowed, monkeypatch)
        ready, release = asyncio.Event(), asyncio.Event()
        refreshes = 0

        async def exchange(form: dict[str, str], deadline: float, previous: _Tokens | None = None) -> _Tokens:
            nonlocal refreshes
            refreshes += 1
            ready.set()
            await release.wait()
            return _Tokens("fresh-fixture", "refresh-fixture", "identity-fixture", time() + 3600)

        async def unexpected_exchange(
            form: dict[str, str], deadline: float, previous: _Tokens | None = None
        ) -> _Tokens:
            raise AssertionError("The borrowed callback must reload the owner's rotated token.")

        monkeypatch.setattr(primary, "_exchange", exchange)
        monkeypatch.setattr(borrowed, "_exchange", unexpected_exchange)
        async with asyncio.timeout(HANG_GUARD), owned_credentials(primary, ready) as (tasks, first):
            second = tasks.create_task(borrowed.credentials())
            release.set()
        assert refreshes == 1
        assert first.result().access_token == second.result().access_token == "fresh-fixture"
        assert not memory.metadata.refresh_attempt
        revision = memory.metadata.revision
        borrowed.logout()
        assert memory.metadata.revision != revision and memory.metadata.logged_out
        assert memory.tokens is None and not primary.logged_in()
        with pytest.raises(OpenAIAuthError, match="sign in again"):
            await primary.credentials()
        assert memory.private_accesses == 0 and not path.parent.exists()
        assert not memory.store_lock.held and not memory.refresh_lock.held

    asyncio.run(scenario())


def test_memory_logout_revision_invalidates_an_inflight_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        path = tmp_path / "never-created" / "openai.json"
        first, other = OpenAIAuth(path), OpenAIAuth(path)
        memory = MemoryAuthStore(_Tokens("expired-fixture", "refresh-fixture", "identity-fixture", 0))
        memory.install(first, monkeypatch)
        memory.install(other, monkeypatch)
        ready, release = asyncio.Event(), asyncio.Event()

        async def exchange(form: dict[str, str], deadline: float, previous: _Tokens | None = None) -> _Tokens:
            ready.set()
            await release.wait()
            return _Tokens("fresh-fixture", "refresh-fixture", "identity-fixture", time() + 3600)

        monkeypatch.setattr(first, "_exchange", exchange)
        with pytest.raises(ExceptionGroup) as caught:
            async with asyncio.timeout(HANG_GUARD), owned_credentials(first, ready):
                await other.logout_async()
                release.set()
        assert len(caught.value.exceptions) == 1
        error = caught.value.exceptions[0]
        assert isinstance(error, OpenAIAuthError) and "removed during refresh" in str(error)
        assert memory.tokens is None and memory.metadata.logged_out
        assert memory.private_accesses == 0 and not path.parent.exists()
        assert not memory.store_lock.held and not memory.refresh_lock.held

    asyncio.run(scenario())


def test_partial_token_mocks_reproduce_boundary_failure_and_interrupt_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        auth = OpenAIAuth(tmp_path / "never-created" / "openai.json")
        memory = MemoryAuthStore(_Tokens("expired-fixture", "refresh-fixture", "identity-fixture", 0))
        # Reproduce G2's old incomplete fixture: no metadata/ownership doubles.
        # A fail-on-private-access boundary reproduces the early Windows failure
        # on POSIX too, without modifying global os.name or pathlib behavior.
        memory.reject_private_io(auth, monkeypatch)
        monkeypatch.setattr(auth, "_load", memory.load)
        monkeypatch.setattr(auth, "_save", memory.save)
        ready = asyncio.Event()

        async def exchange(form: dict[str, str], deadline: float, previous: _Tokens | None = None) -> _Tokens:
            ready.set()
            raise AssertionError("The early filesystem failure must precede token exchange.")

        monkeypatch.setattr(auth, "_exchange", exchange)
        before = asyncio.all_tasks()
        with pytest.raises(ExceptionGroup) as caught:
            async with asyncio.timeout(HANG_GUARD), owned_credentials(auth, ready):
                pytest.fail("A failed owner must never report readiness")
        # The original error, not a 60-second TimeoutError or a swallowed task
        # exception, is what reaches the consumer. All owned tasks are joined.
        assert len(caught.value.exceptions) == 1
        assert isinstance(caught.value.exceptions[0], UnexpectedPrivateAuthIO)
        assert memory.private_accesses == 1 and not ready.is_set()
        assert asyncio.all_tasks() == before

    asyncio.run(scenario())


def test_credential_owner_completing_without_readiness_is_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        auth = OpenAIAuth(tmp_path / "never-created" / "openai.json")
        memory = MemoryAuthStore(_Tokens("fresh-fixture", "refresh-fixture", "identity-fixture", time() + 3600))
        memory.install(auth, monkeypatch)
        ready = asyncio.Event()
        before = asyncio.all_tasks()
        with pytest.raises(ExceptionGroup) as caught:
            async with asyncio.timeout(HANG_GUARD), owned_credentials(auth, ready):
                pytest.fail("Completion without readiness must not enter the consumer")
        assert len(caught.value.exceptions) == 1
        error = caught.value.exceptions[0]
        assert isinstance(error, AssertionError) and str(error) == "Credential owner completed before readiness."
        assert not ready.is_set() and memory.private_accesses == 0
        assert asyncio.all_tasks() == before

    asyncio.run(scenario())


@pytest.mark.parametrize("after_readiness", [False, True])
def test_cancelled_consumer_joins_refresh_and_readiness_and_releases_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after_readiness: bool
) -> None:
    async def scenario() -> None:
        auth = OpenAIAuth(tmp_path / "never-created" / "openai.json")
        memory = MemoryAuthStore(_Tokens("expired-fixture", "refresh-fixture", "identity-fixture", 0))
        memory.install(auth, monkeypatch)
        exchange_started, exchange_closed = asyncio.Event(), asyncio.Event()
        ready, consumer_started = asyncio.Event(), asyncio.Event()

        async def exchange(form: dict[str, str], deadline: float, previous: _Tokens | None = None) -> _Tokens:
            exchange_started.set()
            if after_readiness:
                ready.set()
            try:
                await asyncio.Event().wait()
                raise AssertionError("An unreleased exchange cannot finish normally.")
            finally:
                exchange_closed.set()

        async def consume() -> None:
            async with owned_credentials(auth, ready):
                consumer_started.set()
                await asyncio.Event().wait()

        monkeypatch.setattr(auth, "_exchange", exchange)
        before = asyncio.all_tasks()
        async with asyncio.timeout(HANG_GUARD), asyncio.TaskGroup() as tasks:
            consumer = tasks.create_task(consume())
            await exchange_started.wait()
            if after_readiness:
                await consumer_started.wait()
            consumer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await consumer
        assert exchange_closed.is_set()
        assert consumer_started.is_set() is after_readiness
        assert not memory.store_lock.held and not memory.refresh_lock.held
        assert memory.metadata.refresh_attempt and memory.private_accesses == 0
        assert asyncio.all_tasks() == before
        await auth.close()

    asyncio.run(scenario())


def test_real_auth_store_keeps_non_posix_guard_in_isolated_process(tmp_path: Path) -> None:
    script = """
import asyncio
import os
import sys
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
import aiohttp
from nagents import protected_store
from nagents.provider.device_auth import DeviceAuthorization, OpenAIAuth, OpenAIAuthError, _tokens

host_platform = os.name
path = Path(sys.argv[1])
auth = OpenAIAuth(path)
# Replace only this module's filesystem dependency, never global os/Path.
protected_store.os = SimpleNamespace(name='nt')
def no_requests(*args, **kwargs):
    raise AssertionError('Unsupported storage must fail before an authentication request')
aiohttp.ClientSession = no_requests
def guarded(operation):
    try:
        operation()
    except OpenAIAuthError as error:
        assert str(error) == 'Protected ChatGPT credential storage currently requires POSIX.'
    else:
        raise AssertionError('The real credential store must not accept a non-POSIX platform')
authorization = DeviceAuthorization('https://auth.openai.com/codex/device', 'FIXTURE', 'fixture-device',
                                    1, monotonic() + 60)
guarded(lambda: asyncio.run(auth.credentials()))
guarded(lambda: asyncio.run(auth.complete_device_login(authorization)))
guarded(auth.logout)
guarded(lambda: asyncio.run(auth.logout_async()))
tokens = _tokens({'access_token': 'fixture-access', 'refresh_token': 'fixture-refresh',
                  'id_token': 'fixture-identity', 'expires_in': 3600})
guarded(lambda: auth._save(tokens))
assert not auth.logged_in() and 'unusable' in auth.status()
asyncio.run(auth.close())
assert os.name == host_platform and not path.parent.exists()
blocked = ('nagents.harness', 'nagents.tui', 'nagents.web')
assert not any(name == item or name.startswith(item + '.') for name in sys.modules for item in blocked)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "never-created" / "openai.json")],
        env={key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
        capture_output=True,
        text=True,
        timeout=HANG_GUARD,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
