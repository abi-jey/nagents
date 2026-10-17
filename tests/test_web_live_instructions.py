"""Editable Markdown prompts, inherited defaults, and captured Live admission."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from typing import cast

import pytest
from fastapi import HTTPException

from nagents.harness.providers import ScopedProviderRegistryStore
from nagents.web.live import voice_instructions
from nagents.web.live_instructions import InstructionInput
from nagents.web.live_instructions import save_instructions
from nagents.web.live_settings import GlobalVoiceInput
from nagents.web.live_settings import WorkspaceVoiceInput
from nagents.web.voice_instruction_store import VoiceInstructionStore
from nagents.web.voice_instructions import default_voice_instructions
from nagents.web.voice_preferences import VoiceOverrides
from nagents.web.voice_preferences import VoicePreferences
from tests.support.web import client_app
from tests.test_agent_live_profiles import configuration
from tests.test_web_live_settings import providers
from tests.test_web_live_settings import settings

if TYPE_CHECKING:
    from pathlib import Path

    from nagents.web.voice_instruction_store import InstructionMode
    from nagents.web.voice_instruction_store import InstructionPreference
    from nagents.web.voice_instruction_store import InstructionScope


def test_agent_markdown_instructions_inherit_customize_reset_without_changing_voice_or_model(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path, config=configuration(tmp_path)) as (app, client, headers, harnesses):
            harness = harnesses[0]
            before_model = harness.resolved_model_binding("reader")
            original_voice = (await client.get("/api/live/settings?scope=agent&agent=reader", headers=headers)).json()

            async def save(scope: str, mode: str, text: str = "", agent: str = "") -> dict[str, object]:
                target = f"scope={scope}" + (f"&agent={agent}" if agent else "")
                current = (await client.get("/api/live/instructions?" + target, headers=headers)).json()
                response = await client.put(
                    "/api/live/instructions",
                    headers=headers,
                    json={
                        "scope": scope,
                        "agent": agent,
                        "revision": current["revision"],
                        "mode": mode,
                        "text": text,
                    },
                )
                assert response.status_code == 200, response.text
                return cast("dict[str, object]", response.json())

            first = (await client.get("/api/live/instructions?scope=agent&agent=reader", headers=headers)).json()
            assert first["mode"] == "custom" and not first["present"]
            assert first["text"].endswith("Source voice") and first["source"] == "definition"
            global_saved = await save("global", "custom", "# Shared voice\n\nSpeak plainly.\n")
            assert global_saved["text"] == "# Shared voice\n\nSpeak plainly.\n"
            assert await save("agent", "inherit", agent="reader")
            inherited = (await client.get("/api/live/instructions?scope=agent&agent=reader", headers=headers)).json()
            assert inherited["text"] == global_saved["text"] and inherited["source"] == "global"
            assert inherited["mode"] == "inherit" and inherited["present"]
            assert inherited["inherited"] == global_saved["text"]
            await save("workspace", "custom", "Workspace voice")
            current = (await client.get("/api/live/instructions?scope=agent&agent=reader", headers=headers)).json()
            assert current["text"] == "Workspace voice" and current["source"] == "workspace"
            customized = await save("agent", "custom", "", "reader")
            assert customized["text"] == "" and customized["mode"] == "custom"
            connection = await app.state.live_settings.scoped("workspace", agent="reader")
            assert voice_instructions(connection) == ""
            path = app.state.live_settings.instructions.path("agent", "reader")
            assert path.read_text() == "---\nmode: custom\n---\n"
            await save("agent", "inherit", agent="reader")
            assert path.read_text() == "---\nmode: inherit\n---\n"
            after_voice = (await client.get("/api/live/settings?scope=agent&agent=reader", headers=headers)).json()
            assert after_voice["effective"] == original_voice["effective"]
            assert harness.resolved_model_binding("reader") == before_model
            assert harness.config.profile("reader").live.selected()["instructions"] == "Source voice"

    asyncio.run(scenario())


def test_markdown_cas_includes_ancestors_and_active_calls_keep_their_prompt(tmp_path: Path) -> None:
    providers(tmp_path)

    async def scenario() -> None:
        owner = settings(tmp_path)
        await owner.load()
        before = await owner.connection()
        async with owner.admit(before.revision):
            captured = owner.admitted()
        global_before = await owner.scoped("global")
        await save_instructions(
            owner,
            InstructionInput(scope="global", revision=global_before.revision, mode="custom", text="Global custom"),
        )
        with pytest.raises(HTTPException) as stale:
            await save_instructions(
                owner, InstructionInput(scope="workspace", revision=before.revision, mode="custom", text="Stale child")
            )
        assert stale.value.status_code == 409
        assert not owner.instructions.path("workspace").exists()
        with pytest.raises(HTTPException) as admission:
            async with owner.admit(before.revision):
                pytest.fail("Stale admission must fail")
        assert admission.value.status_code == 409
        assert voice_instructions(captured) == default_voice_instructions("assistant")
        assert voice_instructions(await owner.connection()) == "Global custom"
        # Ordinary Voice edits continue using the same revised CAS and do not overwrite Markdown.
        local = await owner.snapshot("workspace")
        await owner.change(
            WorkspaceVoiceInput(
                scope="workspace", revision=str(local["revision"]), overrides=VoiceOverrides(voice="cedar")
            )
        )
        assert voice_instructions(await owner.connection()) == "Global custom"
        await owner.shutdown()
        restarted = settings(tmp_path)
        await restarted.load()
        assert voice_instructions(await restarted.connection()) == "Global custom"
        await restarted.shutdown()

    asyncio.run(scenario())


def test_legacy_empty_and_nonempty_preferences_keep_their_effective_prompt_until_reset(tmp_path: Path) -> None:
    providers(tmp_path)

    async def scenario() -> None:
        owner = settings(tmp_path)
        await owner.load()
        before = await owner.snapshot("global")
        await owner.change(
            GlobalVoiceInput(
                scope="global",
                revision=str(before["revision"]),
                preferences=VoicePreferences(instructions="Global additional"),
            )
        )
        inherited = await owner.connection()
        assert voice_instructions(inherited).endswith("Global additional")
        await owner.change(
            WorkspaceVoiceInput(
                scope="workspace", revision=inherited.revision, overrides=VoiceOverrides(instructions="")
            )
        )
        cleared = await owner.connection()
        assert voice_instructions(cleared) == default_voice_instructions("assistant")
        assert (
            cleared.instruction_settings["mode"] == "custom" and cleared.instruction_settings["source"] == "workspace"
        )
        await save_instructions(owner, InstructionInput(scope="workspace", revision=cleared.revision, mode="inherit"))
        assert voice_instructions(await owner.connection()).endswith("Global additional")
        assert (await owner.connection()).values.instructions == "", "Existing preference data is not rewritten"
        await owner.shutdown()

    asyncio.run(scenario())


def test_instruction_routes_validate_queries_auth_secrets_and_invalid_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "private-instruction-key")

    async def scenario() -> None:
        async with client_app(tmp_path, config=configuration(tmp_path)) as (_, client, headers, _):
            assert (await client.get("/api/live/instructions?scope=global")).status_code == 403
            for query in (
                "scope=agent&agent=reader&agent=writer",
                "scope=global&token=secret",
                "scope=agent&agent=../reader",
            ):
                assert (await client.get("/api/live/instructions?" + query, headers=headers)).status_code == 400
            current = (await client.get("/api/live/instructions?scope=global", headers=headers)).json()
            body = {"scope": "global", "revision": current["revision"], "mode": "custom", "text": "New text"}
            assert (
                await client.put("/api/live/instructions?scope=global", headers=headers, json=body)
            ).status_code == 400
            for text in ("private-instruction-key", "x" * 12001, "🌍" * 6001, "a\x00b"):
                response = await client.put("/api/live/instructions", headers=headers, json={**body, "text": text})
                assert response.status_code == 422, response.text
            after = (await client.get("/api/live/instructions?scope=global", headers=headers)).json()
            assert after == current

    asyncio.run(scenario())


def test_markdown_store_preserves_body_and_rejects_symlinks_or_unknown_headers(tmp_path: Path) -> None:
    store = VoiceInstructionStore(providers(tmp_path))
    body = "# Voice\n\n---\n\nUnicode: 🌍\n  indentation\r\n"
    with store.locked():
        saved = store.save_locked("global", "", "custom", body, expected="0" * 64)
    assert saved.text == body
    assert store.load("global").text == body
    saved.path.write_text("---\nmode: custom\nunknown: true\n---\nText")
    with pytest.raises(ValueError, match="frontmatter"):
        store.load("global")
    saved.path.unlink()
    target = tmp_path / "target.md"
    target.write_text("---\nmode: custom\n---\nTarget")
    try:
        saved.path.symlink_to(target)
    except OSError:
        pytest.skip("Symlink creation is not permitted")
    with pytest.raises(ValueError, match="regular"):
        store.load("global")


def test_file_edit_after_snapshot_is_not_overwritten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    providers(tmp_path)
    original = VoiceInstructionStore.save_locked

    def external_edit(
        store: VoiceInstructionStore,
        scope: InstructionScope,
        agent: str,
        mode: InstructionMode,
        text: str,
        *,
        expected: str,
    ) -> InstructionPreference:
        store.path(scope, agent).write_text("---\nmode: custom\n---\nEdited outside the UI", encoding="utf-8")
        return original(store, scope, agent, mode, text, expected=expected)

    monkeypatch.setattr(VoiceInstructionStore, "save_locked", external_edit)

    async def scenario() -> None:
        owner = settings(tmp_path)
        await owner.load()
        current = await owner.scoped("global")
        with pytest.raises(HTTPException) as conflict:
            await save_instructions(
                owner, InstructionInput(scope="global", revision=current.revision, mode="custom", text="UI draft")
            )
        assert conflict.value.status_code == 409
        assert voice_instructions(await owner.connection()) == "Edited outside the UI"
        await owner.shutdown()

    asyncio.run(scenario())


def test_independent_owners_compare_and_swap_markdown_writes(tmp_path: Path) -> None:
    providers(tmp_path)

    async def scenario() -> None:
        first, second = settings(tmp_path), settings(tmp_path)
        await asyncio.gather(first.load(), second.load())
        before = await first.scoped("global")
        results = await asyncio.gather(
            save_instructions(
                first, InstructionInput(scope="global", revision=before.revision, mode="custom", text="First")
            ),
            save_instructions(
                second, InstructionInput(scope="global", revision=before.revision, mode="custom", text="Second")
            ),
            return_exceptions=True,
        )
        assert sum(isinstance(result, dict) for result in results) == 1
        rejected = next(result for result in results if isinstance(result, HTTPException))
        assert rejected.status_code == 409
        assert voice_instructions(await first.connection()) == voice_instructions(await second.connection())
        await first.shutdown()
        await second.shutdown()

    asyncio.run(scenario())


def test_external_provider_files_do_not_control_instruction_storage_or_merge_workspaces(tmp_path: Path) -> None:
    external = tmp_path / "external-provider-config"
    external.mkdir()
    paths = {"global": external / "global.json", "workspace": external / "workspace.json"}
    for path in paths.values():
        path.write_text("{}", encoding="utf-8")
    first_registry = ScopedProviderRegistryStore(tmp_path / "first-workspace", paths=paths)
    second_registry = ScopedProviderRegistryStore(tmp_path / "second-workspace", paths=paths)
    first, second = VoiceInstructionStore(first_registry), VoiceInstructionStore(second_registry)
    assert first.path("global").parent == first_registry.model_store("global").path.parent
    assert first.path("workspace").parent == first_registry.model_store("workspace").path.parent
    assert first.path("global") == second.path("global")
    assert first.path("global") != first.path("workspace")
    assert first.path("workspace") != second.path("workspace")
    assert first.path("agent", "assistant") != second.path("agent", "assistant")
    with first.locked():
        first.save_locked("global", "", "custom", "Shared", expected="0" * 64)
        first.save_locked("workspace", "", "custom", "First workspace", expected="0" * 64)
        first.save_locked("agent", "assistant", "custom", "First assistant", expected="0" * 64)
    assert second.load("global").text == "Shared"
    assert not second.load("workspace").present
    assert not second.load("agent", "assistant").present
    with second.locked():
        second.save_locked("workspace", "", "custom", "Second workspace", expected="0" * 64)
    assert first.load("workspace").text == "First workspace"
    assert second.load("workspace").text == "Second workspace"
    assert sorted(path.name for path in external.iterdir()) == ["global.json", "workspace.json"]
    assert all(path.read_text(encoding="utf-8") == "{}" for path in paths.values())
