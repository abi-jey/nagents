"""Secret-free remote authentication status and one-use OAuth refresh tests."""

from __future__ import annotations

import asyncio
import errno
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from nagents.harness.private_store import ProtectedStoreError
from nagents.harness.remote.credentials import RemoteCredentialError
from nagents.harness.remote.credentials import RemoteCredentials
from nagents.harness.remote.profiles import RemoteProfile

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


def isolated_credentials(tmp_path: Path, profile: RemoteProfile, name: str) -> RemoteCredentials:
    """Keep unit-test grants in a unique file under pytest's private tmp tree."""
    path = tmp_path / name / "a2a" / f"{profile.fingerprint}.json"
    return RemoteCredentials(profile, path=path)


def seed_grant(
    credentials: RemoteCredentials,
    *,
    access_token: str = "access-before-refresh",
    expires_at: float,
    refresh_token: str = "refresh-before-refresh",
    token_endpoint: str = TOKEN_ENDPOINT,
    refresh_pending: bool = False,
) -> None:
    credentials.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    credentials.path.parent.parent.chmod(0o700)
    credentials.path.parent.chmod(0o700)
    document: dict[str, str | float | bool] = {
        "access_token": access_token,
        "expires_at": expires_at,
        "refresh_token": refresh_token,
        "token_endpoint": token_endpoint,
    }
    if refresh_pending:
        document["refresh_pending"] = True
    credentials.path.write_text(json.dumps(document), encoding="utf-8")
    credentials.path.chmod(0o600)


def test_global_xdg_credentials_survive_reconstruction_and_bind_full_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_home = tmp_path / "xdg-data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    profile = oauth_profile()
    first_workspace = RemoteCredentials(profile)
    expected = data_home / "ngn" / "auth" / "a2a" / f"{profile.fingerprint}.json"
    assert first_workspace.path == expected

    first_workspace.save(
        "persistent-access-token",
        4102444800.0,
        refresh_token="persistent-refresh-token",
        token_endpoint=TOKEN_ENDPOINT,
    )
    restarted_server = RemoteCredentials(profile)
    second_workspace = RemoteCredentials(oauth_profile())
    assert restarted_server.path == first_workspace.path == second_workspace.path
    status = asyncio.run(restarted_server.status())
    assert status["state"] == "signed_in"
    assert status["refreshable"] is True
    assert asyncio.run(second_workspace.token()) == "persistent-access-token"

    changed_bindings: list[dict[str, object]] = [
        {"url": "https://other-agent.example.test"},
        {"rpc_url": "https://agent.example.test/other-rpc"},
        {"issuer": "https://other-issuer.example.test"},
        {"client_id": "other-public-client"},
        {"resource": "https://other-resource.example.test"},
        {"ca_file": "/other/ca.pem"},
        {"scopes": ["openid", "remote:other"]},
        {"timeout": profile.timeout + 1},
        {"metadata_url": "https://issuer.example.test/metadata"},
    ]
    for changes in changed_bindings:
        changed_profile = RemoteProfile.parse(profile.as_dict() | changes)
        changed = RemoteCredentials(changed_profile)
        assert changed.path != first_workspace.path, changes
        assert asyncio.run(changed.status())["state"] == "sign_in_required", changes


def test_global_credentials_fall_back_to_private_home_data_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    home = tmp_path / "fake-home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    profile = oauth_profile()
    credential = RemoteCredentials(profile)
    assert credential.path == home / ".local" / "share" / "ngn" / "auth" / "a2a" / f"{profile.fingerprint}.json"
    credential.save("home-access", 4102444800.0)
    assert asyncio.run(RemoteCredentials(profile).token()) == "home-access"


def test_relative_xdg_data_home_is_rejected_before_accessing_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", "relative-user-data")
    with pytest.raises(ProtectedStoreError, match="absolute path"):
        RemoteCredentials(oauth_profile())


