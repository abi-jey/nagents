"""Voice defaults, workspace inheritance, and cross-owner admission."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from contextlib import closing
from types import SimpleNamespace
from typing import TYPE_CHECKING
from typing import TypedDict
from typing import cast

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from nagents.harness.config import HarnessConfig
from nagents.harness.providers import ProviderProfile
from nagents.harness.providers import ProviderRegistry
from nagents.harness.providers import ScopedProviderRegistryStore
from nagents.web.live import create_agent
from nagents.web.live import unavailable_reason
from nagents.web.live_settings import GlobalVoiceInput
from nagents.web.live_settings import LiveSettings
from nagents.web.live_settings import WorkspaceVoiceInput
from nagents.web.voice_preferences import MAX_VOICE_BYTES
from nagents.web.voice_preferences import VoiceOverrides
from nagents.web.voice_preferences import VoicePreferences
from tests.support.web import client_app

if TYPE_CHECKING:
    from pathlib import Path


class VoiceSnapshot(TypedDict):
    revision: str
    values: dict[str, str | bool]
    global_preferences: dict[str, str | bool]
    overrides: dict[str, str | bool]
    origins: dict[str, str]
    connection_scope: str


def voice(value: dict[str, object]) -> VoiceSnapshot:
    return cast("VoiceSnapshot", value)


def named(path: Path) -> ScopedProviderRegistryStore:
    providers = ScopedProviderRegistryStore(path)
    global_profile = ProviderProfile(
        kind="openai",
        auth="api-key",
        api_key_env="TEST_VOICE_KEY",
    )
    providers.save_scope(
        ProviderRegistry(active="primary", providers={"primary": global_profile}),
        expected="0" * 64,
        scope="global",
    )
    return providers


def settings(path: Path) -> LiveSettings:
    return LiveSettings(path / "sessions.db", demo=False, active=lambda: "")


def test_voice_behavior_and_context_inherit_independently_and_survive_restart(tmp_path: Path) -> None:
    named(tmp_path)

    async def check() -> None:
        owner = settings(tmp_path)
        await owner.load()
        before = voice(await owner.snapshot("global"))
        instructions = "Keep answers brief.\nParlez doucement. 🌍"
        await owner.change(
            GlobalVoiceInput(
                scope="global",
                revision=before["revision"],
                preferences=VoicePreferences(instructions=instructions, context_mode="summary"),
            )
        )
        inherited = voice(await owner.snapshot("workspace"))
        assert inherited["values"]["instructions"] == instructions
        assert inherited["values"]["context_mode"] == "summary"
        await owner.change(
            WorkspaceVoiceInput(
                scope="workspace", revision=inherited["revision"], overrides=VoiceOverrides(context_mode="none")
            )
        )
        global_before = voice(await owner.snapshot("global"))
        await owner.change(
            GlobalVoiceInput(
                scope="global",
                revision=global_before["revision"],
                preferences=VoicePreferences(instructions="Use a calm tone.", context_mode="recent"),
            )
        )
        local = voice(await owner.snapshot("workspace"))
        assert local["values"]["instructions"] == "Use a calm tone."
        assert local["values"]["context_mode"] == "none"
        assert local["overrides"] == {"context_mode": "none"}
        assert local["origins"]["instructions"] == "global" and local["origins"]["context_mode"] == "workspace"
        await owner.change(
            WorkspaceVoiceInput(
                scope="workspace",
                revision=local["revision"],
                overrides=VoiceOverrides(instructions="", context_mode="none"),
            )
        )
        await owner.shutdown()
        restarted = settings(tmp_path)
        await restarted.load()
        restored = voice(await restarted.snapshot("workspace"))
        assert restored["values"]["instructions"] == "" and restored["values"]["context_mode"] == "none"
        assert voice(await restarted.snapshot("global"))["values"]["instructions"] == "Use a calm tone."
        await restarted.change(
            WorkspaceVoiceInput(scope="workspace", revision=restored["revision"], overrides=VoiceOverrides())
        )
        assert voice(await restarted.snapshot("workspace"))["values"]["instructions"] == "Use a calm tone."
        await restarted.shutdown()

    asyncio.run(check())


@pytest.mark.parametrize("instructions", ["🌍" * 3000, "é" * 6000, "\n" * 6000, "\x01" * 6000])
def test_instruction_storage_accepts_unicode_and_json_escaping_at_the_character_budget(
    tmp_path: Path, instructions: str
) -> None:
    named(tmp_path)

    async def check() -> None:
        owner = settings(tmp_path)
        await owner.load()
        before = voice(await owner.snapshot("global"))
        await owner.change(
            GlobalVoiceInput(
                scope="global", revision=before["revision"], preferences=VoicePreferences(instructions=instructions)
            )
        )
        await owner.shutdown()
        restarted = settings(tmp_path)
        await restarted.load()
        assert voice(await restarted.snapshot("global"))["values"]["instructions"] == instructions
        with closing(sqlite3.connect(restarted.voice.global_db)) as db:
            size = db.execute("SELECT length(CAST(payload AS BLOB)) FROM ngn_voice_preferences").fetchone()[0]
        assert 4096 < size <= MAX_VOICE_BYTES
        await restarted.shutdown()

    asyncio.run(check())


@pytest.mark.parametrize(
    "values",
    [
        {"instructions": "x" * 6001},
        {"instructions": "🌍" * 3001},
        {"instructions": "\ud800"},
        {"instructions": 1},
        {"context_mode": "everything"},
        {"context_mode": True},
    ],
)
def test_invalid_voice_behavior_preferences_are_rejected(values: dict[str, object]) -> None:
    for model in (VoicePreferences, VoiceOverrides):
        with pytest.raises(ValidationError):
            model.model_validate(values)


@pytest.mark.parametrize("saved_voice", ["", "marin", "cedar", "cove"])
def test_old_saved_preferences_add_defaults_without_overwriting_saved_choices(tmp_path: Path, saved_voice: str) -> None:
    named(tmp_path)

    async def check() -> None:
        owner = settings(tmp_path)
        await owner.load()
        payload = json.dumps(
            {
                "enabled": True,
                "model": "saved-live",
                "backend_model": "saved-backend",
                **({"voice": saved_voice} if saved_voice else {}),
            }
        )
        with closing(sqlite3.connect(owner.voice.global_db)) as db, db:
            db.execute("UPDATE ngn_voice_preferences SET payload = ?", (payload,))
        current = voice(await owner.snapshot("global"))
        assert current["values"]["instructions"] == "" and current["values"]["context_mode"] == "recent"
        assert current["values"]["model"] == "saved-live" and current["values"]["backend_model"] == "saved-backend"
        assert current["global_preferences"]["voice"] == (saved_voice or "sol")
        assert current["values"]["voice"] == (saved_voice if saved_voice in {"marin", "cedar"} else "marin")
        with closing(sqlite3.connect(owner.voice.global_db)) as db:
            assert db.execute("SELECT payload FROM ngn_voice_preferences").fetchone()[0] == payload
        await owner.shutdown()

    asyncio.run(check())


def test_voice_instructions_cannot_accidentally_store_a_configured_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    named(tmp_path)
    monkeypatch.setenv("TEST_VOICE_KEY", "private-voice-key")

    async def check() -> None:
        owner = settings(tmp_path)
        await owner.load()
        before = voice(await owner.snapshot("global"))
        with pytest.raises(HTTPException) as rejected:
            await owner.change(
                GlobalVoiceInput(
                    scope="global",
                    revision=before["revision"],
                    preferences=VoicePreferences(instructions="Use private-voice-key"),
                )
            )
        assert rejected.value.status_code == 422
        after = voice(await owner.snapshot("global"))
        assert after["revision"] == before["revision"] and after["values"]["instructions"] == ""
        await owner.shutdown()

    asyncio.run(check())


@pytest.mark.parametrize("kind,auth", [("anthropic", "api-key"), ("gemini", "api-key"), ("openai", "chatgpt")])
def test_voice_connection_preserves_independent_chat_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, auth: str
) -> None:
    providers = named(tmp_path)
    before = providers.load_scope("global")
    providers.save_scope(
        ProviderRegistry(active="chat", providers={**before.providers, "chat": ProviderProfile(kind=kind, auth=auth)}),
        expected=before.revision,
        scope="global",
    )
    monkeypatch.setenv("TEST_VOICE_KEY", "private-voice-key")

    async def handle(transcript: str) -> str:
        return transcript

    async def check() -> None:
        owner = settings(tmp_path)
        await owner.load()
        before = voice(await owner.snapshot("global"))
        await owner.change(
            GlobalVoiceInput(
                scope="global",
                revision=before["revision"],
                preferences=VoicePreferences(enabled=True, connection_id="primary"),
            )
        )
        connection = await owner.connection()
        assert connection.profile_name == "primary" and connection.key_configured
        assert connection.values.connection_id == "primary"
        assert connection.connection_scope == "global"
        assert providers.load().active == "chat"
        assert providers.load().providers["chat"].kind == kind
        assert "private-voice-key" not in str(connection.snapshot())
        assert ("primary", "openai", "global") in connection.connections
        agent = create_agent(connection, client_handler=handle)
        try:
            assert agent.provider.api_key == "private-voice-key"
            assert agent.provider.live_config is not None
            assert agent.provider.live_config.delegation == "client"
        finally:
            await agent.close()
            await owner.shutdown()

    asyncio.run(check())


def test_missing_voice_connection_never_falls_back_to_chat(tmp_path: Path) -> None:
    providers = named(tmp_path)

    async def check() -> None:
        owner = settings(tmp_path)
        await owner.load()
        before = voice(await owner.snapshot("workspace"))
        for identifier in ("missing", "unsupported"):
            if identifier == "unsupported":
                registry = providers.load_scope("workspace")
                providers.save_scope(
                    ProviderRegistry(providers={"unsupported": ProviderProfile(kind="anthropic")}),
                    expected=registry.revision,
                    scope="workspace",
                )
                before = voice(await owner.snapshot("workspace"))
            with pytest.raises(HTTPException) as rejected:
                await owner.change(
                    WorkspaceVoiceInput(
                        scope="workspace",
                        revision=before["revision"],
                        overrides=VoiceOverrides(enabled=True, connection_id=identifier),
                    )
                )
            assert rejected.value.status_code == 422
        await owner.change(
            WorkspaceVoiceInput(
                scope="workspace",
                revision=before["revision"],
                overrides=VoiceOverrides(enabled=True, connection_id="primary"),
            )
        )
        selected = await owner.connection()
        registry = providers.load_scope("global")
        providers.save_scope(ProviderRegistry(), expected=registry.revision, scope="global")
        missing = await owner.connection()
        assert missing.revision != selected.revision
        assert missing.profile is None and not missing.key_configured
        assert missing.profile_name == "primary"
        assert "no longer exists" in unavailable_reason(missing, demo=False)
        await owner.shutdown()

    asyncio.run(check())


def test_global_and_local_voice_values_are_independent_of_provider_yaml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    providers = named(tmp_path)
    local_profile = ProviderProfile(
        kind="openai",
        auth="api-key",
        api_key_env="TEST_VOICE_KEY",
    )
    providers.save_scope(
        ProviderRegistry(active="local", providers={"local": local_profile}),
        expected="0" * 64,
        scope="workspace",
    )
    monkeypatch.setenv("TEST_VOICE_KEY", "private-voice-key")
    original_global = providers.global_store.path.read_bytes()
    original_local = providers.workspace_store.path.read_bytes()

    async def check() -> None:
        first = settings(tmp_path)
        await first.load()
        global_before = voice(await first.snapshot("global"))
        assert global_before["values"]["model"] == "gpt-live-1"
        assert global_before["values"]["enabled"] is False
        await first.change(
            GlobalVoiceInput(
                scope="global",
                revision=global_before["revision"],
                preferences=VoicePreferences(
                    enabled=True,
                    backend_mode="hosted",
                    model="global-live",
                    backend_model="global-backend",
                    voice="cedar",
                ),
            )
        )
        global_before = voice(await first.snapshot("global"))
        workspace_before = voice(await first.snapshot("workspace"))
        assert global_before["values"]["model"] == "global-live"
        assert global_before["values"]["backend_mode"] == "hosted"
        assert workspace_before["values"]["model"] == "global-live"
        assert workspace_before["overrides"] == {}
        await first.change(
            WorkspaceVoiceInput(
                scope="workspace",
                revision=workspace_before["revision"],
                overrides=VoiceOverrides(
                    enabled=True,
                    backend_mode="hosted",
                    model="local-live",
                    backend_model="local-backend",
                    voice="marin",
                ),
            )
        )
        workspace_before = voice(await first.snapshot("workspace"))
        assert workspace_before["values"]["model"] == "local-live"
        assert workspace_before["overrides"] == {
            "enabled": True,
            "backend_mode": "hosted",
            "model": "local-live",
            "backend_model": "local-backend",
            "voice": "marin",
        }
        assert workspace_before["connection_scope"] == "workspace"
        assert (await first.connection()).key_configured
        agent = create_agent(await first.connection())
        try:
            assert agent.provider.model == "local-live"
            assert agent.provider.api_key == "private-voice-key"
        finally:
            await agent.close()

        changed = voice(
            await first.change(
                GlobalVoiceInput(
                    scope="global",
                    revision=global_before["revision"],
                    preferences=VoicePreferences(
                        enabled=True, model="new-global", backend_model="new-backend", voice="cedar"
                    ),
                )
            )
        )
        assert changed["values"]["model"] == "new-global"
        assert voice(await first.snapshot("workspace"))["values"]["model"] == "local-live"
        assert providers.global_store.path.read_bytes() == original_global
        assert providers.workspace_store.path.read_bytes() == original_local

        second = settings(tmp_path)
        await second.load()
        assert voice(await second.snapshot("global"))["values"]["model"] == "new-global"
        assert voice(await second.snapshot("workspace"))["values"]["model"] == "local-live"
        await second.shutdown()
        await first.shutdown()

    asyncio.run(check())


def test_individual_overrides_inherit_later_global_edits_across_workspaces(tmp_path: Path) -> None:
    named(tmp_path)

    async def check() -> None:
        first = settings(tmp_path)
        other_path = tmp_path / "other"
        other_path.mkdir()
        second = settings(other_path)
        await asyncio.gather(first.load(), second.load())
        global_snapshot = voice(await first.snapshot("global"))
        await first.change(
            GlobalVoiceInput(
                scope="global",
                revision=global_snapshot["revision"],
                preferences=VoicePreferences(enabled=True, model="global-live", voice="cedar"),
            )
        )
        initial = voice(await first.snapshot("workspace"))
        assert initial["overrides"] == {} and initial["values"]["voice"] == "cedar"
        saved = voice(
            await first.change(
                WorkspaceVoiceInput(
                    scope="workspace",
                    revision=initial["revision"],
                    overrides=VoiceOverrides(model="workspace-live"),
                )
            )
        )
        assert saved["origins"]["model"] == "workspace" and saved["origins"]["voice"] == "global"
        global_snapshot = voice(await second.snapshot("global"))
        await second.change(
            GlobalVoiceInput(
                scope="global",
                revision=global_snapshot["revision"],
                preferences=VoicePreferences(enabled=True, model="new-global", voice="marin", backend_mode="assistant"),
            )
        )
        updated = voice(await first.snapshot("workspace"))
        assert (updated["values"]["model"], updated["values"]["voice"], updated["values"]["backend_mode"]) == (
            "workspace-live",
            "marin",
            "assistant",
        )
        assert voice(await second.snapshot("workspace"))["values"]["model"] == "new-global"
        with pytest.raises(HTTPException) as stale:
            await first.change(
                WorkspaceVoiceInput(
                    scope="workspace",
                    revision=saved["revision"],
                    overrides=VoiceOverrides(voice="cedar"),
                )
            )
        assert stale.value.status_code == 409
        assert (await first.connection()).revision == updated["revision"]
        await first.shutdown()
        await second.shutdown()

    asyncio.run(check())


def test_foundry_live_uses_voice_model_and_connection_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    providers = ScopedProviderRegistryStore(tmp_path)
    providers.save_scope(
        ProviderRegistry(
            active="foundry",
            providers={
                "foundry": ProviderProfile(
                    kind="foundry",
                    auth="entra",
                    api="responses",
                    base_url="https://example.openai.azure.com/openai/v1",
                ),
            },
        ),
        expected="0" * 64,
        scope="global",
    )
    providers.model_store("global").save("chat-deployment")

    class Credential:
        async def get_token(self, *scopes: str) -> SimpleNamespace:
            return SimpleNamespace(token="synthetic-token")

        async def close(self) -> None:
            pass

    monkeypatch.setitem(sys.modules, "azure.identity.aio", SimpleNamespace(DefaultAzureCredential=Credential))

    async def check() -> None:
        owner = settings(tmp_path)
        await owner.load()
        current = voice(await owner.snapshot("global"))
        await owner.change(
            GlobalVoiceInput(
                scope="global",
                revision=current["revision"],
                preferences=VoicePreferences(enabled=True, backend_mode="hosted", model="voice-deployment"),
            )
        )
        agent = create_agent(await owner.connection())
        try:
            assert agent.provider.model == "voice-deployment"
            assert providers.model() == "chat-deployment"
        finally:
            await agent.close()

    asyncio.run(check())


def test_scoped_revision_conflicts_and_admission_pins_the_model(tmp_path: Path) -> None:
    named(tmp_path)

    async def check() -> None:
        one, two = settings(tmp_path), settings(tmp_path)
        await asyncio.gather(one.load(), two.load())
        global_snapshot = voice(await one.snapshot("global"))
        await one.change(
            GlobalVoiceInput(
                scope="global",
                revision=global_snapshot["revision"],
                preferences=VoicePreferences(enabled=True, voice="cedar"),
            )
        )
        old = voice(await one.snapshot("workspace"))
        writes = await asyncio.gather(
            one.change(
                WorkspaceVoiceInput(
                    scope="workspace",
                    revision=old["revision"],
                    overrides=VoiceOverrides(model="voice-one"),
                )
            ),
            two.change(
                WorkspaceVoiceInput(
                    scope="workspace",
                    revision=old["revision"],
                    overrides=VoiceOverrides(model="voice-two"),
                )
            ),
            return_exceptions=True,
        )
        assert sum(isinstance(write, dict) for write in writes) == 1
        assert sum(isinstance(write, HTTPException) and write.status_code == 409 for write in writes) == 1
        current = await one.connection()
        async with one.admit(current.revision):
            next_values = VoicePreferences.model_validate(
                {
                    **voice(await two.snapshot("global"))["global_preferences"],
                    "voice": "marin",
                }
            )
            global_snapshot = voice(await two.snapshot("global"))
            await two.change(
                GlobalVoiceInput(
                    scope="global",
                    revision=global_snapshot["revision"],
                    preferences=next_values,
                )
            )
            assert one.admitted().values.voice == "cedar"
            assert (await one.connection()).revision != current.revision
            with pytest.raises(HTTPException) as busy:
                await one.change(
                    WorkspaceVoiceInput(
                        scope="workspace",
                        revision=current.revision,
                        overrides=VoiceOverrides(),
                    )
                )
            assert busy.value.status_code == 409
        with pytest.raises(HTTPException) as stale:
            async with one.admit(current.revision):
                pytest.fail("A stale call cannot be admitted")
        assert stale.value.status_code == 409

    asyncio.run(check())


def test_scoped_payload_rejects_connection_fields_and_failed_write_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    named(tmp_path)
    monkeypatch.setenv("TEST_VOICE_KEY", "private-voice-key")
    with pytest.raises(ValidationError):
        WorkspaceVoiceInput.model_validate(
            {
                "scope": "workspace",
                "revision": "0" * 64,
                "overrides": {"model": "voice-model", "api_key": "private", "base_url": "https://other.invalid"},
            }
        )

    async def check() -> None:
        owner = settings(tmp_path)
        await owner.load()
        before = voice(await owner.snapshot("workspace"))
        with pytest.raises(HTTPException) as leaked:
            await owner.change(
                WorkspaceVoiceInput(
                    scope="workspace",
                    revision=before["revision"],
                    overrides=VoiceOverrides(model="private-voice-key"),
                )
            )
        assert leaked.value.status_code == 422 and "private-voice-key" not in str(leaked.value)
        with closing(sqlite3.connect(owner.db_path)) as db, db:
            db.execute(
                "CREATE TRIGGER fail_voice BEFORE UPDATE ON ngn_voice_preferences "
                "BEGIN SELECT RAISE(ABORT, 'private-voice-key'); END"
            )
        with pytest.raises(HTTPException) as failed:
            await owner.change(
                WorkspaceVoiceInput(
                    scope="workspace",
                    revision=before["revision"],
                    overrides=VoiceOverrides(model="later"),
                )
            )
        assert failed.value.status_code == 500 and "private-voice-key" not in str(failed.value)
        assert await owner.snapshot("workspace") == before

    asyncio.run(check())


def test_scoped_http_settings_expose_inheritance_and_never_edit_connection(tmp_path: Path) -> None:
    providers = named(tmp_path)
    original = providers.global_store.path.read_bytes()

    async def check() -> None:
        config = HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "data", demo=True)
        async with client_app(tmp_path, config=config) as (_, client, headers, _):
            global_response = await client.get("/api/live/settings?scope=global", headers=headers)
            local_response = await client.get("/api/live/settings?scope=workspace", headers=headers)
            assert global_response.status_code == local_response.status_code == 200
            assert (
                await client.get(
                    "/api/live/settings?scope=workspace&token=private",
                    headers=headers,
                )
            ).status_code == 400
            global_snapshot = global_response.json()
            local_snapshot = local_response.json()
            assert global_snapshot["global_preferences"]["model"] == "gpt-live-1"
            assert local_snapshot["overrides"] == {}
            assert local_snapshot["origins"]["model"] == "global"
            bad = await client.post(
                "/api/live/settings",
                headers=headers,
                json={
                    "scope": "workspace",
                    "revision": local_snapshot["revision"],
                    "overrides": {"model": "voice-only", "base_url": "https://other.invalid/v1"},
                },
            )
            assert bad.status_code == 422
            changed = await client.post(
                "/api/live/settings",
                headers=headers,
                json={
                    "scope": "workspace",
                    "revision": local_snapshot["revision"],
                    "overrides": {"model": "voice-only"},
                },
            )
            assert changed.status_code == 200, changed.text
            assert changed.json()["values"]["model"] == "voice-only"
            assert changed.json()["origins"]["voice"] == "global"
            assert (await client.get("/api/live", headers=headers)).json()["model"] == "voice-only"
            assert (await client.get("/api/live/settings?scope=global", headers=headers)).json() == global_snapshot
            assert providers.global_store.path.read_bytes() == original

    asyncio.run(check())
