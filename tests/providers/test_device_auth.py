"""Public device OAuth uses synthetic fixtures only, with no provider requests."""

from __future__ import annotations

import asyncio
import errno
import os
import stat
import subprocess
import sys
import traceback
from contextlib import asynccontextmanager
from time import monotonic
from typing import TYPE_CHECKING

import pytest

from nagents.protected_store import ProtectedFileLock
from nagents.provider.device_auth import CLIENT_ID
from nagents.provider.device_auth import ISSUER
from nagents.provider.device_auth import DeviceAuthorization
from nagents.provider.device_auth import OpenAIAuth
from nagents.provider.device_auth import OpenAIAuthError
from nagents.provider.device_auth import _StoreState
from nagents.provider.device_auth import _tokens
from tests.support.hang_guard import HANG_GUARD

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

pytestmark = pytest.mark.requires_posix


def token_data(access: str = "fixture-access", *, expires: int = 3600) -> dict[str, object]:
    return {
        "access_token": access,
        "refresh_token": "fixture-refresh",
        "id_token": "fixture-identity",
        "expires_in": expires,
    }


def authorization() -> DeviceAuthorization:
    return DeviceAuthorization(ISSUER + "/codex/device", "FIXTURE-CODE", "fixture-device", 1, monotonic() + 60)


class FixtureAuth(OpenAIAuth):
    def __init__(self, path: Path, *, access: str = "fixture-rotated") -> None:
        super().__init__(path)
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.response = token_data(access)
        self.failure = ""

    async def _post(
        self, route: str, payload: dict[str, str], deadline: float, *, form: bool = False
    ) -> tuple[int, dict[str, object], float]:
        if route == "/api/accounts/deviceauth/token":
            assert payload == {"device_auth_id": "fixture-device", "user_code": "FIXTURE-CODE"}
            assert not form
            return 200, {"authorization_code": "fixture-code", "code_verifier": "fixture-verifier"}, 1
        assert route == "/oauth/token" and form
        assert payload["client_id"] == CLIENT_ID
        if payload["grant_type"] == "refresh_token":
            assert payload["refresh_token"] == "fixture-refresh"
        else:
            assert payload == {
                "grant_type": "authorization_code",
                "code": "fixture-code",
                "redirect_uri": ISSUER + "/deviceauth/callback",
                "client_id": CLIENT_ID,
                "code_verifier": "fixture-verifier",
            }
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        if self.failure:
            raise OpenAIAuthError(self.failure)
        return 200, self.response, 1


