"""Secret-free remote authentication status and one-use OAuth refresh tests."""

from __future__ import annotations

import asyncio
import json
import os
from typing import TYPE_CHECKING
from urllib.parse import parse_qs

import httpx
import pytest

from nagents.harness.private_store import ProtectedStoreError
from nagents.harness.remote_credentials import RemoteCredentialError
from nagents.harness.remote_credentials import RemoteCredentials
from nagents.harness.remote_profiles import RemoteProfile

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.requires_posix

ISSUER = "https://issuer.example.test"
TOKEN_ENDPOINT = f"{ISSUER}/token"


def oauth_profile(*, token_env: str = "") -> RemoteProfile:
    return RemoteProfile(
        url="https://agent.example.test",
        issuer="" if token_env else ISSUER,
        client_id="" if token_env else "public-client",
        token_env=token_env,
        scopes=("openid", "remote:invoke"),
    )


def seed_grant(
    credentials: RemoteCredentials,
    *,
    access_token: str = "access-before-refresh",
    expires_at: float,
    refresh_token: str = "refresh-before-refresh",
    refresh_pending: bool = False,
) -> None:
    credentials.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    credentials.path.parent.parent.chmod(0o700)
    credentials.path.parent.chmod(0o700)
    document: dict[str, str | float | bool] = {
        "access_token": access_token,
        "expires_at": expires_at,
        "refresh_token": refresh_token,
        "token_endpoint": TOKEN_ENDPOINT,
    }
    if refresh_pending:
        document["refresh_pending"] = True
    credentials.path.write_text(json.dumps(document), encoding="utf-8")
    credentials.path.chmod(0o600)


def test_authentication_status_is_secret_free_and_classifies_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        oauth = RemoteCredentials(tmp_path / "oauth", oauth_profile())
        assert await oauth.status() == {
            "state": "sign_in_required",
            "expires_at": 0.0,
            "error_code": "A2A_AUTH_REQUIRED",
            "refreshable": False,
        }
        oauth.save(
            "private-access-token",
            4102444800.0,
            refresh_token="private-refresh-token",
            token_endpoint=TOKEN_ENDPOINT,
        )
        status = await oauth.status()
        assert status == {
            "state": "signed_in",
            "expires_at": 4102444800.0,
            "error_code": "",
            "refreshable": True,
        }
        assert "private-access-token" not in json.dumps(status)
        assert "private-refresh-token" not in json.dumps(status)

        expiring = RemoteCredentials(tmp_path / "expiring", oauth_profile())
        seed_grant(expiring, expires_at=1.0)
        assert (await expiring.status())["state"] == "signed_in"
        assert (await expiring.status())["refreshable"] is True
        assert (await expiring.status())["expires_at"] == 1.0
        another_workspace = RemoteCredentials(tmp_path / "other-workspace", oauth_profile())
        isolated = await another_workspace.status()
        assert isolated["state"] == "sign_in_required"
        assert isolated["refreshable"] is False
        no_refresh = RemoteCredentials(tmp_path / "no-refresh", oauth_profile())
        no_refresh.save("expires-soon", 4102444800.0)
        no_refresh.path.write_text(json.dumps({"access_token": "expired", "expires_at": 1.0}), encoding="utf-8")
        no_refresh.path.chmod(0o600)
        assert (await no_refresh.status())["state"] == "sign_in_required"
        assert (await no_refresh.status())["refreshable"] is False

        env_profile = RemoteCredentials(tmp_path / "env", oauth_profile(token_env="NGN_REMOTE_TEST_TOKEN"))
        assert (await env_profile.status())["state"] == "token_missing"
        monkeypatch.setenv("NGN_REMOTE_TEST_TOKEN", "private-env-token")
        env_status = await env_profile.status()
        assert env_status == {
            "state": "token_ready",
            "expires_at": 0.0,
            "error_code": "",
            "refreshable": False,
        }
        assert "private-env-token" not in json.dumps(env_status)

        no_auth = RemoteCredentials(tmp_path / "plain", RemoteProfile(url="https://agent.example.test"))
        assert (await no_auth.status())["state"] == "not_required"
        host_setup = RemoteCredentials(
            tmp_path / "host-setup", RemoteProfile(url="https://agent.example.test", issuer=ISSUER)
        )
        assert (await host_setup.status())["state"] == "host_setup_required"

        unsafe = RemoteCredentials(tmp_path / "unsafe", oauth_profile())
        unsafe.path.parent.mkdir(mode=0o700, parents=True)
        unsafe.path.parent.parent.chmod(0o700)
        unsafe.path.parent.chmod(0o700)
        target = tmp_path / "outside.json"
        target.write_text("{}", encoding="utf-8")
        unsafe.path.symlink_to(target)
        assert (await unsafe.status())["state"] == "storage_error"
        assert (await unsafe.status())["error_code"] == "A2A_AUTH_STORAGE"

    asyncio.run(scenario())


