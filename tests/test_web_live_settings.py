"""Scoped Live preference revisions and legacy persisted connection reads."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING
from typing import cast

import pytest
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
                preferences=VoicePreferences(instructions="Speak calmly.", context_mode="summary"),
            )
        )
        assert fields(saved_global, "global_preferences")["instructions"] == "Speak calmly."
        inherited = await owner.snapshot("workspace")
        assert fields(inherited, "values")["instructions"] == "Speak calmly."
        await owner.change(
            WorkspaceVoiceInput(
                scope="workspace",
                revision=str(inherited["revision"]),
                overrides=VoiceOverrides(context_mode="none"),
            )
        )
        local = await owner.snapshot("workspace")
        assert fields(local, "values")["instructions"] == "Speak calmly."
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


def test_voice_preferences_reject_configured_api_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    providers(tmp_path)
    monkeypatch.setenv("TEST_LIVE_KEY", "private-live-key")

    async def check() -> None:
        owner = settings(tmp_path)
        await owner.load()
        before = await owner.snapshot("global")
        with pytest.raises(HTTPException) as rejected:
            await owner.change(
                GlobalVoiceInput(
                    scope="global",
                    revision=str(before["revision"]),
                    preferences=VoicePreferences(instructions="Repeat private-live-key."),
                )
            )
        assert rejected.value.status_code == 422
        after = await owner.snapshot("global")
        assert after["revision"] == before["revision"]
        assert "private-live-key" not in str(after)
        await owner.shutdown()

    asyncio.run(check())


def test_legacy_voice_preferences_migrate_once_without_copying_connection_secrets(tmp_path: Path) -> None:
    providers(tmp_path)
    database = tmp_path / "sessions.db"
    legacy = {
        "enabled": True,
        "provider": "openai",
        "model": "gpt-live-1",
        "backend_model": "gpt-realtime-1.5",
        "voice": "marin",
        "instructions": "Speak slowly.",
        "context_mode": "summary",
        "base_url": "https://private.example/v1",
    }
    with closing(sqlite3.connect(database)) as db, db:
        db.execute(
            "CREATE TABLE ngn_web_live_settings (id INTEGER PRIMARY KEY, version INTEGER, values_json TEXT, revision TEXT)"
        )
        db.execute("CREATE TABLE ngn_web_live_key (id INTEGER PRIMARY KEY, provider TEXT, endpoint TEXT, secret TEXT)")
        db.execute(
            "INSERT INTO ngn_web_live_settings VALUES (1, 1, ?, ?)",
            (json.dumps(legacy), "a" * 64),
        )
        db.execute(
            "INSERT INTO ngn_web_live_key VALUES (1, ?, ?, ?)",
            ("openai", "https://private.example/v1", "legacy-private-key"),
        )

    async def check() -> None:
        owner = settings(tmp_path)
        await owner.load()
        snapshot = await owner.snapshot("workspace")
        assert fields(snapshot, "values")["enabled"] is True
        assert fields(snapshot, "values")["backend_mode"] == "hosted"
        assert fields(snapshot, "values")["instructions"] == "Speak slowly."
        assert fields(snapshot, "values")["context_mode"] == "summary"
        assert fields(snapshot, "overrides") == {
            "enabled": True,
            "backend_mode": "hosted",
            "model": "gpt-live-1",
            "backend_model": "gpt-realtime-1.5",
            "voice": "marin",
            "instructions": "Speak slowly.",
            "context_mode": "summary",
        }
        assert "private.example" not in json.dumps(snapshot)
        assert "legacy-private-key" not in json.dumps(snapshot)
        connection = await owner.connection()
        assert connection.profile_name == "voice"
        assert connection.api_key.get_secret_value() != "legacy-private-key"

        def read_rows() -> tuple[str, str, str]:
            with closing(sqlite3.connect(owner.db_path)) as db:
                return (
                    db.execute("SELECT values_json FROM ngn_web_live_settings WHERE id = 1").fetchone()[0],
                    db.execute("SELECT secret FROM ngn_web_live_key WHERE id = 1").fetchone()[0],
                    cast("str", db.execute("SELECT payload FROM ngn_voice_preferences WHERE id = 1").fetchone()[0]),
                )

        persisted, key, migrated_payload = await asyncio.to_thread(read_rows)
        assert json.loads(persisted) == legacy and key == "legacy-private-key"
        await owner.shutdown()

        restarted = settings(tmp_path)
        await restarted.load()
        assert fields(await restarted.snapshot("workspace"), "overrides") == fields(snapshot, "overrides")

        def read_migrated_payload() -> str:
            with closing(sqlite3.connect(restarted.db_path)) as db:
                return cast("str", db.execute("SELECT payload FROM ngn_voice_preferences WHERE id = 1").fetchone()[0])

        repeated_payload = await asyncio.to_thread(read_migrated_payload)
        assert repeated_payload == migrated_payload
        await restarted.shutdown()

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


def test_existing_workspace_voice_override_is_not_replaced_by_legacy_preferences(tmp_path: Path) -> None:
    providers(tmp_path)
    database = tmp_path / "sessions.db"
    legacy = {"enabled": True, "voice": "marin"}
    payload = VoiceOverrides(context_mode="none").model_dump_json(exclude_unset=True)
    with closing(sqlite3.connect(database)) as db, db:
        db.execute(
            "CREATE TABLE ngn_web_live_settings (id INTEGER PRIMARY KEY, version INTEGER, values_json TEXT, revision TEXT)"
        )
        db.execute(
            "INSERT INTO ngn_web_live_settings VALUES (1, 1, ?, ?)",
            (json.dumps(legacy), "b" * 64),
        )
        db.execute(
            "CREATE TABLE ngn_voice_preferences (id INTEGER PRIMARY KEY, version INTEGER, payload TEXT, revision TEXT)"
        )
        db.execute("INSERT INTO ngn_voice_preferences VALUES (1, 1, ?, ?)", (payload, "c" * 64))

    async def check() -> None:
        owner = settings(tmp_path)
        await owner.load()
        assert fields(await owner.snapshot("workspace"), "overrides") == {"context_mode": "none"}
        with closing(sqlite3.connect(database)) as db:
            assert db.execute("SELECT payload FROM ngn_voice_preferences WHERE id = 1").fetchone()[0] == payload
        await owner.shutdown()

    asyncio.run(check())


def test_corrupt_legacy_voice_preferences_fail_without_rewriting_old_rows(tmp_path: Path) -> None:
    providers(tmp_path)
    database = tmp_path / "sessions.db"
    malformed = "{not-json"
    with closing(sqlite3.connect(database)) as db, db:
        db.execute(
            "CREATE TABLE ngn_web_live_settings (id INTEGER PRIMARY KEY, version INTEGER, values_json TEXT, revision TEXT)"
        )
        db.execute("INSERT INTO ngn_web_live_settings VALUES (1, 1, ?, ?)", (malformed, "d" * 64))

    async def check() -> None:
        owner = settings(tmp_path)
        with pytest.raises(HTTPException, match="could not be loaded"):
            await owner.load()
        with closing(sqlite3.connect(database)) as db:
            assert db.execute("SELECT values_json FROM ngn_web_live_settings WHERE id = 1").fetchone()[0] == malformed
            assert (
                db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ngn_voice_preferences'"
                ).fetchone()
                is None
            )

    asyncio.run(check())
