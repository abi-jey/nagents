"""Reasoning preferences use saved scopes and read-only capability metadata."""

import asyncio
from pathlib import Path
from unittest.mock import Mock

import pytest

from nagents.harness import Harness
from nagents.harness.config import HarnessConfig
from nagents.harness.providers.registry import ProviderProfile
from nagents.harness.providers.registry import ProviderRegistry
from nagents.harness.providers.registry import ScopedProviderRegistryStore
from nagents.web.settings.preferences import WebSettings
from tests.support.web import client_app
from tests.support.web import no_guarded_workspace_io as no_guarded_workspace_io


def test_reasoning_preferences_persist_inherit_clear_and_reset(tmp_path: Path) -> None:
    async def check() -> None:
        async with client_app(tmp_path) as (_, client, headers, harnesses):
            global_values = (await client.get("/api/settings/global", headers=headers)).json()
            saved = await client.post(
                "/api/settings/global",
                headers=headers,
                json={
                    "revision": global_values["revision"],
                    "values": {**global_values["values"], "reasoning_effort": "high"},
                },
            )
            assert saved.status_code == 200, saved.text
            local = (await client.get("/api/settings", headers=headers)).json()
            assert local["values"]["reasoning_effort"] == "high"
            cleared = await client.post(
                "/api/settings",
                headers=headers,
                json={
                    "revision": local["revision"],
                    "values": {**local["values"], "reasoning_effort": ""},
                },
            )
            assert cleared.status_code == 200, cleared.text
            assert harnesses[-1].reasoning_effort() == ""
        async with client_app(tmp_path) as (_, client, headers, harnesses):
            local = (await client.get("/api/settings", headers=headers)).json()
            assert local["values"]["reasoning_effort"] == ""
            assert local["defaults"]["reasoning_effort"] == "high"
            reset = await client.post("/api/settings/reset", headers=headers, json={"revision": local["revision"]})
            assert reset.status_code == 200, reset.text
            assert reset.json()["values"]["reasoning_effort"] == "high"
            invalid = await client.post(
                "/api/settings",
                headers=headers,
                json={
                    "revision": reset.json()["revision"],
                    "values": {**reset.json()["values"], "reasoning_effort": "turbo"},
                },
            )
            assert invalid.status_code == 422

    asyncio.run(check())


def test_capability_scope_and_accepted_route_do_not_leak_cached_choices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    global_route = ProviderProfile(kind="openai", auth="api-key")
    workspace_route = ProviderProfile(kind="gemini", auth="api-key")
    global_registry = ProviderRegistry(active="same", providers={"same": global_route})
    local_registry = ProviderRegistry(active="same", providers={"same": workspace_route})
    monkeypatch.setattr(ScopedProviderRegistryStore, "load", lambda _store: local_registry)
    monkeypatch.setattr(ScopedProviderRegistryStore, "load_scope", lambda _store, _scope: global_registry)

    async def check() -> None:
        harness = Harness(
            HarnessConfig(
                workspace=tmp_path,
                demo=True,
                provider="same",
                providers={"same": workspace_route},
                model="gemini-3-pro-preview",
            )
        )
        cached = Mock(return_value=("high",))
        monkeypatch.setattr(harness.agent.provider, "cached_reasoning_efforts", cached)
        try:
            settings = WebSettings(harness)
            local = settings.reasoning_options(provider="same", model="gemini-3-pro-preview")
            assert local["options"] == ["high"]
            cached.assert_called_once_with("gemini-3-pro-preview")
            cached.reset_mock()
            shared = settings.reasoning_options(scope="global", provider="same", model="gpt-5")
            assert isinstance(shared["options"], list)
            assert "medium" in shared["options"]
            cached.assert_not_called()
            # Mutable configuration can lead the client during connection publication.
            harness.config.providers["same"] = global_route
            settings.reasoning_options(scope="global", provider="same", model="gpt-5")
            cached.assert_not_called()
            with pytest.raises(ValueError, match="unavailable"):
                settings.reasoning_options(scope="global", provider="missing", model="gpt-5")
        finally:
            await harness.close()

    asyncio.run(check())


def test_capabilities_endpoint_is_read_only_and_bounded(tmp_path: Path) -> None:
    async def check() -> None:
        async with client_app(tmp_path) as (_, client, headers, _):
            response = await client.get("/api/reasoning-options", headers=headers, params={"model": "gpt-5"})
            assert response.status_code == 200
            assert "medium" in response.json()["options"] and "" not in response.json()["options"]
            unknown = await client.get("/api/reasoning-options", headers=headers, params={"model": "custom-model"})
            assert unknown.json()["options"] == []
            huge = await client.get("/api/reasoning-options", headers=headers, params={"model": "x" * 201})
            assert huge.status_code == 422
            for query in (
                "model=a&model=b",
                "token=secret",
                "model=%ZZ",
                "model=%00",
                "model=%FF",
                "kind=openai",
                "api=auto",
                "auth=api-key",
                "base_url=https://example.com",
            ):
                invalid = await client.get(f"/api/reasoning-options?{query}", headers=headers)
                assert invalid.status_code == 400
            denied = await client.post("/api/reasoning-options?model=gpt-5", headers=headers)
            assert denied.status_code == 400

    asyncio.run(check())
