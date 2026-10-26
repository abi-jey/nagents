"""Settings persist model/connection pairs independently of connection activation."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from nagents.harness.config import AgentProfile
from nagents.harness.config import HarnessConfig
from nagents.harness.providers.model_bindings import ModelBinding
from nagents.harness.providers.registry import ProviderProfile
from nagents.harness.providers.registry import ProviderRegistry
from nagents.harness.providers.registry import ScopedProviderRegistryStore
from tests.support.web import client_app
from tests.support.web import no_guarded_workspace_io as no_guarded_workspace_io

if TYPE_CHECKING:
    from pathlib import Path


def configuration(workspace: Path) -> HarnessConfig:
    store = ScopedProviderRegistryStore(workspace)
    home = ProviderProfile(kind="openai", auth="api-key")
    work = ProviderProfile(kind="openai_compatible", auth="api-key", base_url="https://workspace.example.test/v1")
    store.global_store.save(ProviderRegistry(active="home", providers={"home": home}), expected="0" * 64)
    store.workspace_store.save(ProviderRegistry(providers={"work": work}), expected="0" * 64)
    store.model_store("global").save_binding(ModelBinding("home", "global-model"))
    return HarnessConfig(
        workspace,
        data_dir=workspace / "data",
        demo=True,
        model="global-model",
        provider="home",
        providers={"home": home, "work": work},
        profiles={"mini": AgentProfile(description="A helper")},
    )


def test_workspace_pair_persists_restarts_and_resets_to_global(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = configuration(tmp_path)
        store = ScopedProviderRegistryStore(tmp_path)
        async with client_app(tmp_path, config=config) as (app, client, headers, _):
            initial = (await client.get("/api/settings", headers=headers)).json()
            assert initial["model_binding"] == {"provider": "home", "id": "global-model"}
            assert initial["model_binding_source"] == "global"
            saved = await client.post(
                "/api/settings",
                headers=headers,
                json={
                    "revision": initial["revision"],
                    "values": {**initial["values"], "model": "workspace-model"},
                    "model_binding": {"provider": "work", "id": "workspace-model"},
                },
            )
            assert saved.status_code == 200, saved.text
            snapshot = saved.json()
            assert snapshot["model_binding"] == {"provider": "work", "id": "workspace-model"}
            assert snapshot["default_model_binding"] == initial["model_binding"]
            assert snapshot["model_binding_source"] == "workspace"
            assert snapshot["provider_origin"] == "workspace"
            assert store.model_binding("workspace") == ModelBinding("work", "workspace-model")
            assert store.load().active == "home"
            assert app.state.web.harness.config.provider == "work"
        restart = HarnessConfig(tmp_path, data_dir=tmp_path / "data", demo=True)
        async with client_app(tmp_path, config=restart) as (app, client, headers, _):
            reopened = (await client.get("/api/settings", headers=headers)).json()
            assert reopened["model_binding"] == {"provider": "work", "id": "workspace-model"}
            assert reopened["values"]["model"] == "workspace-model"
            reset = await client.post("/api/settings/reset", headers=headers, json={"revision": reopened["revision"]})
            assert reset.status_code == 200, reset.text
            assert reset.json()["model_binding"] == {"provider": "home", "id": "global-model"}
            assert reset.json()["model_binding_source"] == "global"
            assert app.state.web.harness.config.provider == "home"

    asyncio.run(scenario())


def test_global_pair_can_use_workspace_connection_and_assistant_override_remains_highest(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = configuration(tmp_path)
        store = ScopedProviderRegistryStore(tmp_path)
        store.agent_models.save("assistant", ModelBinding("home", "assistant-model"), expected="0" * 64)
        async with client_app(tmp_path, config=config) as (app, client, headers, _):
            initial = (await client.get("/api/settings/global", headers=headers)).json()
            saved = await client.post(
                "/api/settings/global",
                headers=headers,
                json={
                    "revision": initial["revision"],
                    "values": {**initial["values"], "model": "new-global-model"},
                    "model_binding": {"provider": "work", "id": "new-global-model"},
                },
            )
            assert saved.status_code == 200, saved.text
            assert saved.json()["model_binding"] == {"provider": "work", "id": "new-global-model"}
            assert saved.json()["provider_origin"] == "workspace"
            assert store.model_binding("global") == ModelBinding("work", "new-global-model")
            assert store.load().active == "home"
            workspace = (await client.get("/api/settings", headers=headers)).json()
            assert workspace["values"]["model"] == "new-global-model"
            assert workspace["model_binding"] == saved.json()["model_binding"]
            assert workspace["effective_model"] == "assistant-model"
            assert (app.state.web.harness.config.provider, app.state.web.harness.agent.provider.model) == (
                "home",
                "assistant-model",
            )

    asyncio.run(scenario())


def test_unknown_mismatched_and_stale_binding_requests_do_not_write_preferences(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = configuration(tmp_path)
        store = ScopedProviderRegistryStore(tmp_path)
        async with client_app(tmp_path, config=config) as (_, client, headers, _):
            before = (await client.get("/api/settings", headers=headers)).json()
            global_bytes = store.model_store("global").path.read_bytes()
            for provider, identifier, revision, status in (
                ("missing", "candidate", before["revision"], 422),
                ("work", "different-id", before["revision"], 422),
                ("work", "candidate", "0" * 64, 409),
            ):
                response = await client.post(
                    "/api/settings",
                    headers=headers,
                    json={
                        "revision": revision,
                        "values": {**before["values"], "model": "candidate"},
                        "model_binding": {"provider": provider, "id": identifier},
                    },
                )
                assert response.status_code == status, response.text
                assert (await client.get("/api/settings", headers=headers)).json() == before
                assert not store.model_store("workspace").path.exists()
                assert store.model_store("global").path.read_bytes() == global_bytes

    asyncio.run(scenario())


def test_global_binding_uses_merged_provider_identity_for_same_id(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = configuration(tmp_path)
        store = ScopedProviderRegistryStore(tmp_path)
        local = store.workspace_store.load()
        overridden = ProviderProfile(
            kind="openai_compatible", auth="api-key", base_url="https://override.example.test/v1"
        )
        store.workspace_store.save(
            ProviderRegistry(active=local.active, providers={**local.providers, "home": overridden}),
            expected=local.revision,
        )
        async with client_app(tmp_path, config=config) as (app, client, headers, _):
            initial = (await client.get("/api/settings/global", headers=headers)).json()
            saved = await client.post(
                "/api/settings/global",
                headers=headers,
                json={
                    "revision": initial["revision"],
                    "values": {**initial["values"], "model": "same-name-model"},
                    "model_binding": {"provider": "home", "id": "same-name-model"},
                },
            )
            assert saved.status_code == 200, saved.text
            assert saved.json()["provider_origin"] == "workspace"
            assert app.state.web.harness.config.provider_profile().base_url == overridden.base_url

    asyncio.run(scenario())


def test_removed_default_profile_does_not_break_settings_snapshot(tmp_path: Path) -> None:
    async def scenario() -> None:
        config = configuration(tmp_path)
        async with client_app(tmp_path, config=config) as (app, client, headers, _):
            initial = (await client.get("/api/settings", headers=headers)).json()
            selected = await client.post(
                "/api/settings",
                headers=headers,
                json={
                    "revision": initial["revision"],
                    "values": {**initial["values"], "agent": "mini"},
                },
            )
            assert selected.status_code == 200, selected.text
            app.state.web.harness.config.profiles.pop("mini")
            app.state.web.harness.refresh_agent_definitions()
            snapshot = await client.get("/api/settings", headers=headers)
            assert snapshot.status_code == 200, snapshot.text
            assert "mini" not in [profile["name"] for profile in snapshot.json()["profiles"]]
            changed = await client.post(
                "/api/settings",
                headers=headers,
                json={
                    "revision": snapshot.json()["revision"],
                    "values": {**snapshot.json()["values"], "agent": "assistant"},
                },
            )
            assert changed.status_code == 200, changed.text

    asyncio.run(scenario())
