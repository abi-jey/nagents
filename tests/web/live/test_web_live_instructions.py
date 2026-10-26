"""Voice prompt HTTP edits use shared assistant Markdown documents."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from typing import cast

from nagents.harness.agents.documents import agent_document_store
from tests.support.web import client_app
from tests.test_agent_live_profiles import configuration

if TYPE_CHECKING:
    from pathlib import Path

    import httpx
    import pytest


async def get_prompt(
    client: httpx.AsyncClient, headers: dict[str, str], scope: str, agent: str = ""
) -> dict[str, object]:
    query = f"scope={scope}" + (f"&agent={agent}" if agent else "")
    response = await client.get(f"/api/live/instructions?{query}", headers=headers)
    assert response.status_code == 200, response.text
    result = cast("dict[str, object]", response.json())
    assert "revision" in result and "present" in result and "text" in result
    assert "mode" not in result
    return result


async def save_prompt(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    current: dict[str, object],
    scope: str,
    *,
    agent: str = "",
    prompt: str | None = None,
) -> dict[str, object]:
    body: dict[str, object] = {"scope": scope, "revision": current["revision"]}
    if agent:
        body["agent"] = agent
    if prompt is not None:
        body["prompt"] = prompt
    response = await client.put("/api/live/instructions", headers=headers, json=body)
    assert response.status_code == 200, response.text
    result = cast("dict[str, object]", response.json())
    assert "mode" not in result
    return result


def test_main_and_voice_http_prompts_share_one_global_markdown_file(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path, config=configuration(tmp_path)) as (app, client, headers, _):
            main = await client.get("/api/agent-settings?scope=global", headers=headers)
            assert main.status_code == 200, main.text
            saved_main = await client.put(
                "/api/agent-settings",
                headers=headers,
                json={
                    "scope": "global",
                    "agent": "assistant",
                    "revision": main.json()["revision"],
                    "preferences": {"instructions": "MAIN_PROMPT"},
                },
            )
            assert saved_main.status_code == 200, saved_main.text

            voice = await get_prompt(client, headers, "global")
            saved_voice = await save_prompt(client, headers, voice, "global", prompt="VOICE_PROMPT")
            store = agent_document_store(app.state.web.harness.provider_store, "global")
            saved = store.load("assistant")
            assert saved.preferences["prompt"] == "MAIN_PROMPT"
            live = saved.preferences["live"]
            assert isinstance(live, dict) and live["prompt"] == "VOICE_PROMPT"
            assert saved.preferences["auto_update"] is False
            assert str(saved.path) == saved_voice["preferences_path"]

    asyncio.run(scenario())


def test_voice_prompt_omission_inherits_and_empty_prompt_is_explicit(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path, config=configuration(tmp_path)) as (app, client, headers, _):
            current = await get_prompt(client, headers, "agent", "reader")
            assert current["present"] is False
            saved_empty = await save_prompt(client, headers, current, "agent", agent="reader", prompt="")
            assert saved_empty["present"] is True and saved_empty["text"] == ""
            store = agent_document_store(app.state.web.harness.provider_store, "agent")
            live = store.load("reader").preferences.get("live")
            assert isinstance(live, dict) and live.get("prompt") == ""

            inherited = await save_prompt(client, headers, saved_empty, "agent", agent="reader")
            assert inherited["present"] is False
            inherited_text = inherited["text"]
            assert isinstance(inherited_text, str) and inherited_text.endswith("Source voice")
            live = store.load("reader").preferences.get("live")
            assert not isinstance(live, dict) or "prompt" not in live

    asyncio.run(scenario())


def test_old_voice_files_and_legacy_payload_keys_are_ignored(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path, config=configuration(tmp_path)) as (app, client, headers, _):
            providers = app.state.web.harness.provider_store
            legacy = providers.model_store("workspace").path.parent / "live-instructions.md"
            legacy.parent.mkdir(parents=True, exist_ok=True)
            legacy.write_text("---\nmode: custom\n---\nLEGACY_PROMPT", encoding="utf-8")

            global_current = await get_prompt(client, headers, "global")
            global_saved = await save_prompt(client, headers, global_current, "global", prompt="GLOBAL_PROMPT")
            workspace = await get_prompt(client, headers, "workspace")
            assert workspace["present"] is False and workspace["text"] == "GLOBAL_PROMPT"

            for legacy_fields in ({"mode": "custom", "prompt": "ignored"}, {"text": "ignored"}):
                response = await client.put(
                    "/api/live/instructions",
                    headers=headers,
                    json={"scope": "workspace", "revision": workspace["revision"], **legacy_fields},
                )
                assert response.status_code == 422, response.text
            assert await get_prompt(client, headers, "workspace") == workspace
            assert global_saved["text"] == "GLOBAL_PROMPT"

    asyncio.run(scenario())


def test_voice_prompt_edits_compare_ancestor_revision_and_validate_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "private-instruction-key")

    async def scenario() -> None:
        async with client_app(tmp_path, config=configuration(tmp_path)) as (_, client, headers, _):
            assert (await client.get("/api/live/instructions?scope=global")).status_code == 403
            stale_workspace = await get_prompt(client, headers, "workspace")
            global_current = await get_prompt(client, headers, "global")
            await save_prompt(client, headers, global_current, "global", prompt="Changed ancestor")

            stale = await client.put(
                "/api/live/instructions",
                headers=headers,
                json={"scope": "workspace", "revision": stale_workspace["revision"], "prompt": "Stale"},
            )
            assert stale.status_code == 409, stale.text
            current = await get_prompt(client, headers, "workspace")
            assert current["text"] == "Changed ancestor"

            for prompt in ("private-instruction-key", "x" * 12001, "🌍" * 6001, "a\x00b"):
                rejected = await client.put(
                    "/api/live/instructions",
                    headers=headers,
                    json={"scope": "workspace", "revision": current["revision"], "prompt": prompt},
                )
                assert rejected.status_code == 422, rejected.text
            assert await get_prompt(client, headers, "workspace") == current

    asyncio.run(scenario())
