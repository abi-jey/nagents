"""The dashboard shell is read-only and keeps the local HTTP boundary."""

from pathlib import Path

import pytest

from tests.support.web import client_app


@pytest.mark.asyncio
async def test_existing_web_host_serves_dashboard(tmp_path: Path) -> None:
    async with client_app(tmp_path) as (_, client, headers, harnesses):
        assert (await client.get("/dashboard")).status_code == 200
        assert (await client.get("/api/bootstrap")).json()["capabilities"] == {"chat": True}
        assert (await client.get("/api/dashboard")).status_code == 403
        response = await client.get("/api/dashboard", headers=headers)
        assert response.json() == {"workspace": str(tmp_path), "status": "not_connected"}
        assert len(harnesses) == 1
