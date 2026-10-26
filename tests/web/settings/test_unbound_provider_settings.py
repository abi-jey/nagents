"""Unbound startup model preferences follow the active provider connection."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from nagents.harness.config import HarnessConfig
from nagents.harness.model_bindings import ModelBinding
from nagents.harness.providers import ProviderProfile
from nagents.harness.providers import ProviderRegistry
from nagents.harness.providers import ScopedProviderRegistryStore
from tests.support.web import client_app

if TYPE_CHECKING:
    from pathlib import Path


def test_global_settings_follow_active_provider_without_saved_model_pair(tmp_path: Path) -> None:
    async def scenario() -> None:
        store = ScopedProviderRegistryStore(tmp_path)
        providers = {
            "first": ProviderProfile(kind="openai", auth="api-key"),
            "second": ProviderProfile(kind="anthropic", auth="api-key"),
        }
        store.global_store.save(ProviderRegistry(active="first", providers=providers), expected="0" * 64)
        config = HarnessConfig(
            workspace=tmp_path,
            data_dir=tmp_path / "data",
            demo=True,
            model="startup-model",
            provider="first",
            providers=providers,
        )
        assert not config.model_config_explicit
        assert not store.model_store("global").path.exists()

        async with client_app(tmp_path, config=config) as (_, client, headers, _):
            initial = (await client.get("/api/settings/global", headers=headers)).json()
            assert initial["model_binding"] == {"provider": "first", "id": "startup-model"}

            registry = (await client.get("/api/provider-scopes/global/providers", headers=headers)).json()
            activated = await client.post(
                "/api/provider-scopes/global/providers/second/activate",
                headers=headers,
                json={"revision": registry["revision"]},
            )
            assert activated.status_code == 200, activated.text

            current = (await client.get("/api/settings/global", headers=headers)).json()
            assert current["model_binding"] == {"provider": "second", "id": "startup-model"}
            changed = await client.post(
                "/api/settings/global",
                headers=headers,
                json={
                    "revision": current["revision"],
                    "values": {**current["values"], "model": "second-model"},
                },
            )
            assert changed.status_code == 200, changed.text
            assert changed.json()["model_binding"] == {"provider": "second", "id": "second-model"}
            assert store.model_binding("global") == ModelBinding("second", "second-model")

    asyncio.run(scenario())
