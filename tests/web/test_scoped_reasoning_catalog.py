"""Scoped catalog choices remain saveable without borrowing another route's cache."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from nagents.harness.auth import OpenAIAuth
from nagents.harness.config import HarnessConfig
from nagents.harness.model_catalog import ModelCatalog
from nagents.harness.providers import ProviderProfile
from nagents.harness.providers import ProviderRegistry
from nagents.harness.providers import ScopedProviderRegistryStore
from nagents.web import model_capabilities
from nagents.web.model_capabilities import CatalogCapabilities
from nagents.web.model_capabilities import catalog_route
from tests.support.web import client_app

if TYPE_CHECKING:
    from pathlib import Path

    from nagents.types import ReasoningEffort

MODEL = "custom-catalog-model"


@pytest.mark.parametrize("invalidation", ["profile", "credentials", "expiry"])
def test_global_catalog_choices_save_and_remain_separate_from_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalidation: str
) -> None:
    store = ScopedProviderRegistryStore(tmp_path)
    profiles = {
        scope: ProviderProfile(
            kind="openai_compatible",
            api="responses",
            base_url=f"https://{scope}.example/v1",
            api_key_env=f"{scope.upper()}_KEY",
        )
        for scope in ("global", "workspace")
    }
    for scope, profile in profiles.items():
        monkeypatch.setenv(profile.key_env, f"SECRET-{scope}-key")
        store.store(scope).save(ProviderRegistry(active="shared", providers={"shared": profile}), expected="0" * 64)
    monkeypatch.setattr(model_capabilities, "monotonic", lambda: 1000.0)
    refreshed = False

    async def discover(name: str, *, scope: str) -> ModelCatalog:
        assert name == "shared"
        efforts: list[ReasoningEffort] = ["high"] if scope == "global" and not refreshed else ["low"]
        return ModelCatalog([MODEL], {MODEL: {"reasoning_efforts": efforts}})

    async def check() -> None:
        nonlocal refreshed
        config = HarnessConfig(
            workspace=tmp_path,
            data_dir=tmp_path / "data",
            provider="shared",
            providers=dict(store.load().providers),
        )
        async with client_app(tmp_path, config=config) as (_, client, headers, harnesses):
            discovery = AsyncMock(side_effect=discover)
            monkeypatch.setattr(harnesses[0], "provider_catalog", discovery)
            for scope, effort in (("global", "high"), ("workspace", "low")):
                response = await client.get(f"/api/provider-scopes/{scope}/providers/shared/models", headers=headers)
                assert response.status_code == 200, response.text
                options = await client.get(
                    "/api/reasoning-options",
                    headers=headers,
                    params={"scope": scope, "provider": "shared", "model": MODEL},
                )
                assert options.json()["options"] == [effort]
                assert options.json()["source"] == "Provider metadata"
                assert "SECRET" not in response.text + options.text

            global_settings = (await client.get("/api/settings/global", headers=headers)).json()
            saved = await client.post(
                "/api/settings/global",
                headers=headers,
                json={
                    "revision": global_settings["revision"],
                    "values": {**global_settings["values"], "model": MODEL, "reasoning_effort": "high"},
                },
            )
            assert saved.status_code == 200, saved.text
            assert saved.json()["values"]["reasoning_effort"] == "high"
            unsupported = await client.post(
                "/api/settings/global",
                headers=headers,
                json={
                    "revision": saved.json()["revision"],
                    "values": {**saved.json()["values"], "reasoning_effort": "medium"},
                },
            )
            assert unsupported.status_code == 422
            workspace_settings = (await client.get("/api/settings", headers=headers)).json()
            wrong_scope = await client.post(
                "/api/settings",
                headers=headers,
                json={
                    "revision": workspace_settings["revision"],
                    "values": {**workspace_settings["values"], "reasoning_effort": "high"},
                },
            )
            assert wrong_scope.status_code == 422
            assert discovery.await_count == 2, "Capability reads and validation must not rediscover models"

            if invalidation == "profile":
                current = store.load_scope("global")
                changed = replace(profiles["global"], base_url="https://changed.example/v1")
                store.store("global").save(
                    ProviderRegistry(active="shared", providers={"shared": changed}), expected=current.revision
                )
            elif invalidation == "credentials":
                monkeypatch.setenv("GLOBAL_KEY", "SECRET-replaced-key")
            else:
                monkeypatch.setattr(model_capabilities, "monotonic", lambda: 1301.0)
            refreshed = True
            options = await client.get(
                "/api/reasoning-options",
                headers=headers,
                params={"scope": "global", "provider": "shared", "model": MODEL},
            )
            assert options.json()["options"] == []
            rejected = await client.post(
                "/api/settings/global",
                headers=headers,
                json={"revision": saved.json()["revision"], "values": saved.json()["values"]},
            )
            assert rejected.status_code == 422
            assert "Refresh the model catalog" in rejected.json()["detail"]
            assert discovery.await_count == 3

    asyncio.run(check())


def test_catalog_cache_is_bounded_and_preserves_explicit_empty_choices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CATALOG_KEY", "SECRET-key")
    monkeypatch.setattr(model_capabilities, "MAX_CATALOG_CONNECTIONS", 2)
    profile = ProviderProfile(
        kind="openai_compatible", base_url="https://catalog.example/v1", api_key_env="CATALOG_KEY"
    )
    route = catalog_route(profile, OpenAIAuth())
    cache = CatalogCapabilities()
    empty = ModelCatalog([MODEL], {MODEL: {"reasoning_efforts": []}})
    for name in ("first", "second", "third"):
        cache.remember("global", name, route, empty)
    assert not cache.lookup("global", "first", route, MODEL).tracked
    kept = cache.lookup("global", "third", route, MODEL)
    assert kept.known and kept.options == ()
    assert not cache.lookup("workspace", "third", route, MODEL).tracked
    monkeypatch.setenv("CATALOG_KEY", "SECRET-other-key")
    changed = cache.lookup("global", "third", catalog_route(profile, OpenAIAuth()), MODEL)
    assert changed.tracked and not changed.known
    assert "SECRET" not in repr(route)


def test_catalog_is_not_retained_if_credentials_change_during_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "SECRET-before")

    async def check() -> None:
        config = HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "data")
        async with client_app(tmp_path, config=config) as (_, client, headers, harnesses):

            async def changed(name: str, *, scope: str) -> ModelCatalog:
                monkeypatch.setenv("OPENAI_API_KEY", "SECRET-after")
                return ModelCatalog([MODEL], {MODEL: {"reasoning_efforts": ["high"]}})

            monkeypatch.setattr(harnesses[0], "provider_catalog", changed)
            response = await client.get("/api/provider-scopes/workspace/providers/fixture/models", headers=headers)
            assert response.status_code == 200
            options = await client.get(
                "/api/reasoning-options", headers=headers, params={"provider": "fixture", "model": MODEL}
            )
            assert options.json()["options"] == []

    asyncio.run(check())


@pytest.mark.parametrize("auth_mode", ["chatgpt", "entra"])
@pytest.mark.parametrize("advertised_after_refresh", [False, True])
def test_save_verifies_choices_after_oauth_refresh_or_for_opaque_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, auth_mode: str, advertised_after_refresh: bool
) -> None:
    from nagents.harness import runtime
    from nagents.harness.auth import _Tokens
    from nagents.provider import Provider
    from nagents.provider import ProviderType

    tokens = _Tokens("SECRET-old-access", "SECRET-refresh", "", 1.0)
    monkeypatch.setattr(OpenAIAuth, "_load", lambda _self: tokens)
    monkeypatch.setattr(model_capabilities, "time", lambda: 100.0)
    profile = (
        ProviderProfile(kind="openai", auth="chatgpt")
        if auth_mode == "chatgpt"
        else ProviderProfile(kind="foundry", auth="entra", api="responses", base_url="https://foundry.example/v1")
    )
    store = ScopedProviderRegistryStore(tmp_path)
    store.global_store.save(ProviderRegistry(active="shared", providers={"shared": profile}), expected="0" * 64)
    monkeypatch.setattr(
        runtime,
        "build_provider",
        lambda *_args: Provider(ProviderType.OPENAI_COMPATIBLE, "SECRET-fixture", model=MODEL),
    )

    calls = 0

    async def discover(name: str, *, scope: str) -> ModelCatalog:
        nonlocal tokens, calls
        calls += 1
        tokens = _Tokens("SECRET-fresh-access", "SECRET-refresh", "", 10_000.0)
        return ModelCatalog(
            [MODEL], {MODEL: {"reasoning_efforts": ["high"]}} if calls == 1 or advertised_after_refresh else {}
        )

    async def check() -> None:
        config = HarnessConfig(
            workspace=tmp_path, data_dir=tmp_path / "data", provider="shared", providers={"shared": profile}
        )
        async with client_app(tmp_path, config=config) as (_, client, headers, harnesses):
            discovery = AsyncMock(side_effect=discover)
            monkeypatch.setattr(harnesses[0], "provider_catalog", discovery)
            response = await client.get("/api/provider-scopes/global/providers/shared/models", headers=headers)
            assert response.status_code == 200
            global_settings = (await client.get("/api/settings/global", headers=headers)).json()
            saved = await client.post(
                "/api/settings/global",
                headers=headers,
                json={
                    "revision": global_settings["revision"],
                    "values": {**global_settings["values"], "model": MODEL, "reasoning_effort": "high"},
                },
            )
            assert saved.status_code == (200 if advertised_after_refresh else 422), saved.text
            if not advertised_after_refresh:
                assert "Refresh the model catalog" in saved.json()["detail"]
            assert discovery.await_count == 2, "Save must verify uncached or opaque credential choices"
            assert "SECRET" not in response.text + saved.text
            options = await client.get(
                "/api/reasoning-options",
                headers=headers,
                params={"scope": "global", "provider": "shared", "model": MODEL},
            )
            assert options.json()["options"] == (["high"] if advertised_after_refresh else [])
            assert discovery.await_count == 2, "Read-only options must stay local"

    asyncio.run(check())


@pytest.mark.parametrize("explicit_empty", [False, True])
def test_unknown_efforts_can_be_verified_but_explicit_empty_efforts_are_authoritative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, explicit_empty: bool
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "SECRET-fixture-key")

    async def check() -> None:
        config = HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "data")
        async with client_app(tmp_path, config=config) as (_, client, headers, harnesses):
            discovery = AsyncMock(
                side_effect=[
                    ModelCatalog([MODEL], {MODEL: {"reasoning_efforts": []}} if explicit_empty else {}),
                    ModelCatalog([MODEL], {MODEL: {"reasoning_efforts": ["high"]}}),
                ]
            )
            monkeypatch.setattr(harnesses[0], "provider_catalog", discovery)
            response = await client.get("/api/provider-scopes/workspace/providers/fixture/models", headers=headers)
            assert response.status_code == 200
            settings = (await client.get("/api/settings", headers=headers)).json()
            saved = await client.post(
                "/api/settings",
                headers=headers,
                json={
                    "revision": settings["revision"],
                    "values": {**settings["values"], "model": MODEL, "reasoning_effort": "high"},
                },
            )
            assert saved.status_code == (422 if explicit_empty else 200), saved.text
            assert discovery.await_count == (1 if explicit_empty else 2)

    asyncio.run(check())