def test_public_auth_import_blocks_application_layers_and_discovery(tmp_path: Path) -> None:
    script = """
import asyncio
import importlib.abc
import sys
from pathlib import Path

blocked = ('nagents.harness', 'nagents.tui', 'nagents.web', 'nagents.a2a.server', 'fastapi', 'textual', 'uvicorn')
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        assert not any(fullname == name or fullname.startswith(name + '.') for name in blocked), fullname
sys.meta_path.insert(0, Guard())
from nagents.protected_store import ProtectedFileStore
from nagents.provider.device_auth import OpenAIAuth, OpenAIAuthError
from nagents.provider.openai import OpenAIProvider
from nagents.provider.codex import openai

def no_discovery(*args, **kwargs):
    raise AssertionError('Explicit callback must never discover Codex caches')
openai._load_config = no_discovery
async def scenario():
    path = Path(sys.argv[1])
    auth = OpenAIAuth(path=path)
    assert auth.path == path
    async with OpenAIProvider(credentials=auth.credentials, model='fixture-model') as provider:
        assert provider.uses_chatgpt_auth
        try:
            await provider._credentials()
        except OpenAIAuthError:
            pass
        else:
            raise AssertionError('Missing login must not fall back to an API key')
    await auth.close()
asyncio.run(scenario())
assert not any(name == item or name.startswith(item + '.') for name in sys.modules for item in blocked)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "explicit" / "openai.json")],
        env={
            **{key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
            "CODEX_HOME": str(tmp_path / "never-discover"),
            "OPENAI_API_KEY": "fixture-unused-api-key",
        },
        capture_output=True,
        text=True,
        timeout=HANG_GUARD,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_legacy_modules_are_identity_aliases_and_global_patches_propagate(monkeypatch: pytest.MonkeyPatch) -> None:
    from nagents import protected_store
    from nagents.harness import private_store
    from nagents.harness.providers import auth as legacy
    from nagents.provider import device_auth

    assert legacy is device_auth
    assert private_store is protected_store
    assert legacy.OpenAIAuth is OpenAIAuth
    assert legacy._tokens is _tokens

    def clock() -> float:
        return 42.0

    monkeypatch.setattr(legacy, "monotonic", clock)
    assert vars(device_auth)["monotonic"] is clock
    monkeypatch.setattr(device_auth, "ISSUER", "https://fixture.invalid")
    assert legacy.ISSUER == "https://fixture.invalid"


def test_normal_refresh_reloads_across_instances_and_metadata_is_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        path = tmp_path / "private" / "openai.json"
        first, second = FixtureAuth(path), FixtureAuth(path)
        first._save(_tokens(token_data(expires=1)))
        first.release.clear()
        waiting = asyncio.Event()
        acquire = second._refresh_lock._acquire

        def observed(directory: int, descriptor: int) -> bool:
            result = acquire(directory, descriptor)
            if not result:
                waiting.set()
            return result

        monkeypatch.setattr(second._refresh_lock, "_acquire", observed)
        owner = asyncio.create_task(first.credentials())
        async with asyncio.timeout(HANG_GUARD):
            await first.entered.wait()
            contender = asyncio.create_task(second.credentials())
            await waiting.wait()
            first.release.set()
            results = await asyncio.gather(owner, contender)
        assert all(result.access_token == "fixture-rotated" for result in results)
        assert first.calls == 1 and second.calls == 0
        assert (await FixtureAuth(path).credentials()).access_token == "fixture-rotated"
        assert first._state().refresh_attempt == ""
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        for file in path.parent.iterdir():
            assert stat.S_IMODE(file.stat().st_mode) == 0o600
            assert file.stat().st_nlink == 1
            if file != path:
                assert all(secret not in file.read_text() for secret in ("fixture-refresh", "fixture-identity"))
        locks = {lock.path: lock.path.stat().st_ino for lock in (first._store_lock, first._refresh_lock)}
        await first.logout_async()
        assert not path.exists() and not second.logged_in()
        assert all(file.stat().st_ino == inode for file, inode in locks.items())

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["refresh", "store"])
def test_cancel_waiting_for_flock_closes_descriptors_without_late_acquisition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    async def scenario() -> None:
        path = tmp_path / "private" / "openai.json"
        auth = FixtureAuth(path)
        auth._save(_tokens(token_data()))
        before = path.read_bytes()
        lock = auth._refresh_lock if kind == "refresh" else auth._store_lock
        holder = ProtectedFileLock(lock.path, subject="ChatGPT", error=OpenAIAuthError)
        waiting = asyncio.Event()
        acquire = lock._acquire

        def observed(directory: int, descriptor: int) -> bool:
            result = acquire(directory, descriptor)
            if not result:
                waiting.set()
            return result

        opened: set[int] = set()
        real_open = os.open

        def tracked_open(path: str | Path, flags: int, mode: int = 0o777, *, dir_fd: int = -1) -> int:
            fd = real_open(path, flags, mode) if dir_fd == -1 else real_open(path, flags, mode, dir_fd=dir_fd)
            opened.add(fd)
            return fd

        async with holder.acquire():
            monkeypatch.setattr(lock, "_acquire", observed)
            with monkeypatch.context() as patches:
                patches.setattr(os, "open", tracked_open)
                task = asyncio.create_task(auth.credentials())
                async with asyncio.timeout(HANG_GUARD):
                    await waiting.wait()  # A real LOCK_NB conflict, not a sleep-based timing guess.
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                assert opened
                for fd in opened:
                    with pytest.raises(OSError) as caught:
                        os.fstat(fd)
                    assert caught.value.errno == errno.EBADF
            await auth.close()
            await auth.close()
            with pytest.raises(OpenAIAuthError, match="busy"), lock.try_acquire():
                pytest.fail("Closing a cancelled waiter released another instance's ownership")
            assert auth.calls == 0 and path.read_bytes() == before
            assert not auth._state_store.path.exists()
        assert (await auth.credentials()).access_token == "fixture-access"
        assert auth.calls == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["cancel", "connection", "malformed", "commit"])
def test_attempt_marker_prevents_refresh_replay_after_ambiguous_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def scenario() -> None:
        path = tmp_path / "private" / "openai.json"
        auth = FixtureAuth(path)
        auth._save(_tokens(token_data(expires=1)))
        before = path.read_bytes()
        auth.release.clear()
        if failure == "connection":
            auth.failure = "Fixture connection failed."
        elif failure == "malformed":
            auth.response = token_data("")
        elif failure == "commit":

            def failed_save(tokens: object) -> None:
                raise OpenAIAuthError("Fixture save failed.")

            monkeypatch.setattr(auth, "_save", failed_save)
        task = asyncio.create_task(auth.credentials())
        async with asyncio.timeout(HANG_GUARD):
            await auth.entered.wait()
            if failure == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                auth.release.set()
                with pytest.raises(OpenAIAuthError):
                    await task
        assert before == path.read_bytes() and auth.calls == 1
        reopened = FixtureAuth(path)
        with pytest.raises(OpenAIAuthError, match="refresh was interrupted or failed") as caught:
            await reopened.credentials()
        assert reopened.calls == 0
        assert "fixture-refresh" not in "".join(traceback.format_exception(caught.value))
        for lock in (reopened._refresh_lock, reopened._store_lock):
            with lock.try_acquire():
                pass
        # Only a new explicit device login recovers the poisoned token snapshot.
        await reopened.complete_device_login(authorization())
        assert (await FixtureAuth(path).credentials()).access_token == "fixture-rotated"

    asyncio.run(scenario())
    captured = capsys.readouterr()
    assert "fixture-refresh" not in caplog.text + captured.out + captured.err


def test_cancel_after_exchange_while_waiting_to_commit_never_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        path = tmp_path / "private" / "openai.json"
        auth, other = FixtureAuth(path), FixtureAuth(path)
        auth._save(_tokens(token_data(expires=1)))
        before = path.read_bytes()
        auth.release.clear()
        task = asyncio.create_task(auth.credentials())
        waiting = asyncio.Event()
        acquire = auth._store_lock._acquire

        def observed(directory: int, descriptor: int) -> bool:
            result = acquire(directory, descriptor)
            if not result:
                waiting.set()
            return result

        async with asyncio.timeout(HANG_GUARD):
            await auth.entered.wait()
            async with other._store_lock.acquire():
                monkeypatch.setattr(auth._store_lock, "_acquire", observed)
                auth.release.set()
                await waiting.wait()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            with pytest.raises(OpenAIAuthError, match="refresh was interrupted or failed"):
                await other.credentials()
        assert path.read_bytes() == before and auth.calls == 1 and other.calls == 0
        for lock in (other._refresh_lock, other._store_lock):
            with lock.try_acquire():
                pass

    asyncio.run(scenario())


def test_saved_rotated_tokens_survive_failure_to_clear_attempt_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        path = tmp_path / "private" / "openai.json"
        auth = FixtureAuth(path)
        auth._save(_tokens(token_data(expires=1)))
        save_state = auth._save_state

        def fail_after_replace(state: _StoreState) -> None:
            if not state.refresh_attempt:
                raise OpenAIAuthError("Fixture metadata save failed.")
            save_state(state)

        monkeypatch.setattr(auth, "_save_state", fail_after_replace)
        with pytest.raises(OpenAIAuthError, match="metadata save failed"):
            await auth.credentials()
        assert auth._state().refresh_attempt
        reopened = FixtureAuth(path)
        # The marker identifies only the old snapshot, never the newer token.
        assert (await reopened.credentials()).access_token == "fixture-rotated"
        assert reopened.calls == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("replacement", ["logout", "login", "external_save"])
def test_refresh_never_resurrects_logout_or_clobbers_newer_tokens(tmp_path: Path, replacement: str) -> None:
    async def scenario() -> None:
        path = tmp_path / "private" / "openai.json"
        owner, newer = FixtureAuth(path), FixtureAuth(path, access="fixture-newer-login")
        owner._save(_tokens(token_data(expires=1)))
        owner.release.clear()
        task = asyncio.create_task(owner.credentials())
        async with asyncio.timeout(HANG_GUARD):
            await owner.entered.wait()
            if replacement == "logout":
                newer.logout()
            elif replacement == "login":
                await newer.complete_device_login(authorization())
            else:
                # Even a private/raw writer that bypasses revisions cannot be clobbered.
                newer._save(_tokens(token_data("fixture-newer-login")))
            owner.release.set()
            with pytest.raises(OpenAIAuthError, match=r"removed during refresh|changed during refresh"):
                await task
        if replacement == "logout":
            assert not path.exists()
            with pytest.raises(OpenAIAuthError):
                await FixtureAuth(path).credentials()
        else:
            assert (await FixtureAuth(path).credentials()).access_token == "fixture-newer-login"

    asyncio.run(scenario())


@pytest.mark.parametrize("replacement", ["logout", "login"])
def test_pending_login_cannot_overwrite_another_instance_revision(tmp_path: Path, replacement: str) -> None:
    async def scenario() -> None:
        path = tmp_path / "private" / "openai.json"
        first, newer = FixtureAuth(path), FixtureAuth(path, access="fixture-newer-login")
        first._save(_tokens(token_data()))
        first.release.clear()
        task = asyncio.create_task(first.complete_device_login(authorization()))
        async with asyncio.timeout(HANG_GUARD):
            await first.entered.wait()
            if replacement == "logout":
                await newer.logout_async()
            else:
                await newer.complete_device_login(authorization())
            first.release.set()
            with pytest.raises(OpenAIAuthError, match="cancelled by logout or a newer login"):
                await task
        if replacement == "logout":
            assert not first.logged_in() and not path.exists()
        else:
            assert (await FixtureAuth(path).credentials()).access_token == "fixture-newer-login"

    asyncio.run(scenario())


@pytest.mark.parametrize("logged_out", [False, True])
def test_login_reserves_revision_before_writing_and_preserves_prior_state_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logged_out: bool
) -> None:
    async def scenario() -> None:
        path = tmp_path / "private" / "openai.json"
        older, newer = FixtureAuth(path), FixtureAuth(path, access="fixture-newer-login")
        older._save(_tokens(token_data()))
        if logged_out:
            older.logout()
        before = path.read_bytes() if path.exists() else b""
        older.release.clear()
        task = asyncio.create_task(older.complete_device_login(authorization()))

        def fail_save(tokens: object) -> None:
            raise OpenAIAuthError("Fixture token save failed.")

        monkeypatch.setattr(newer, "_save", fail_save)
        async with asyncio.timeout(HANG_GUARD):
            await older.entered.wait()
            with pytest.raises(OpenAIAuthError, match="token save failed"):
                await newer.complete_device_login(authorization())
            older.release.set()
            with pytest.raises(OpenAIAuthError, match="cancelled by logout or a newer login"):
                await task
        if logged_out:
            assert not path.exists() and not FixtureAuth(path).logged_in()
            with pytest.raises(OpenAIAuthError):
                await FixtureAuth(path).credentials()
        else:
            assert path.read_bytes() == before
            assert (await FixtureAuth(path).credentials()).access_token == "fixture-access"

    asyncio.run(scenario())


def test_logout_tombstone_survives_interrupted_token_removal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        path = tmp_path / "private" / "openai.json"
        auth = FixtureAuth(path)
        auth._save(_tokens(token_data()))
        before = path.read_bytes()

        def failed_unlink() -> None:
            raise OpenAIAuthError("Fixture token removal failed.")

        monkeypatch.setattr(auth, "_unlink", failed_unlink)
        with pytest.raises(OpenAIAuthError, match="removal failed"):
            auth.logout()
        reopened = FixtureAuth(path)
        assert path.read_bytes() == before and not reopened.logged_in() and reopened.status() == "Not signed in"
        with pytest.raises(OpenAIAuthError):
            await reopened.credentials()
        assert reopened.calls == 0
        await reopened.complete_device_login(authorization())
        assert (await FixtureAuth(path).credentials()).access_token == "fixture-rotated"

    asyncio.run(scenario())


@pytest.mark.parametrize("raw", ["fixture-refresh not json", "[]", '{"version":true}', '{"version":1}'])
def test_corrupt_metadata_is_redacted_and_logout_allows_recovery(tmp_path: Path, raw: str) -> None:
    async def scenario() -> None:
        path = tmp_path / "private" / "openai.json"
        auth = FixtureAuth(path)
        auth._save(_tokens(token_data()))
        auth._save_state(_StoreState())
        auth._state_store.path.write_text(raw)
        assert not auth.logged_in() and raw not in auth.status()
        with pytest.raises(OpenAIAuthError) as caught:
            await auth.credentials()
        assert raw not in "".join(traceback.format_exception(caught.value))
        assert auth.calls == 0
        await auth.logout_async()
        await auth.complete_device_login(authorization())
        assert (await FixtureAuth(path).credentials()).access_token == "fixture-rotated"

    asyncio.run(scenario())


def test_sync_logout_fails_fast_and_async_logout_wait_is_cancellable(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "private" / "openai.json"
        auth = FixtureAuth(path)
        auth._save(_tokens(token_data()))
        async with auth._store_lock.acquire():
            other = FixtureAuth(path)
            with pytest.raises(OpenAIAuthError, match="busy"):
                other.logout()
            async with asyncio.timeout(HANG_GUARD):
                # The contender reaches its first LOCK_NB sleep in this loop turn.
                task = asyncio.create_task(other.logout_async())
                await asyncio.sleep(0)
                assert not task.done()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            assert path.exists() and not other._state_store.path.exists()
        await other.logout_async()
        assert not path.exists()

    asyncio.run(scenario())


@pytest.mark.parametrize("sidecar", ["lock", "refresh.lock", "state.json"])
@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "fifo", "mode", "owner"])
def test_unsafe_lock_and_state_files_refuse_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sidecar: str, unsafe: str
) -> None:
    path = tmp_path / "private" / "openai.json"
    auth = FixtureAuth(path)
    auth._save(_tokens(token_data()))
    auth._save_state(_StoreState())
    target = path.with_name(path.name + "." + sidecar)
    if not target.exists():
        target.touch(mode=0o600)
    original = path.read_bytes()
    other = path.parent / "other"
    if unsafe == "symlink":
        target.rename(other)
        target.symlink_to(other)
    elif unsafe == "hardlink":
        os.link(target, other)
    elif unsafe == "fifo":
        target.unlink()
        os.mkfifo(target, 0o600)
    elif unsafe == "mode":
        target.chmod(0o644)
    else:
        inode = target.stat().st_ino
        real_fstat = os.fstat

        def foreign_owner(fd: int) -> os.stat_result:
            info = real_fstat(fd)
            values = list(info)
            if info.st_ino == inode:
                values[4] = os.getuid() + 1
            return os.stat_result(values)

        monkeypatch.setattr(os, "fstat", foreign_owner)
    with pytest.raises(OpenAIAuthError, match="unsafe"):
        asyncio.run(auth.credentials())
    assert auth.calls == 0 and path.read_bytes() == original


def test_replaced_lock_inode_is_rejected_by_waiter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        path = tmp_path / "private" / "openai.json"
        auth = FixtureAuth(path)
        auth._save(_tokens(token_data()))
        lock = auth._refresh_lock
        holder = ProtectedFileLock(lock.path, subject="ChatGPT", error=OpenAIAuthError)
        waiting = asyncio.Event()
        acquire = lock._acquire

        def observed(directory: int, descriptor: int) -> bool:
            result = acquire(directory, descriptor)
            if not result:
                waiting.set()
            return result

        monkeypatch.setattr(lock, "_acquire", observed)
        async with asyncio.timeout(HANG_GUARD):
            async with holder.acquire():
                task = asyncio.create_task(auth.credentials())
                await waiting.wait()
                lock.path.rename(lock.path.with_suffix(".old"))
                lock.path.touch(mode=0o600)
            with pytest.raises(OpenAIAuthError, match="unsafe"):
                await task
        assert auth.calls == 0

    asyncio.run(scenario())


WORKER = """
import asyncio
import sys
from pathlib import Path
from nagents.provider.device_auth import CLIENT_ID, OpenAIAuth, OpenAIAuthError