class RotatingTransport(httpx.AsyncBaseTransport):
    def __init__(self, seen: list[str], *, fail: bool = False) -> None:
        self.seen = seen
        self.fail = fail

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode("ascii"))
        self.seen.append(form["refresh_token"][0])
        await asyncio.sleep(0.03)
        if self.fail:
            raise httpx.ReadTimeout("private response body and token detail", request=request)
        index = len(self.seen)
        return httpx.Response(
            200,
            json={
                "access_token": f"fresh-access-{index}",
                "refresh_token": f"fresh-refresh-{index}",
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "openid remote:invoke",
            },
            request=request,
        )

    async def aclose(self) -> None:
        return None


class PausedTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.requests = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        self.started.set()
        await self.release.wait()
        return httpx.Response(
            200,
            json={
                "access_token": "refreshed-access",
                "refresh_token": "rotated-refresh",
                "token_type": "Bearer",
                "expires_in": 3600,
            },
            request=request,
        )

    async def aclose(self) -> None:
        return None


class RejectedTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.requests = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        return httpx.Response(
            400,
            json={"error": "invalid_grant", "error_description": "private account information"},
            request=request,
        )

    async def aclose(self) -> None:
        return None


def install_transport(monkeypatch: pytest.MonkeyPatch, transport: httpx.AsyncBaseTransport) -> None:
    original = httpx.AsyncClient

    def client(*, transport: httpx.AsyncBaseTransport | None = None, **kwargs: object) -> httpx.AsyncClient:
        return original(transport=transport or client_transport, **kwargs)  # type: ignore[arg-type]

    client_transport = transport
    monkeypatch.setattr(httpx, "AsyncClient", client)


def test_concurrent_expired_grants_rotate_once_and_use_rotated_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        seen: list[str] = []
        install_transport(monkeypatch, RotatingTransport(seen))
        profile = oauth_profile()
        credentials = [RemoteCredentials(tmp_path / "shared-workspace", profile) for _ in range(10)]
        seed_grant(credentials[0], expires_at=1.0)

        tokens = await asyncio.gather(*(credential.token() for credential in credentials))

        assert tokens == ["fresh-access-1"] * 10
        assert seen == ["refresh-before-refresh"]
        persisted = json.loads(credentials[0].path.read_text(encoding="utf-8"))
        assert persisted["refresh_token"] == "fresh-refresh-1"
        assert persisted.get("refresh_pending") is not True

        seed_grant(
            credentials[0],
            access_token="expired-access",
            expires_at=1.0,
            refresh_token="fresh-refresh-1",
        )
        assert await credentials[1].token() == "fresh-access-2"
        assert seen == ["refresh-before-refresh", "fresh-refresh-1"]

    asyncio.run(scenario())


def test_uncertain_response_and_crash_marker_never_replay_refresh_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        seen: list[str] = []
        install_transport(monkeypatch, RotatingTransport(seen, fail=True))
        credentials = RemoteCredentials(tmp_path / "uncertain", oauth_profile())
        seed_grant(credentials, expires_at=1.0)

        with pytest.raises(RemoteCredentialError) as first:
            await credentials.token()
        assert first.value.code == "A2A_AUTH_REFRESH_UNCERTAIN"
        assert "private response body" not in str(first.value)
        stored = json.loads(credentials.path.read_text(encoding="utf-8"))
        assert stored["refresh_pending"] is True

        with pytest.raises(RemoteCredentialError) as second:
            await RemoteCredentials(tmp_path / "uncertain", oauth_profile()).token()
        assert second.value.code == "A2A_AUTH_REFRESH_UNCERTAIN"
        assert seen == ["refresh-before-refresh"]
        status = await credentials.status()
        assert status["state"] == "sign_in_required"
        assert status["refreshable"] is False

    asyncio.run(scenario())


