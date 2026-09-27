"""Voice defaults, workspace inheritance, v1 import and cross-owner admission."""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from contextlib import closing
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING
from typing import TypedDict
from typing import cast

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from nagents.harness.config import HarnessConfig
from nagents.harness.providers import LiveProfile
from nagents.harness.providers import ProviderProfile
from nagents.harness.providers import ProviderRegistry
from nagents.harness.providers import ScopedProviderRegistryStore
from nagents.web.live import create_agent
from nagents.web.live_settings import GlobalVoiceInput
from nagents.web.live_settings import LiveSettings
from nagents.web.live_settings import WorkspaceVoiceInput
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
        model="chat-model",
        auth="api-key",
        api_key_env="TEST_VOICE_KEY",
        live=LiveProfile(enabled=True, model="global-live", backend_model="global-backend", voice="cedar"),
    )
    providers.save_scope(
        ProviderRegistry(active="primary", providers={"primary": global_profile}),
        expected="0" * 64,
        scope="global",
    )
    return providers


def settings(path: Path) -> LiveSettings:
    return LiveSettings(path / "sessions.db", demo=False, active=lambda: "")


def test_imported_global_and_local_live_values_are_independent_of_provider_yaml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    providers = named(tmp_path)
    local_profile = ProviderProfile(
        kind="openai",
        model="other-chat",
        auth="api-key",
        api_key_env="TEST_VOICE_KEY",
        live=LiveProfile(enabled=True, model="local-live", backend_model="local-backend", voice="marin"),
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
        workspace_before = voice(await first.snapshot("workspace"))
        assert global_before["values"]["model"] == "global-live"
        assert global_before["values"]["backend_mode"] == "hosted"
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

        # A v1 provider edit after migration must not replace either saved Voice scope.
        registry = providers.load_scope("global")
        providers.save_scope(
            ProviderRegistry(
                active=registry.active,
                providers={"primary": replace(registry.providers["primary"], live=LiveProfile(model="later-yaml"))},
            ),
            expected=registry.revision,
            scope="global",
        )
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
                    model="chat-deployment",
                    auth="entra",
                    api="responses",
                    base_url="https://example.openai.azure.com/openai/v1",
                ),
            },
        ),
        expected="0" * 64,
        scope="global",
    )

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
            assert providers.load().providers["foundry"].model == "chat-deployment"
        finally:
            await agent.close()

    asyncio.run(check())


def test_scoped_revision_conflicts_and_admission_pins_the_model(tmp_path: Path) -> None:
    named(tmp_path)

    async def check() -> None:
        one, two = settings(tmp_path), settings(tmp_path)
        await asyncio.gather(one.load(), two.load())
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
            assert global_snapshot["global_preferences"]["model"] == "global-live"
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
