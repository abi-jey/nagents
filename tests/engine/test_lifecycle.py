"""The engine uses the real web lifespan, but owns its own transport and process."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import socket
import stat
import sys
from contextlib import asynccontextmanager
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING
from typing import cast
from unittest.mock import AsyncMock
from unittest.mock import Mock
from urllib.parse import urlsplit

import aiohttp
import httpx
import pytest

from nagents.engine.api import Backend
from nagents.engine.lifecycle import create_engine_app
from nagents.engine.registration import LOCK_NAME
from nagents.engine.registration import REGISTRATION_NAME
from nagents.engine.registration import EngineStateError
from nagents.engine.registration import Registration
from nagents.engine.registration import own_state
from nagents.harness import Harness
from nagents.harness.config import HarnessConfig
from nagents.harness.providers.model_bindings import ModelBinding
from nagents.harness.providers.registry import ProviderProfile
from nagents.harness.providers.registry import ProviderRegistry
from nagents.harness.providers.registry import ScopedProviderRegistryStore
from nagents.protocol import EVENTS_SUBPROTOCOL
from nagents.protocol import PROTOCOL_REVISION
from nagents.protocol import PROTOCOL_VERSION
from nagents.protocol.models import EngineConfig
from nagents.protocol.models import EngineInfo
from tests.support.hang_guard import HANG_GUARD

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from collections.abc import Callable
    from collections.abc import Sequence

    from nagents.web.execution.service import WebState

pytestmark = pytest.mark.requires_posix
INFO = "/api/v1/info"
EVENTS = "/api/v1/events?after=0"


def test_headless_app_reuses_one_web_lifespan_without_browser_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_assets() -> Path:
        raise AssertionError("The headless app must not look for a frontend build")

    monkeypatch.setattr("nagents.web.http.app.built_assets", no_assets)
    harnesses: list[Harness] = []
    states: list[WebState] = []
    backend = Mock(spec=Backend)
    backend.info = AsyncMock(
        return_value=EngineInfo(
            protocol_version=PROTOCOL_VERSION, protocol_revision=PROTOCOL_REVISION, version="test", capabilities=[]
        )
    )

    def harness_factory(config: HarnessConfig) -> Harness:
        harness = Harness(config)
        harnesses.append(harness)
        return harness

    def backend_factory(get_state: Callable[[], WebState]) -> Backend:
        state = get_state()
        assert state.harness is harnesses[0] and state.selected_session_id
        states.append(state)
        return cast("Backend", backend)

    async def check() -> None:
        app = create_engine_app(
            HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "engine", demo=True),
            port=32123,
            token="test-only-engine-token",
            harness_factory=harness_factory,
            backend_factory=backend_factory,
        )
        base = "http://127.0.0.1:32123"
        auth = {"Authorization": "Bearer test-only-engine-token"}
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 42001))
        assert not harnesses and not states
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=transport, base_url=base) as http:
            assert len(states) == len(harnesses) == 1 and app.state.web is states[0]
            assert (await http.get(INFO)).status_code == 401
            assert (await http.get(INFO, headers=[("Authorization", auth["Authorization"])] * 2)).status_code == 401
            backend.info.assert_not_awaited()
            assert (await http.get(INFO, headers=auth)).json()["version"] == "test"
            for path in ("/", "/api/bootstrap", "/api/sessions", "/assets/index.js", "/openapi.json", "/docs"):
                assert (await http.get(path, headers=auth)).status_code == 404
        assert harnesses[0]._closed

    asyncio.run(check())


@asynccontextmanager
async def _launch(
    workspace: Path,
    directory: Path | None = None,
    *,
    port: int = 0,
    stdio: bool = False,
    piped: bool = True,
    demo: bool = True,
    flags: Sequence[str] = (),
) -> AsyncIterator[asyncio.subprocess.Process]:
    entry_point = Path(sys.executable).with_name("nagents")
    command = [str(entry_point)] if entry_point.is_file() else [sys.executable, "-m", "nagents.engine.cli"]
    command += [
        "engine",
        "--workspace",
        str(workspace),
        "--port",
        str(port),
    ]
    if directory is not None:
        command += ["--state-dir", str(directory)]
    if stdio:
        command.append("--stdio")
    if demo:
        command.append("--demo")
    command.extend(flags)
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE if stdio and piped else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        yield process
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
        await asyncio.wait_for(process.communicate(), HANG_GUARD)


def _registration(raw: bytes, pid: int) -> Registration:
    fields = json.loads(raw)
    assert set(fields) == {"url", "token", "pid", "protocol"}
    entry = Registration(**fields)
    url = urlsplit(entry.url)
    assert url.scheme == "http" and url.hostname == "127.0.0.1" and url.port
    assert re.fullmatch(r"[a-zA-Z0-9_-]{43}", entry.token)
    assert entry.pid == pid and entry.protocol == PROTOCOL_VERSION
    return entry


async def _registered(path: Path, process: asyncio.subprocess.Process) -> Registration:
    async with asyncio.timeout(HANG_GUARD):
        while process.returncode is None:
            try:
                raw = path.read_bytes()
            except FileNotFoundError:
                pass
            else:
                # A crashed predecessor's registration can still be there
                # while the replacement is importing modules.
                if json.loads(raw)["pid"] == process.pid:
                    return _registration(raw, process.pid)
            await asyncio.sleep(0.01)
    assert process.stderr is not None
    pytest.fail(f"The engine exited before publishing registration: {(await process.stderr.read()).decode()}")


async def _terminated(process: asyncio.subprocess.Process, entry: Registration) -> None:
    output, logs = await asyncio.wait_for(process.communicate(), HANG_GUARD)
    if entry.token.encode() in logs or entry.token.encode() in output:
        pytest.fail("Engine diagnostics leaked the private bearer token.")
    assert process.returncode == 0, logs.decode()
    assert not output
    assert b"nagents engine stopped:" in logs


def _workspace_directory(base: Path, workspace: Path) -> Path:
    return base / "engine" / "workspaces" / hashlib.sha256(os.fsencode(workspace.resolve())).hexdigest()


def test_process_http_and_websocket_require_the_same_private_bearer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy = tmp_path / "legacy-state"
    monkeypatch.setenv("NGN_DATA_DIR", str(legacy))

    async def check() -> None:
        directory = _workspace_directory(legacy, tmp_path)
        async with _launch(tmp_path) as process:
            path = directory / REGISTRATION_NAME
            entry = await _registered(path, process)
            assert stat.S_IMODE(directory.stat().st_mode) == 0o700
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
            auth = {"Authorization": f"Bearer {entry.token}"}
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=HANG_GUARD)) as http:
                async with http.get(entry.url + INFO, headers=auth) as response:
                    assert response.status == 200
                    payload = await response.json()
                    assert "token" not in payload
                    info = EngineInfo.model_validate(payload)
                    assert info.protocol_version == PROTOCOL_VERSION and info.protocol_revision == PROTOCOL_REVISION
                    assert response.headers["Cache-Control"] == "no-store"
                async with http.get(entry.url + INFO, params={"token": entry.token}) as response:
                    assert response.status == 401
                    assert response.headers["WWW-Authenticate"] == "Bearer"
                async with http.get(entry.url + INFO, headers={"Authorization": "Bearer stale"}) as response:
                    assert response.status == 401
                for added in ({"Host": "untrusted.test"}, {"Origin": entry.url}, {"Sec-Fetch-Site": "same-origin"}):
                    async with http.get(entry.url + INFO, headers=auth | added) as response:
                        assert response.status == 403
                async with http.get(entry.url + INFO, headers=auth | {"X-Forwarded-For": "192.0.2.100"}) as response:
                    assert response.status == 200  # Access control never trusts proxy headers.
                async with http.get(entry.url + "/api/bootstrap", headers=auth) as response:
                    assert response.status == 404

                for headers in ({}, auth | {"Host": "untrusted.test"}, auth | {"Origin": entry.url}):
                    with pytest.raises(aiohttp.WSServerHandshakeError) as rejected:
                        await http.ws_connect(entry.url + EVENTS, headers=headers, protocols=[EVENTS_SUBPROTOCOL])
                    assert rejected.value.status == 403
                with pytest.raises(aiohttp.WSServerHandshakeError) as query_auth:
                    await http.ws_connect(entry.url + EVENTS + f"&token={entry.token}", protocols=[EVENTS_SUBPROTOCOL])
                assert query_auth.value.status == 403
                async with http.ws_connect(entry.url + EVENTS, headers=auth, protocols=[EVENTS_SUBPROTOCOL]) as events:
                    assert events.protocol == EVENTS_SUBPROTOCOL
            process.terminate()
            await _terminated(process, entry)
            assert not path.exists()
            assert stat.S_IMODE((directory / LOCK_NAME).stat().st_mode) == 0o600
            assert list(legacy.iterdir()) == [legacy / "engine"]

    asyncio.run(check())


def test_default_discovery_uses_distinct_canonical_workspaces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = tmp_path / "legacy-state"
    first = tmp_path / "first-café"
    second = tmp_path / "second"
    alias = tmp_path / "first-alias"
    first.mkdir()
    second.mkdir()
    alias.symlink_to(first, target_is_directory=True)
    monkeypatch.setenv("NGN_DATA_DIR", str(base))

    async def check() -> None:
        async with _launch(alias) as a, _launch(second) as b:
            entries = await asyncio.gather(
                _registered(_workspace_directory(base, first) / REGISTRATION_NAME, a),
                _registered(_workspace_directory(base, second) / REGISTRATION_NAME, b),
            )
            assert entries[0].url != entries[1].url
            assert entries[0].token != entries[1].token
            a.terminate()
            b.terminate()
            await asyncio.gather(_terminated(a, entries[0]), _terminated(b, entries[1]))

    asyncio.run(check())


@pytest.mark.parametrize("model", ["", "launch-model"])
def test_owned_engine_honors_launch_provider_model_and_read_only(tmp_path: Path, model: str) -> None:
    store = ScopedProviderRegistryStore(tmp_path)
    provider = ProviderProfile(kind="openai", auth="api-key")
    store.global_store.save(
        ProviderRegistry(active="configured", providers={"configured": provider, "selected": provider}),
        expected="0" * 64,
    )
    store.model_store("workspace").save_binding(ModelBinding("configured", "configured-model"))
    flags = ["--read-only", "--provider-id", "selected", "--no-wire-log"]
    if model:
        flags += ["--model", model]

    async def check() -> None:
        directory = tmp_path / "engine"
        async with _launch(tmp_path, directory, demo=False, flags=flags) as process:
            entry = await _registered(directory / REGISTRATION_NAME, process)
            async with (
                aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=HANG_GUARD)) as http,
                http.get(entry.url + "/api/v1/config", headers={"Authorization": f"Bearer {entry.token}"}) as response,
            ):
                assert response.status == 200
                config = EngineConfig.model_validate(await response.json())
            assert config.read_only and config.provider == "selected"
            assert config.model == (model or "configured-model")
            process.terminate()
            await _terminated(process, entry)

    asyncio.run(check())


def test_owner_excludes_competitors_and_recovers_crashed_registration(tmp_path: Path) -> None:
    async def check() -> None:
        directory = tmp_path / "engine"
        path = directory / REGISTRATION_NAME
        async with _launch(tmp_path, directory) as owner:
            first = await _registered(path, owner)
            lock_inode = (directory / LOCK_NAME).stat().st_ino
            async with _launch(tmp_path, directory) as contender:
                output, logs = await asyncio.wait_for(contender.communicate(), HANG_GUARD)
                assert not output and contender.returncode == 2
                assert b"already owns this state directory" in logs
                assert _registration(path.read_bytes(), owner.pid) == first
            owner.kill()
            await asyncio.wait_for(owner.communicate(), HANG_GUARD)
            assert path.is_file()  # SIGKILL cannot clean up, but it releases the lock.
        async with _launch(tmp_path, directory) as replacement:
            second = await _registered(path, replacement)
            assert first.token != second.token
            assert (directory / LOCK_NAME).stat().st_ino == lock_inode
            async with (
                aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=HANG_GUARD)) as http,
                http.get(second.url + INFO, headers={"Authorization": f"Bearer {first.token}"}) as response,
            ):
                assert response.status == 401
            replacement.terminate()
            await _terminated(replacement, second)
            assert not path.exists()

    asyncio.run(check())


def test_bind_failure_does_not_publish_registration(tmp_path: Path) -> None:
    async def check() -> None:
        directory = tmp_path / "engine"
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            async with _launch(tmp_path, directory, port=listener.getsockname()[1]) as process:
                output, logs = await asyncio.wait_for(process.communicate(), HANG_GUARD)
                assert process.returncode == 2, logs.decode()
                assert not output and not (directory / REGISTRATION_NAME).exists()
        with own_state(directory):
            assert not (directory / REGISTRATION_NAME).exists()

    asyncio.run(check())


def test_stdio_readiness_is_private_and_parent_eof_shuts_down(tmp_path: Path) -> None:
    async def check() -> None:
        directory = tmp_path / "private-engine"
        async with _launch(tmp_path, directory, stdio=True) as child:
            assert child.stdout is not None and child.stdin is not None
            line = await asyncio.wait_for(child.stdout.readline(), HANG_GUARD)
            if not line:
                assert child.stderr is not None
                pytest.fail(f"The private engine did not publish readiness: {(await child.stderr.read()).decode()}")
            entry = _registration(line, child.pid)
            assert not (directory / REGISTRATION_NAME).exists()
            async with _launch(tmp_path, directory) as contender:
                _, logs = await asyncio.wait_for(contender.communicate(), HANG_GUARD)
                assert contender.returncode == 2 and b"already owns this state directory" in logs
            async with (
                aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=HANG_GUARD)) as http,
                http.get(entry.url + "/api/v1/config", headers={"Authorization": f"Bearer {entry.token}"}) as response,
            ):
                assert response.status == 200
                assert (await response.json())["workspace"] == str(tmp_path)
            child.stdin.close()
            await _terminated(child, entry)
        assert not (directory / REGISTRATION_NAME).exists()

    asyncio.run(check())


def test_stdio_eof_during_startup_does_not_publish_readiness(tmp_path: Path) -> None:
    async def check() -> None:
        directory = tmp_path / "private-engine"
        async with _launch(tmp_path, directory, stdio=True) as child:
            assert child.stdin is not None
            child.stdin.close()
            output, logs = await asyncio.wait_for(child.communicate(), HANG_GUARD)
            assert child.returncode == 0, logs.decode()
            assert not output and not (directory / REGISTRATION_NAME).exists()

    asyncio.run(check())


def test_stdio_refuses_non_pipe_before_owning_state(tmp_path: Path) -> None:
    async def check() -> None:
        directory = tmp_path / "not-opened"
        async with _launch(tmp_path, directory, stdio=True, piped=False) as child:
            output, logs = await asyncio.wait_for(child.communicate(), HANG_GUARD)
            assert not output and child.returncode == 2
            assert b"--stdio requires private stdin and stdout pipes" in logs
        assert not directory.exists()

    asyncio.run(check())


def test_owner_refuses_an_unsafe_directory_or_registration_symlink(tmp_path: Path) -> None:
    directory = tmp_path / "engine"
    directory.mkdir()
    directory.chmod(0o755)
    with pytest.raises(EngineStateError, match="unsafe"), own_state(directory):
        pytest.fail("A public state directory was accepted")
    assert not (directory / LOCK_NAME).exists()
    directory.chmod(0o700)
    file = tmp_path / "external"
    file.write_text("Do not overwrite", encoding="utf-8")
    (directory / REGISTRATION_NAME).symlink_to(file)
    with pytest.raises(EngineStateError), own_state(directory):
        pytest.fail("A registration symlink was accepted")
    assert file.read_text(encoding="utf-8") == "Do not overwrite"
