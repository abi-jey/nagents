"""Device OAuth tests use fake tokens, isolated XDG paths, and loopback only."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import stat
import traceback
from contextlib import asynccontextmanager
from dataclasses import replace
from time import monotonic
from time import time
from typing import TYPE_CHECKING

import pytest
from aiohttp import web

from nagents.harness import auth as auth_module
from nagents.harness.auth import CLIENT_ID
from nagents.harness.auth import OpenAIAuth
from nagents.harness.auth import OpenAIAuthError
from nagents.harness.auth import _tokens

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from collections.abc import Awaitable
    from collections.abc import Callable
    from pathlib import Path


ACCESS = "fake-access-secret"
REFRESH = "fake-refresh-secret"
IDENTITY = "fake-id-secret"
DEVICE = "fake-device-secret"
CODE = "FAKE-CODE"


def token_data(**changes: object) -> dict[str, object]:
    return {"access_token": ACCESS, "refresh_token": REFRESH, "id_token": IDENTITY, "expires_in": 3600, **changes}


def jwt(claims: dict[str, object]) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"fake.{payload}.signature"


@pytest.fixture(autouse=True)
def private_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


@asynccontextmanager
async def issuer(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
) -> AsyncIterator[str]:
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handler)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    url = f"http://127.0.0.1:{runner.addresses[0][1]}"
    monkeypatch.setattr(auth_module, "ISSUER", url)
    try:
        yield url
    finally:
        await runner.cleanup()


@pytest.mark.requires_posix
def test_device_success_pending_and_secure_reopen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def scenario() -> None:
        polls = 0
        waits: list[float] = []

        async def wait(self: OpenAIAuth, delay: float, deadline: float) -> None:
            waits.append(delay)

        monkeypatch.setattr(OpenAIAuth, "_wait", wait)
        account_token = jwt(
            {
                "https://api.openai.com/auth": {"chatgpt_account_id": "nested-account"},
                "chatgpt_account_id": "top-account",
            }
        )
        access_token = jwt({"https://api.openai.com/auth": {"chatgpt_compute_residency": "eu"}, "exp": time() + 1800})

        async def handle(request: web.Request) -> web.Response:
            nonlocal polls
            assert request.headers["User-Agent"].startswith("ngn/")
            assert "Authorization" not in request.headers
            if request.path.endswith("/usercode"):
                assert await request.json() == {"client_id": CLIENT_ID}
                return web.json_response({"device_auth_id": DEVICE, "usercode": CODE, "interval": "2"})
            if request.path.endswith("/deviceauth/token"):
                assert await request.json() == {"device_auth_id": DEVICE, "user_code": CODE}
                polls += 1
                if polls <= 2:
                    return web.Response(status=403 if polls == 1 else 404)
                return web.json_response({"authorization_code": "fake-auth-code", "code_verifier": "fake-verifier"})
            assert request.path == "/oauth/token"
            assert request.content_type == "application/x-www-form-urlencoded"
            assert dict(await request.post()) == {
                "grant_type": "authorization_code",
                "code": "fake-auth-code",
                "code_verifier": "fake-verifier",
                "client_id": CLIENT_ID,
                "redirect_uri": auth_module.ISSUER + "/deviceauth/callback",
            }
            return web.json_response(token_data(access_token=access_token, id_token=account_token))

        async with issuer(monkeypatch, handle) as url:
            auth = OpenAIAuth()
            assert auth.path == tmp_path / "xdg" / "ngn" / "auth" / "openai.json"
            assert not auth.logged_in() and auth.status() == "Not signed in"
            authorization = await auth.start_device_login()
            assert authorization.verification_url == url + "/codex/device"
            assert authorization.user_code == CODE
            assert 890 < authorization.expires_at - monotonic() <= 900
            assert DEVICE not in repr(authorization) and CODE not in repr(authorization)
            await auth.complete_device_login(authorization)
            assert waits == [2.0, 2.0]
            assert stat.S_IMODE(auth.path.stat().st_mode) == 0o600
            assert stat.S_IMODE(auth.path.parent.stat().st_mode) == 0o700
            reopened = OpenAIAuth()
            assert reopened.logged_in() and reopened.status() == "ChatGPT device login saved"
            credentials = await reopened.credentials()
            assert credentials.access_token == access_token
            assert credentials.account_id == "nested-account" and credentials.residency == "eu"
            assert access_token not in repr(credentials) and "nested-account" not in repr(credentials)
            assert "access_token" not in repr(_tokens(token_data()))
            await auth.close()
            reopened.logout()
            assert not reopened.logged_in() and not reopened.path.exists()
            reopened.logout()
        captured = capsys.readouterr()
        for secret in (ACCESS, REFRESH, IDENTITY, DEVICE, CODE, access_token, account_token):
            assert secret not in captured.out + captured.err + caplog.text

    asyncio.run(scenario())


@pytest.mark.parametrize("interval,expected", [(0, 1.0), ("0", 1.0), ("3", 3.0), (None, 5.0), ("bad", 5.0)])
def test_interval_parsing(monkeypatch: pytest.MonkeyPatch, interval: object, expected: float) -> None:
    async def scenario() -> None:
        async def handle(request: web.Request) -> web.Response:
            return web.json_response({"device_auth_id": DEVICE, "user_code": CODE, "interval": interval})

        async with issuer(monkeypatch, handle):
            assert (await OpenAIAuth().start_device_login()).interval == expected

    asyncio.run(scenario())


def test_rate_limits_slow_down_and_bounded_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        waits: list[float] = []
        polls = 0

        async def wait(self: OpenAIAuth, delay: float, deadline: float) -> None:
            waits.append(delay)

        monkeypatch.setattr(OpenAIAuth, "_wait", wait)

        async def handle(request: web.Request) -> web.Response:
            nonlocal polls
            if request.path.endswith("/usercode"):
                return web.json_response({"device_auth_id": DEVICE, "user_code": CODE, "interval": 1})
            polls += 1
            if polls == 1:
                return web.json_response({"error": "slow_down"}, status=429, headers={"Retry-After": "9"})
            if polls == 2:
                return web.json_response({"error": "slow_down"}, status=400)
            return web.json_response({"error": "expired_token", "error_description": ACCESS}, status=400)

        async with issuer(monkeypatch, handle):
            auth = OpenAIAuth()
            authorization = await auth.start_device_login()
            with pytest.raises(OpenAIAuthError, match="expired"):
                await auth.complete_device_login(authorization)
            assert waits == [9.0, 14.0]
            with pytest.raises(OpenAIAuthError, match="expired"):
                await auth.complete_device_login(replace(authorization, expires_at=monotonic() - 1))
            assert polls == 3

    asyncio.run(scenario())


def test_deadline_interrupts_pending_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async def handle(request: web.Request) -> web.Response:
            if request.path.endswith("/usercode"):
                return web.json_response({"device_auth_id": DEVICE, "user_code": CODE})
            return web.Response(status=403)

        async with issuer(monkeypatch, handle):
            auth = OpenAIAuth()
            authorization = replace(await auth.start_device_login(), expires_at=monotonic() + 0.03)
            with pytest.raises(OpenAIAuthError, match="expired"):
                await asyncio.wait_for(auth.complete_device_login(authorization), timeout=1)
            assert not auth.path.exists()

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["start", "exchange", "refresh"])
@pytest.mark.requires_posix
def test_rate_limit_retry_on_code_and_token_endpoints(monkeypatch: pytest.MonkeyPatch, phase: str) -> None:
    async def scenario() -> None:
        limited = False
        waits: list[float] = []

        async def wait(self: OpenAIAuth, delay: float, deadline: float) -> None:
            waits.append(delay)

        monkeypatch.setattr(OpenAIAuth, "_wait", wait)

        async def handle(request: web.Request) -> web.Response:
            nonlocal limited
            limit_path = "/api/accounts/deviceauth/usercode" if phase == "start" else "/oauth/token"
            if not limited and request.path == limit_path:
                limited = True
                return web.Response(status=429, text=ACCESS, headers={"Retry-After": "7"})
            if request.path.endswith("/usercode"):
                return web.json_response({"device_auth_id": DEVICE, "user_code": CODE})
            if request.path.endswith("/deviceauth/token"):
                return web.json_response({"authorization_code": "fake-code", "code_verifier": "fake-verifier"})
            return web.json_response(token_data())

        async with issuer(monkeypatch, handle):
            auth = OpenAIAuth()
            if phase == "refresh":
                auth._save(_tokens(token_data(expires_in=1)))
                await auth.credentials()
            else:
                await auth.complete_device_login(await auth.start_device_login())
            assert auth.logged_in() and waits == [7.0]

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["cancel", "malformed", "denied", "timeout", "exchange"])
@pytest.mark.requires_posix
def test_failed_login_never_replaces_saved_tokens(monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    async def scenario() -> None:
        pending = asyncio.Event()
        release = asyncio.Event()

        async def handle(request: web.Request) -> web.Response:
            if request.path.endswith("/usercode"):
                return web.json_response({"device_auth_id": DEVICE, "user_code": CODE})
            if failure in {"cancel", "timeout"}:
                pending.set()
                await release.wait()
            if request.path == "/oauth/token":
                return web.json_response(token_data(access_token=""))
            if failure == "exchange":
                return web.json_response({"authorization_code": "fake-code", "code_verifier": "fake-verifier"})
            if failure == "denied":
                return web.json_response({"error": "access_denied", "error_description": ACCESS}, status=403)
            return web.Response(text=ACCESS, content_type="application/json")

        async with issuer(monkeypatch, handle):
            auth = OpenAIAuth()
            auth._save(_tokens(token_data()))
            before = auth.path.read_bytes()
            authorization = await auth.start_device_login()
            if failure == "timeout":
                monkeypatch.setattr(auth_module, "_REQUEST_SECONDS", 0.03)
            task = asyncio.create_task(auth.complete_device_login(authorization))
            try:
                if failure == "cancel":
                    await asyncio.wait_for(pending.wait(), 1)
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    with pytest.raises(OpenAIAuthError) as caught:
                        await asyncio.wait_for(task, 1)
                    assert ACCESS not in "".join(traceback.format_exception(caught.value))
                assert auth.path.read_bytes() == before
            finally:
                release.set()

    asyncio.run(scenario())


@pytest.mark.requires_posix
def test_refresh_rotation_is_serialized_and_saved(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        requests = 0

        async def handle(request: web.Request) -> web.Response:
            nonlocal requests
            requests += 1
            assert request.path == "/oauth/token"
            assert dict(await request.post()) == {
                "grant_type": "refresh_token",
                "refresh_token": REFRESH,
                "client_id": CLIENT_ID,
            }
            return web.json_response(token_data(access_token="new-access", refresh_token="rotated-refresh"))

        async with issuer(monkeypatch, handle):
            auth = OpenAIAuth()
            auth._save(_tokens(token_data(expires_in=10)))
            assert auth.logged_in()  # Local presence, not a refresh request.
            results = await asyncio.gather(auth.credentials(), auth.credentials(), auth.credentials())
            assert all(result.access_token == "new-access" for result in results)
            assert requests == 1
            stored = json.loads(auth.path.read_text())
            assert stored["refresh_token"] == "rotated-refresh"
            assert (await OpenAIAuth().credentials()).access_token == "new-access"
            assert requests == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [400, 401, 403, 500])
@pytest.mark.requires_posix
def test_refresh_errors_are_safe_and_preserve_store(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    async def scenario() -> None:
        async def handle(request: web.Request) -> web.Response:
            return web.json_response({"error": "invalid_grant", "error_description": REFRESH}, status=status)

        async with issuer(monkeypatch, handle):
            auth = OpenAIAuth()
            auth._save(_tokens(token_data(expires_in=1)))
            before = auth.path.read_bytes()
            with pytest.raises(OpenAIAuthError, match="sign in again") as caught:
                await auth.credentials()
            assert REFRESH not in "".join(traceback.format_exception(caught.value))
            assert before == auth.path.read_bytes()

    asyncio.run(scenario())


@pytest.mark.requires_posix
def test_logout_during_refresh_does_not_resurrect_login(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        pending, release = asyncio.Event(), asyncio.Event()

        async def handle(request: web.Request) -> web.Response:
            pending.set()
            await release.wait()
            return web.json_response(token_data(access_token="new-access"))

        async with issuer(monkeypatch, handle):
            auth = OpenAIAuth()
            auth._save(_tokens(token_data(expires_in=1)))
            task = asyncio.create_task(auth.credentials())
            await asyncio.wait_for(pending.wait(), 1)
            auth.logout()
            release.set()
            with pytest.raises(OpenAIAuthError, match="removed during refresh"):
                await task
            assert not auth.path.exists()

    asyncio.run(scenario())


@pytest.mark.parametrize("unsafe", ["symlink", "directory_symlink", "hardlink", "fifo", "file_mode", "directory_mode"])
@pytest.mark.requires_posix
def test_unsafe_stores_refused(tmp_path: Path, unsafe: str) -> None:
    auth = OpenAIAuth()
    auth._save(_tokens(token_data()))
    original = auth.path.read_bytes()
    other = tmp_path / "other"
    if unsafe == "symlink":
        auth.path.rename(other)
        auth.path.symlink_to(other)
    elif unsafe == "directory_symlink":
        auth.path.parent.rename(other)
        auth.path.parent.symlink_to(other, target_is_directory=True)
    elif unsafe == "hardlink":
        os.link(auth.path, other)
    elif unsafe == "fifo":
        auth.path.unlink()
        os.mkfifo(auth.path, 0o600)
    elif unsafe == "file_mode":
        auth.path.chmod(0o644)
    else:
        auth.path.parent.chmod(0o755)
    assert auth.logged_in() is False
    assert "unusable" in auth.status()
    with pytest.raises(OpenAIAuthError):
        asyncio.run(auth.credentials())
    with pytest.raises(OpenAIAuthError):
        auth._save(_tokens(token_data(access_token="replacement")))
    with pytest.raises(OpenAIAuthError):
        auth.logout()
    if unsafe == "symlink":
        assert other.read_bytes() == original


@pytest.mark.parametrize("raw", ["not json fake-access-secret", "[]", '{"version":99}', '{"version":1}'])
@pytest.mark.requires_posix
def test_corrupt_store_does_not_reveal_contents(raw: str) -> None:
    auth = OpenAIAuth()
    auth._save(_tokens(token_data()))
    auth.path.write_text(raw)
    assert not auth.logged_in()
    assert raw not in auth.status()
    with pytest.raises(OpenAIAuthError, match="sign in again") as caught:
        asyncio.run(auth.credentials())
    assert ACCESS not in "".join(traceback.format_exception(caught.value))
    auth.logout()


@pytest.mark.requires_posix
def test_atomic_save_failure_keeps_previous_login(monkeypatch: pytest.MonkeyPatch) -> None:
    auth = OpenAIAuth()
    auth._save(_tokens(token_data()))
    before = auth.path.read_bytes()

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError(ACCESS)

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OpenAIAuthError) as caught:
        auth._save(_tokens(token_data(access_token="new-access")))
    assert ACCESS not in "".join(traceback.format_exception(caught.value))
    assert auth.path.read_bytes() == before
    assert list(auth.path.parent.iterdir()) == [auth.path]


def test_auth_redirects_never_forward_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        paths: list[str] = []

        async def handle(request: web.Request) -> web.Response:
            paths.append(request.path)
            return web.Response(status=307, headers={"Location": auth_module.ISSUER + "/unexpected"}, text=ACCESS)

        async with issuer(monkeypatch, handle):
            with pytest.raises(OpenAIAuthError) as caught:
                await OpenAIAuth().start_device_login()
            assert paths == ["/api/accounts/deviceauth/usercode"]
            assert ACCESS not in str(caught.value)

    asyncio.run(scenario())


@pytest.mark.requires_posix
def test_claims_are_optional_routing_metadata_and_expiry_is_bounded() -> None:
    auth = OpenAIAuth()
    auth._save(
        _tokens(
            token_data(
                id_token=jwt({"chatgpt_account_id": "top-account"}),
                access_token=jwt({"chatgpt_compute_residency": "no_constraint"}),
            )
        )
    )
    credentials = asyncio.run(auth.credentials())
    assert credentials.account_id == "top-account" and credentials.residency == ""
    with pytest.raises(OpenAIAuthError):
        _tokens(token_data(access_token=jwt({"exp": time() - 1})))
    with pytest.raises(OpenAIAuthError):
        _tokens(token_data(expires_in="nan"))
    auth._save(_tokens(token_data()))
    assert asyncio.run(auth.credentials()).account_id == ""
