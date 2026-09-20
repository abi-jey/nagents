"""Read-only context accounting endpoint for the ngn web host."""

import asyncio
from pathlib import Path

from tests.support.web import client_app


def test_session_context_endpoint_reports_a_read_only_breakdown(tmp_path: Path) -> None:
    async def check() -> None:
        async with client_app(tmp_path) as (_, client, headers, _):
            snapshot = (await client.get("/api/sessions", headers=headers)).json()
            session_id = snapshot["session_id"]

            # Authentication follows the existing token boundary.
            assert (await client.get(f"/api/sessions/{session_id}/context")).status_code == 403

            response = await client.get(f"/api/sessions/{session_id}/context", headers=headers)
            assert response.status_code == 200
            data = response.json()
            keys = [component["key"] for component in data["components"]]
            assert "tools" in keys
            assert any(component["key"] == "tools" and component["tokens"] > 0 for component in data["components"])
            assert data["total_tokens"] == sum(component["tokens"] for component in data["components"])
            assert isinstance(data["context_window"], int) and data["context_window"] > 0
            assert data["remaining_tokens"] == data["context_window"] - data["total_tokens"]
            assert data["provider"] and data["model"]
            # Read-only: a second call returns the same session snapshot.
            again = (await client.get("/api/sessions", headers=headers)).json()
            assert again["session_id"] == session_id

    asyncio.run(check())


def test_session_context_unknown_session_is_not_found(tmp_path: Path) -> None:
    async def check() -> None:
        async with client_app(tmp_path) as (_, client, headers, _):
            response = await client.get("/api/sessions/ngn-missing/context", headers=headers)
            assert response.status_code == 404

    asyncio.run(check())
