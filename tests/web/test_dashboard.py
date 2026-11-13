"""The dashboard shell is read-only and keeps the local HTTP boundary."""

import asyncio
import signal
from pathlib import Path
from unittest.mock import AsyncMock
from unittest.mock import Mock

import httpx
import pytest
import uvicorn

from nagents.web.dashboard import DashboardHost
from nagents.web.dashboard import create_dashboard_app
from nagents.web.dashboard import open_dashboard_browser
from tests.support.web import client_app


@pytest.fixture
def assets(tmp_path: Path) -> Path:
    directory = tmp_path / "static"
    directory.mkdir()
    (directory / "assets").mkdir()
    (directory / "index.html").write_text("<html>dashboard shell</html>")
    return directory


@pytest.mark.asyncio
async def test_dashboard_routes_and_security(tmp_path: Path, assets: Path) -> None:
    app = create_dashboard_app(tmp_path, assets, "127.0.0.1:8765")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765") as client:
        page = await client.get("/dashboard")
        assert page.status_code == 200
        assert "dashboard shell" in page.text
        assert page.headers["x-frame-options"] == "DENY"
        assert (await client.get("/api/dashboard")).status_code == 403
        bootstrap = (await client.get("/api/bootstrap")).json()
        assert bootstrap["capabilities"] == {"chat": False}
        assert (await client.get("/")).status_code == 200
        headers = {"X-Ngn-Token": bootstrap["token"]}
        summary = await client.get("/api/dashboard", headers=headers)
        assert summary.json() == {"workspace": str(tmp_path), "status": "not_connected"}
        assert (
            await client.post("/api/messages", json={}, headers={**headers, "Origin": "http://127.0.0.1:8765"})
        ).status_code == 404
        assert (
            await client.get("/api/dashboard", headers={**headers, "Origin": "https://example.com"})
        ).status_code == 403
        assert (await client.get("/dashboard", headers={"Host": "evil.example"})).status_code == 403


@pytest.mark.asyncio
async def test_existing_web_host_serves_dashboard(tmp_path: Path) -> None:
    async with client_app(tmp_path) as (_, client, headers, harnesses):
        assert (await client.get("/dashboard")).status_code == 200
        assert (await client.get("/api/bootstrap")).json()["capabilities"] == {"chat": True}
        assert (await client.get("/api/dashboard")).status_code == 403
        response = await client.get("/api/dashboard", headers=headers)
        assert response.json() == {"workspace": str(tmp_path), "status": "not_connected"}
        assert len(harnesses) == 1


@pytest.mark.asyncio
async def test_dashboard_host_reuses_server_and_closes(
    tmp_path: Path, assets: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("nagents.web.built_assets", lambda: assets)
    host = DashboardHost(tmp_path)
    try:
        url = await host.start()
        assert url.startswith("http://127.0.0.1:")
        assert url.endswith("/dashboard")
        assert await host.start() == url
        async with httpx.AsyncClient() as client:
            assert (await client.get(url)).status_code == 200
    finally:
        await host.close()
    assert host.url == ""
    await host.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_embedded_server_leaves_process_signals_untouched(
    tmp_path: Path, assets: Path, monkeypatch: pytest.MonkeyPatch, legacy: bool
) -> None:
    monkeypatch.setattr("nagents.web.built_assets", lambda: assets)
    if legacy:

        async def legacy_serve(self: uvicorn.Server, sockets: list[object]) -> None:
            hook = "install_signal_handlers"
            getattr(self, hook)()
            self.started = True
            while not self.should_exit:
                await asyncio.sleep(0)

        monkeypatch.setattr(uvicorn.Server, "serve", legacy_serve)
    original = signal.getsignal(signal.SIGINT)
    changed = Mock(side_effect=AssertionError("embedded server changed signals"))
    with monkeypatch.context() as signals:
        signals.setattr(signal, "signal", changed)
        host = DashboardHost(tmp_path)
        try:
            await host.start()
            assert signal.getsignal(signal.SIGINT) is original
        finally:
            await host.close()
        changed.assert_not_called()


@pytest.mark.asyncio
async def test_browser_launcher_uses_default_desktop_without_terminal_io(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY", "WAYLAND_DISPLAY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("BROWSER", "lynx")
    monkeypatch.setattr("nagents.web.dashboard.sys.platform", "linux")
    process = Mock(returncode=0, wait=AsyncMock(return_value=0))
    launch = AsyncMock(return_value=process)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    assert await open_dashboard_browser("http://127.0.0.1:8765/dashboard")
    launch.assert_awaited_once_with(
        "/usr/bin/gio",
        "open",
        "http://127.0.0.1:8765/dashboard",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )


@pytest.mark.asyncio
async def test_browser_launcher_skips_headless_and_remote_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nagents.web.dashboard.sys.platform", "linux")
    for name in ("DISPLAY", "WAYLAND_DISPLAY", "SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"):
        monkeypatch.delenv(name, raising=False)
    launch = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    assert not await open_dashboard_browser("http://127.0.0.1:8765/dashboard")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("SSH_CONNECTION", "remote")
    assert not await open_dashboard_browser("http://127.0.0.1:8765/dashboard")
    launch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_browser_launcher_reaps_hung_process(monkeypatch: pytest.MonkeyPatch, cancel: bool) -> None:
    for name in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("nagents.web.dashboard.sys.platform", "darwin")
    started = asyncio.Event()

    async def wait() -> int:
        started.set()
        if cancel:
            await asyncio.Event().wait()
        raise TimeoutError

    async def reap() -> int:
        if process.returncode is not None:
            return 0
        return await wait()

    process = Mock(returncode=None)
    process.wait = AsyncMock(side_effect=reap)
    process.kill.side_effect = lambda: setattr(process, "returncode", -9)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    task = asyncio.create_task(open_dashboard_browser("http://127.0.0.1:8765/dashboard"))
    if cancel:
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert not await task
    process.kill.assert_called_once()
    assert process.wait.await_count == 2
