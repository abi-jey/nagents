"""Imported agent metadata and invocation policy at the web settings boundary."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from nagents.harness.config import AgentProfile
from nagents.harness.config import HarnessConfig
from tests.support.web import client_app
from tests.support.web import no_guarded_workspace_io as no_guarded_workspace_io

if TYPE_CHECKING:
    from pathlib import Path


def test_agent_metadata_diagnostics_and_server_side_selection(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = HarnessConfig(
            tmp_path,
            demo=True,
            data_dir=tmp_path / "data",
            profiles={
                "research": AgentProfile(
                    description="Find evidence",
                    source=".ngn/agents/research.md",
                    source_format="native",
                    instructions="Private prompt body",
                ),
                "child-only": AgentProfile(user_invocable=False),
            },
            agent_diagnostics=(".claude/agents/invalid.md: unsupported field hooks",),
        )
        async with client_app(tmp_path, config=config) as (_, client, headers, harnesses):
            snapshot = (await client.get("/api/settings", headers=headers)).json()
            assert {profile["name"] for profile in snapshot["profiles"]} == {"assistant", "research"}
            research = next(profile for profile in snapshot["profiles"] if profile["name"] == "research")
            assert research["description"] == "Find evidence"
            assert research["source"] == ".ngn/agents/research.md"
            assert research["source_format"] == "native"
            assert "Private prompt body" not in str(snapshot)
            assert snapshot["agent_diagnostics"] == list(config.agent_diagnostics)
            response = await client.post(
                "/api/settings",
                headers=headers,
                json={
                    "revision": snapshot["revision"],
                    "values": {**snapshot["values"], "agent": "child-only"},
                },
            )
            assert response.status_code == 422
            assert "unavailable agent profile" in response.text
            assert harnesses[0].config.agent == "assistant"

    asyncio.run(scenario())