def test_authentication_status_is_secret_free_and_classifies_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        oauth = isolated_credentials(tmp_path, oauth_profile(), "oauth")
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

        expiring = isolated_credentials(tmp_path, oauth_profile(), "expiring")
        seed_grant(expiring, expires_at=1.0)
        assert (await expiring.status())["state"] == "signed_in"
        assert (await expiring.status())["refreshable"] is True
        assert (await expiring.status())["expires_at"] == 1.0
        another_workspace = isolated_credentials(tmp_path, oauth_profile(), "other-workspace")
        isolated = await another_workspace.status()
        assert isolated["state"] == "sign_in_required"
        assert isolated["refreshable"] is False
        no_refresh = isolated_credentials(tmp_path, oauth_profile(), "no-refresh")
        no_refresh.save("expires-soon", 4102444800.0)
        no_refresh.path.write_text(json.dumps({"access_token": "expired", "expires_at": 1.0}), encoding="utf-8")
        no_refresh.path.chmod(0o600)
        assert (await no_refresh.status())["state"] == "sign_in_required"
        assert (await no_refresh.status())["refreshable"] is False

        env_profile = isolated_credentials(tmp_path, oauth_profile(token_env="NGN_REMOTE_TEST_TOKEN"), "env")
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

        no_auth = isolated_credentials(tmp_path, RemoteProfile(url="https://agent.example.test"), "plain")
        assert (await no_auth.status())["state"] == "not_required"
        host_setup = isolated_credentials(
            tmp_path, RemoteProfile(url="https://agent.example.test", issuer=ISSUER), "host-setup"
        )
        assert (await host_setup.status())["state"] == "host_setup_required"

        unsafe = isolated_credentials(tmp_path, oauth_profile(), "unsafe")
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
        credentials = [isolated_credentials(tmp_path, profile, "shared-workspace") for _ in range(10)]
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


