"""Live settings reads keep a coherent snapshot of assistant Markdown fields."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from typing import cast

from nagents.harness.agents.documents import agent_document_store
from tests.support.web import client_app

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from nagents.web.voice.preferences import Scope
    from nagents.web.voice.preferences import VoicePreferenceSnapshot


def test_global_live_read_rejects_markdown_change_between_voice_and_prompt_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            settings = app.state.live_settings
            original_snapshot = settings.voice.snapshot
            document = agent_document_store(settings.providers, "global")
            changed = False

            def snapshot(scope: Scope = "workspace") -> VoicePreferenceSnapshot:
                nonlocal changed
                current = cast("VoicePreferenceSnapshot", original_snapshot(scope))
                if scope == "global" and not changed:
                    changed = True
                    document.patch_live(
                        "assistant",
                        {"prompt": "RACING_PROMPT", "voice": "cedar"},
                        expected=current.global_revision,
                    )
                return current

            monkeypatch.setattr(settings.voice, "snapshot", snapshot)
            response = await client.get("/api/live/settings?scope=global", headers=headers)

            assert changed
            assert response.status_code == 409, response.text
            assert "Markdown settings changed" in response.json()["detail"]

    asyncio.run(scenario())


def test_global_live_read_ignores_malformed_workspace_assistant_document(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            workspace_document = agent_document_store(app.state.live_settings.providers, "workspace")
            path = workspace_document.load("assistant").path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("---\nlive: [broken\n---\n", encoding="utf-8")

            response = await client.get("/api/live/settings?scope=global", headers=headers)

            assert response.status_code == 200, response.text
            assert response.json()["scope"] == "global"

    asyncio.run(scenario())