def test_new_grant_write_failure_keeps_refresh_pending_and_refuses_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        seen: list[str] = []
        install_transport(monkeypatch, RotatingTransport(seen))
        credentials = RemoteCredentials(tmp_path / "write-failure", oauth_profile())
        seed_grant(credentials, expires_at=1.0)

        def fail_save(*args: object, **kwargs: object) -> None:
            raise ProtectedStoreError("private path detail")

        monkeypatch.setattr(credentials, "save", fail_save)
        with pytest.raises(RemoteCredentialError) as first:
            await credentials.token()
        assert first.value.code == "A2A_AUTH_REFRESH_UNCERTAIN"
        assert "private path detail" not in str(first.value)
        assert json.loads(credentials.path.read_text(encoding="utf-8"))["refresh_pending"] is True

        with pytest.raises(RemoteCredentialError) as second:
            await RemoteCredentials(tmp_path / "write-failure", oauth_profile()).token()
        assert second.value.code == "A2A_AUTH_REFRESH_UNCERTAIN"
        assert seen == ["refresh-before-refresh"]

    asyncio.run(scenario())


def test_rejected_refresh_requires_signin_and_clears_the_one_use_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        transport = RejectedTransport()
        install_transport(monkeypatch, transport)
        credentials = RemoteCredentials(tmp_path / "rejected", oauth_profile())
        seed_grant(credentials, expires_at=1.0)

        with pytest.raises(RemoteCredentialError) as first:
            await credentials.token()
        assert first.value.code == "A2A_AUTH_REQUIRED"
        assert "private account information" not in str(first.value)
        persisted = json.loads(credentials.path.read_text(encoding="utf-8"))
        assert "refresh_token" not in persisted
        assert persisted.get("refresh_pending") is not True

        with pytest.raises(RemoteCredentialError) as second:
            await credentials.token()
        assert second.value.code == "A2A_AUTH_REQUIRED"
        assert transport.requests == 1
        status = await credentials.status()
        assert status["state"] == "sign_in_required"
        assert status["refreshable"] is False

    asyncio.run(scenario())


def test_login_commit_waits_for_refresh_and_new_login_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        transport = PausedTransport()
        install_transport(monkeypatch, transport)
        credentials = RemoteCredentials(tmp_path / "login-race", oauth_profile())
        seed_grant(credentials, expires_at=1.0)

        refresh = asyncio.create_task(credentials.token())
        await transport.started.wait()
        during_refresh = await credentials.status()
        assert during_refresh["state"] == "signed_in"
        assert during_refresh["refreshable"] is True
        assert during_refresh["error_code"] == ""
        login = asyncio.create_task(
            credentials.save_login(
                "login-access",
                4102444800.0,
                refresh_token="login-refresh",
                token_endpoint=TOKEN_ENDPOINT,
            )
        )
        await asyncio.sleep(0.02)
        transport.release.set()

        assert await refresh == "refreshed-access"
        await login
        persisted = json.loads(credentials.path.read_text(encoding="utf-8"))
        assert persisted["access_token"] == "login-access"
        assert persisted["refresh_token"] == "login-refresh"
        assert persisted.get("refresh_pending") is not True
        assert await credentials.token() == "login-access"
        assert transport.requests == 1

    asyncio.run(scenario())


def test_refresh_lock_rejects_hardlinks_before_contacting_token_endpoint(tmp_path: Path) -> None:
    async def scenario() -> None:
        credentials = RemoteCredentials(tmp_path / "hardlink-lock", oauth_profile())
        seed_grant(credentials, expires_at=1.0, refresh_pending=True)
        lock = credentials.path.parent / f".{credentials.profile.fingerprint}.refresh.lock"
        outside = tmp_path / "outside-lock"
        outside.write_text("lock fixture", encoding="utf-8")
        outside.chmod(0o600)
        os.link(outside, lock)
        assert outside.stat().st_nlink == 2
        status = await credentials.status()
        assert status["state"] == "storage_error"
        assert status["error_code"] == "A2A_AUTH_STORAGE"
        with pytest.raises(RemoteCredentialError) as error:
            await credentials.token()
        assert error.value.code == "A2A_AUTH_STORAGE"

    asyncio.run(scenario())