def test_global_refresh_lock_rotates_once_across_workspace_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[str] = []
    request_lock = threading.Lock()

    class TokenHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            form = parse_qs(self.rfile.read(length).decode("ascii"))
            with request_lock:
                requests.append(form["refresh_token"][0])
                index = len(requests)
            time.sleep(0.2)
            body = json.dumps(
                {
                    "access_token": f"cross-process-access-{index}",
                    "refresh_token": f"cross-process-refresh-{index}",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    server = ThreadingHTTPServer(("127.0.0.1", 0), TokenHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    issuer = f"http://127.0.0.1:{server.server_address[1]}"
    profile = RemoteProfile(url=issuer, issuer=issuer, client_id="fixture-public-client", loopback_http=True)
    data_home = tmp_path / "shared-xdg-data"
    data_home.mkdir(mode=0o700)
    (data_home / "ngn").mkdir(mode=0o700)
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    home = tmp_path / "shared-home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    credentials = RemoteCredentials(profile)
    # Establish the shared path through the same protected API used in production,
    # before changing only the fixture expiry to trigger concurrent rotation.
    credentials.save("initial-access", time.time() + 3600)
    seed_grant(credentials, expires_at=1.0, token_endpoint=f"{issuer}/token")

    source = str(Path(__file__).resolve().parents[3] / "src")
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [source, env.get("PYTHONPATH", "")]))
    workspace_a, workspace_b = tmp_path / "workspace-a", tmp_path / "workspace-b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    child = """
import asyncio
import pathlib
import sys
import time
from nagents.harness.remote.credentials import RemoteCredentials
from nagents.harness.remote.profiles import RemoteProfile

issuer = sys.argv[1]
ready = pathlib.Path(sys.argv[2])
barrier = pathlib.Path(sys.argv[3])
profile = RemoteProfile(url=issuer, issuer=issuer, client_id="fixture-public-client", loopback_http=True)
credentials = RemoteCredentials(profile)
ready.touch()
deadline = time.monotonic() + 10
while not barrier.exists() and time.monotonic() < deadline:
    time.sleep(0.01)
if not barrier.exists():
    raise TimeoutError("test barrier was not released")
print(asyncio.run(credentials.token()))
"""
    barrier = tmp_path / "start-refresh"
    ready_paths = (workspace_a / "ready", workspace_b / "ready")
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", child, issuer, str(ready), str(barrier)],
            cwd=workspace,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for workspace, ready in zip((workspace_a, workspace_b), ready_paths, strict=True)
    ]
    try:
        deadline = time.monotonic() + 10
        while not all(path.exists() for path in ready_paths) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert all(path.exists() for path in ready_paths), (
            "both workspace processes must reach the shared refresh barrier"
        )
        barrier.touch()
        results = [process.communicate(timeout=12) for process in processes]
        assert all(process.returncode == 0 for process in processes), results
        assert sorted(stdout.strip() for stdout, _ in results) == [
            "cross-process-access-1",
            "cross-process-access-1",
        ]
        assert requests == ["refresh-before-refresh"]
        persisted = json.loads(RemoteCredentials(profile).path.read_text(encoding="utf-8"))
        assert persisted["refresh_token"] == "cross-process-refresh-1"
        assert persisted.get("refresh_pending") is not True
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)


def test_uncertain_response_and_crash_marker_never_replay_refresh_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        seen: list[str] = []
        install_transport(monkeypatch, RotatingTransport(seen, fail=True))
        credentials = isolated_credentials(tmp_path, oauth_profile(), "uncertain")
        seed_grant(credentials, expires_at=1.0)

        with pytest.raises(RemoteCredentialError) as first:
            await credentials.token()
        assert first.value.code == "A2A_AUTH_REFRESH_UNCERTAIN"
        assert "private response body" not in str(first.value)
        stored = json.loads(credentials.path.read_text(encoding="utf-8"))
        assert stored["refresh_pending"] is True

        with pytest.raises(RemoteCredentialError) as second:
            await isolated_credentials(tmp_path, oauth_profile(), "uncertain").token()
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
        credentials = isolated_credentials(tmp_path, oauth_profile(), "write-failure")
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
            await isolated_credentials(tmp_path, oauth_profile(), "write-failure").token()
        assert second.value.code == "A2A_AUTH_REFRESH_UNCERTAIN"
        assert seen == ["refresh-before-refresh"]

    asyncio.run(scenario())


def test_rejected_refresh_requires_signin_and_clears_the_one_use_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        transport = RejectedTransport()
        install_transport(monkeypatch, transport)
        credentials = isolated_credentials(tmp_path, oauth_profile(), "rejected")
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
        credentials = isolated_credentials(tmp_path, oauth_profile(), "login-race")
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
        credentials = isolated_credentials(tmp_path, oauth_profile(), "hardlink-lock")
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


def test_refresh_lock_reports_safe_operation_and_errno(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import fcntl

    credentials = isolated_credentials(tmp_path, oauth_profile(), "private-account")
    seed_grant(credentials, expires_at=1.0)

    def unavailable(descriptor: int, operation: int) -> None:
        del descriptor, operation
        raise OSError(errno.EIO, "sensitive-token-and-private-path")

    monkeypatch.setattr(fcntl, "flock", unavailable)
    with pytest.raises(RemoteCredentialError) as error:
        asyncio.run(credentials.token())
    assert error.value.code == "A2A_AUTH_STORAGE"
    assert "phase=acquire_lock error_type=OSError errno=5" in caplog.text
    for private in ("sensitive-token", "private-account", credentials.profile.fingerprint, "access-before-refresh"):
        assert private not in caplog.text


def test_refresh_lock_avoids_create_or_open_race_and_keeps_one_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_open = os.open
    flags_seen: list[int] = []

    def guarded_open(path: str, flags: int, mode: int = 0o777, **kwargs: int) -> int:
        if path.endswith(".refresh.lock"):
            flags_seen.append(flags)
            if flags & os.O_CREAT and not flags & os.O_EXCL:
                # macOS can return ENOENT when simultaneous openat callers
                # create-or-open one new lock. Reject that syscall shape.
                raise FileNotFoundError(errno.ENOENT, "simulated create-or-open race")
        return original_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(os, "open", guarded_open)
    credentials = isolated_credentials(tmp_path, oauth_profile(), "shared-lock")

    async def scenario() -> None:
        async with credentials._refresh_lock():
            lock = credentials.path.parent / f".{credentials.profile.fingerprint}.refresh.lock"
            first_inode = lock.stat().st_ino
        async with credentials._refresh_lock():
            assert lock.stat().st_ino == first_inode
            assert lock.stat().st_mode & 0o777 == 0o600

    asyncio.run(scenario())
    assert len(flags_seen) == 3
    assert all(flags & os.O_NOFOLLOW for flags in flags_seen)
    assert flags_seen[0] & os.O_EXCL and flags_seen[1] & os.O_EXCL
    assert not flags_seen[2] & os.O_CREAT


def test_refresh_lock_reports_unsafe_file_without_private_details(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    credentials = isolated_credentials(tmp_path, oauth_profile(), "private-account")
    seed_grant(credentials, expires_at=1.0)
    lock = credentials.path.parent / f".{credentials.profile.fingerprint}.refresh.lock"
    lock.write_text("private-lock-content")
    lock.chmod(0o644)
    with pytest.raises(RemoteCredentialError) as error:
        asyncio.run(credentials.token())
    assert error.value.code == "A2A_AUTH_STORAGE"
    assert "phase=validate_lock error_type=ProtectedStoreError errno=unavailable" in caplog.text
    for private in ("private-account", "private-lock-content", credentials.profile.fingerprint):
        assert private not in caplog.text
