"""Scoped Live preference revisions and cross-owner CAS behavior."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from typing import cast

from fastapi import HTTPException

from nagents.harness.providers import ProviderProfile
from nagents.harness.providers import ProviderRegistry
from nagents.harness.providers import ScopedProviderRegistryStore
from nagents.web.live_settings import GlobalVoiceInput
from nagents.web.live_settings import LiveSettings
from nagents.web.live_settings import WorkspaceVoiceInput
from nagents.web.voice_preferences import VoiceOverrides
from nagents.web.voice_preferences import VoicePreferences

if TYPE_CHECKING:
    from pathlib import Path


def fields(snapshot: dict[str, object], key: str) -> dict[str, object]:
    value = snapshot[key]
    assert isinstance(value, dict)
    return cast("dict[str, object]", value)


def providers(path: Path) -> ScopedProviderRegistryStore:
    store = ScopedProviderRegistryStore(path)
    store.save_scope(
        ProviderRegistry(
            active="voice",
            providers={"voice": ProviderProfile(kind="openai", auth="api-key", api_key_env="TEST_LIVE_KEY")},
        ),
        expected="0" * 64,
        scope="global",
    )
    return store


def settings(path: Path) -> LiveSettings:
    return LiveSettings(path / "sessions.db", demo=False, active=lambda: "")


def test_scoped_preference_revisions_persist_and_inherit_independently(tmp_path: Path) -> None:
    providers(tmp_path)

    async def check() -> None:
        owner = settings(tmp_path)
        await owner.load()
        global_before = await owner.snapshot("global")
        local_before = await owner.snapshot("workspace")
        assert global_before["scope"] == "global" and local_before["scope"] == "workspace"
        assert global_before["revision"] != local_before["revision"]

        saved_global = await owner.change(
            GlobalVoiceInput(
                scope="global",
                revision=str(global_before["revision"]),
                preferences=VoicePreferences(context_mode="summary"),
            )
        )
        assert fields(saved_global, "global_preferences")["context_mode"] == "summary"
        inherited = await owner.snapshot("workspace")
        assert fields(inherited, "values")["instructions"] == fields(global_before, "values")["instructions"]
        await owner.change(
            WorkspaceVoiceInput(
                scope="workspace",
                revision=str(inherited["revision"]),
                overrides=VoiceOverrides(context_mode="none"),
            )
        )
        local = await owner.snapshot("workspace")
        assert fields(local, "values")["instructions"] == fields(global_before, "values")["instructions"]
        assert fields(local, "values")["context_mode"] == "none"
        assert fields(local, "origins")["context_mode"] == "workspace"
        await owner.shutdown()

        restarted = settings(tmp_path)
        await restarted.load()
        assert fields(await restarted.snapshot("global"), "values")["context_mode"] == "summary"
        restored = await restarted.snapshot("workspace")
        assert fields(restored, "values")["context_mode"] == "none"
        await restarted.shutdown()

    asyncio.run(check())


def test_independent_owners_compare_and_swap_scoped_preference_writes(tmp_path: Path) -> None:
    providers(tmp_path)

    async def check() -> None:
        first, second = settings(tmp_path), settings(tmp_path)
        await asyncio.gather(first.load(), second.load())
        before = await first.snapshot("global")
        assert await second.snapshot("global") == before
        writes = (
            GlobalVoiceInput(
                scope="global",
                revision=str(before["revision"]),
                preferences=VoicePreferences(voice="sol"),
            ),
            GlobalVoiceInput(
                scope="global",
                revision=str(before["revision"]),
                preferences=VoicePreferences(voice="cove"),
            ),
        )
        results = await asyncio.gather(first.change(writes[0]), second.change(writes[1]), return_exceptions=True)
        assert sum(isinstance(result, dict) for result in results) == 1
        rejected = next(result for result in results if isinstance(result, HTTPException))
        assert rejected.status_code == 409
        assert await first.snapshot("global") == await second.snapshot("global")
        await first.shutdown()
        await second.shutdown()

    asyncio.run(check())


def test_fresh_workspace_uses_standard_voice_preferences(tmp_path: Path) -> None:
    providers(tmp_path)

    async def check() -> None:
        owner = settings(tmp_path)
        await owner.load()
        snapshot = await owner.snapshot("workspace")
        assert fields(snapshot, "values")["enabled"] is False
        assert fields(snapshot, "values")["backend_mode"] == "assistant"
        assert fields(snapshot, "values")["voice"] == "marin"
        assert fields(snapshot, "global_preferences")["voice"] == "sol"
        assert fields(snapshot, "overrides") == {}
        await owner.shutdown()

    asyncio.run(check())