class Worker(OpenAIAuth):
    async def _post(self, route, payload, deadline, *, form=False):
        assert route == '/oauth/token' and form
        assert payload == {'grant_type': 'refresh_token', 'refresh_token': 'fixture-refresh', 'client_id': CLIENT_ID}
        print('refreshing', flush=True)
        command = await asyncio.to_thread(sys.stdin.readline)
        assert command.strip() == 'continue'
        return 200, {'access_token': 'fixture-rotated', 'refresh_token': 'fixture-refresh-rotated',
                     'id_token': 'fixture-identity', 'expires_in': 3600}, 1

async def scenario():
    auth = Worker(Path(sys.argv[1]))
    acquire = auth._refresh_lock._acquire
    waiting = False
    def observe(directory, descriptor):
        nonlocal waiting
        result = acquire(directory, descriptor)
        if not result and not waiting:
            waiting = True
            print('waiting', flush=True)
        return result
    auth._refresh_lock._acquire = observe
    try:
        result = await auth.credentials()
    except OpenAIAuthError as error:
        assert sys.argv[2] == 'superseded' and 'removed during refresh' in str(error)
        print('superseded', flush=True)
    else:
        assert sys.argv[2] == 'success' and result.access_token == 'fixture-rotated'
        print('done', flush=True)
    await auth.close()
