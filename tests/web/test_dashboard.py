"""The dashboard shell is read-only and keeps the local HTTP boundary."""

from pathlib import Path

import httpx
import pytest

from nagents.web.dashboard import DashboardHost
from nagents.web.dashboard import create_dashboard_app
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