asyncio.run(scenario())
"""


@asynccontextmanager
async def worker(path: Path, mode: str = "success") -> AsyncIterator[asyncio.subprocess.Process]:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        WORKER,
        str(path),
        mode,
        env={key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        yield process
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()


async def line(process: asyncio.subprocess.Process, expected: bytes) -> None:
    assert process.stdout is not None
    result = await process.stdout.readline()
    assert result == expected + b"\n", result


async def release(process: asyncio.subprocess.Process) -> None:
    assert process.stdin is not None
    process.stdin.write(b"continue\n")
    await process.stdin.drain()


async def finished(process: asyncio.subprocess.Process) -> None:
    _, errors = await process.communicate()
    assert process.returncode == 0, errors.decode()


def test_cross_process_refresh_has_one_owner_and_reloads_rotated_tokens(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "private" / "openai.json"
        FixtureAuth(path)._save(_tokens(token_data(expires=1)))
        async with asyncio.timeout(HANG_GUARD), worker(path) as first:
            await line(first, b"refreshing")
            async with worker(path) as second:
                await line(second, b"waiting")
                await release(first)
                await line(first, b"done")
                await line(second, b"done")  # No second "refreshing" request.
                await finished(first)
                await finished(second)
        assert (await FixtureAuth(path).credentials()).access_token == "fixture-rotated"

    asyncio.run(scenario())


@pytest.mark.parametrize("new_login", [False, True])
def test_cross_process_logout_and_new_login_supersede_inflight_refresh(tmp_path: Path, new_login: bool) -> None:
    async def scenario() -> None:
        path = tmp_path / "private" / "openai.json"
        auth = FixtureAuth(path, access="fixture-newer-login")
        auth._save(_tokens(token_data(expires=1)))
        async with asyncio.timeout(HANG_GUARD), worker(path, "superseded") as process:
            await line(process, b"refreshing")
            await auth.logout_async()
            if new_login:
                await auth.complete_device_login(authorization())
            await release(process)
            await line(process, b"superseded")
            await finished(process)
        if new_login:
            assert (await FixtureAuth(path).credentials()).access_token == "fixture-newer-login"
        else:
            assert not path.exists() and not auth.logged_in()

    asyncio.run(scenario())


def test_process_death_releases_flock_but_never_replays_a_refresh(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "private" / "openai.json"
        auth = FixtureAuth(path)
        auth._save(_tokens(token_data(expires=1)))
        before = path.read_bytes()
        async with asyncio.timeout(HANG_GUARD), worker(path) as process:
            await line(process, b"refreshing")
            process.kill()
            await process.wait()
        with pytest.raises(OpenAIAuthError, match="refresh was interrupted or failed"):
            await auth.credentials()
        assert path.read_bytes() == before and auth.calls == 0
        await auth.complete_device_login(authorization())
        assert (await FixtureAuth(path).credentials()).access_token == "fixture-rotated"

    asyncio.run(scenario())
