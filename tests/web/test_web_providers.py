"""Provider management API uses one HOME registry and never accepts key values."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import patch

import pytest

from nagents.harness.config import HarnessConfig
from nagents.harness.config import load_config
from nagents.harness.providers import ProviderProfile
from nagents.harness.providers import ProviderRegistry
from nagents.harness.providers import ProviderRegistryStore
from nagents.harness.providers import ScopedProviderRegistryStore
from nagents.harness.tools import CodingTools
from tests.support.config import connection
from tests.support.web import client_app

if TYPE_CHECKING:
    from pathlib import Path


def test_provider_crud_model_catalog_and_live_are_shared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("TEST_PROVIDER_KEY", "synthetic-private-key")
    monkeypatch.setattr(CodingTools, "instructions", Mock(return_value=""))
    monkeypatch.setattr(CodingTools, "discover_skills", Mock())

    async def check() -> None:
        config = HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "data")
        with caplog.at_level(logging.INFO, logger="uvicorn.error"):
            async with client_app(tmp_path, config=config, controlled=False) as (_, client, headers, harnesses):
                before = (await client.get("/api/providers", headers=headers)).json()
                assert before["active"] == "" and before["providers"] == {}
                profile = {
                    "kind": "openai",
                    "auth": "api-key",
                    "api": "responses",
                    "api_key_env": "${TEST_PROVIDER_KEY}",
                }
                invalid = await client.put(
                    "/api/provider-scopes/workspace/providers/work",
                    headers=headers,
                    json={
                        "revision": before["revision"],
                        "profile": {**profile, "api_key": "synthetic-private-key"},
                    },
                )
                assert invalid.status_code == 422
                coupled = await client.put(
                    "/api/provider-scopes/workspace/providers/work",
                    headers=headers,
                    json={"revision": before["revision"], "profile": {**profile, "model": "gpt-6-luna"}},
                )
                assert coupled.status_code == 422
                voice_coupled = await client.put(
                    "/api/provider-scopes/workspace/providers/work",
                    headers=headers,
                    json={"revision": before["revision"], "profile": {**profile, "live": {"enabled": True}}},
                )
                assert voice_coupled.status_code == 422
                result = await client.put(
                    "/api/provider-scopes/workspace/providers/work",
                    headers=headers,
                    json={
                        "revision": before["revision"],
                        "profile": profile,
                    },
                )
                assert result.status_code == 200, result.text
                data = result.json()
                assert data["active"] == "work" and data["providers"]["work"]["key_configured"]
                assert "model" not in data["providers"]["work"]
                assert data["providers"]["work"]["credential_source"] == "API key $TEST_PROVIDER_KEY"
                assert data["providers"]["work"]["effective_endpoint"] == "https://api.openai.com/v1"
                assert "synthetic-private-key" not in result.text
                store = ScopedProviderRegistryStore(tmp_path)
                assert data["scope"] == "workspace"
                assert data["origins"]["work"] == "workspace"
                assert "synthetic-private-key" not in store.workspace_store.path.read_text()
                document = json.loads(store.workspace_store.path.read_text())
                assert document["version"] == 2
                assert "model" not in document["providers"]["work"]
                assert "live" not in document["providers"]["work"]
                assert (await client.get("/api/provider-scopes/global/providers", headers=headers)).json()[
                    "providers"
                ] == {}
                stale = await client.put(
                    "/api/provider-scopes/workspace/providers/work",
                    headers=headers,
                    json={
                        "revision": before["revision"],
                        "profile": profile,
                    },
                )
                assert stale.status_code == 409
                assert harnesses[0].config.provider == "work"
                live = (await client.get("/api/live/settings", headers=headers)).json()
                assert live["source"] == "providers" and live["profile_name"] == "work"
                assert live["connection_scope"] == "workspace"
                assert live["values"]["model"] == "gpt-live-1"
                with patch.object(harnesses[0], "provider_models", new_callable=AsyncMock, return_value=["model-one"]):
                    models = await client.get("/api/providers/work/models", headers=headers)
                assert models.json() == {"models": ["model-one"], "source": "work"}
                assert "Model catalog requested: connection=work" in caplog.text
                assert "Model catalog completed: connection=work count=1" in caplog.text
                assert "providers={}" in caplog.text
                assert "synthetic-private-key" not in caplog.text

    asyncio.run(check())


def test_named_connection_cannot_take_credentials_from_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_BASE_KEY", "base-key")
    monkeypatch.delenv("TEST_SHARED_KEY", raising=False)
    monkeypatch.setattr(CodingTools, "instructions", Mock(return_value=""))
    monkeypatch.setattr(CodingTools, "discover_skills", Mock())

    async def check() -> None:
        config = HarnessConfig(
            tmp_path, data_dir=tmp_path / "data", providers=connection(auth="api-key", api_key_env="TEST_BASE_KEY")
        )
        async with client_app(tmp_path, config=config, controlled=False) as (_, client, headers, _):
            before = (await client.get("/api/settings", headers=headers)).json()
            values = {
                **before["values"],
                "provider": "openrouter",
                "auth": "api-key",
                "api": "auto",
                "base_url": "",
                "api_key_env": "TEST_SHARED_KEY",
            }
            rejected = await client.post(
                "/api/settings",
                headers=headers,
                json={"revision": before["revision"], "values": values, "api_key": "stored-test-key"},
            )
            assert rejected.status_code == 422
            assert "TEST_SHARED_KEY" not in os.environ

            registry = (await client.get("/api/providers", headers=headers)).json()
            named = await client.put(
                "/api/provider-scopes/workspace/providers/team",
                headers=headers,
                json={
                    "revision": registry["revision"],
                    "profile": {"kind": "openrouter", "auth": "api-key", "api_key_env": "TEST_SHARED_KEY"},
                },
            )
            assert named.status_code == 200, named.text
            assert "TEST_SHARED_KEY" not in os.environ
            assert named.json()["providers"]["team"]["key_configured"] is False
            assert (await client.get("/api/settings", headers=headers)).json()["connection"]["key_configured"] is False
            bootstrap = (await client.get("/api/bootstrap")).json()
            assert bootstrap["provider_setup"]["configured"] is False
            assert "stored-test-key" not in str(bootstrap)

    asyncio.run(check())


@pytest.mark.parametrize(
    "profile",
    [
        ProviderProfile(kind="openai", auth="codex"),
        ProviderProfile(
            kind="foundry",
            auth="entra",
            api="responses",
            base_url="https://example.openai.azure.com/openai/v1",
        ),
    ],
)
def test_named_auth_can_initialize_web_settings(tmp_path: Path, profile: ProviderProfile) -> None:
    ProviderRegistryStore().save(
        ProviderRegistry(active="selected", providers={"selected": profile}), expected="0" * 64
    )

    async def check() -> None:
        config = load_config(tmp_path)
        config.demo = True
        config.data_dir = tmp_path / "data"
        async with client_app(tmp_path, config=config) as (_, client, headers, _):
            result = await client.get("/api/settings", headers=headers)
            assert result.status_code == 200, result.text
            assert result.json()["values"]["auth"] == profile.auth

    asyncio.run(check())


def test_global_connections_are_inherited_or_selected_per_workspace(tmp_path: Path) -> None:
    async def check() -> None:
        config = HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "data", demo=True)
        async with client_app(tmp_path, config=config) as (_, client, headers, harnesses):
            store = ScopedProviderRegistryStore(tmp_path)
            global_before = (await client.get("/api/provider-scopes/global/providers", headers=headers)).json()
            profile = {"kind": "openai", "auth": "codex"}
            added = await client.put(
                "/api/provider-scopes/global/providers/personal",
                headers=headers,
                json={
                    "revision": global_before["revision"],
                    "profile": profile,
                },
            )
            assert added.status_code == 200, added.text
            assert added.json()["active"] == "personal"
            inherited = (await client.get("/api/providers", headers=headers)).json()
            assert inherited["active"] == "personal" and inherited["inherited_active"]
            assert inherited["origins"] == {"personal": "global"}
            assert not store.workspace_store.path.exists()
            assert store.global_store.path.is_file()
            live_before = (await client.get("/api/live/settings", headers=headers)).json()
            selected = await client.post(
                "/api/provider-scopes/workspace/providers/personal/activate",
                headers=headers,
                json={
                    "revision": inherited["revision"],
                },
            )
            assert selected.status_code == 200 and not selected.json()["inherited_active"]
            live_selected = (await client.get("/api/live/settings", headers=headers)).json()
            assert live_selected["revision"] != live_before["revision"]
            stale_live = await client.post(
                "/api/live/settings",
                headers=headers,
                json={
                    "scope": "workspace",
                    "revision": live_before["revision"],
                    "overrides": {},
                },
            )
            assert stale_live.status_code == 409
            local = await client.put(
                "/api/provider-scopes/workspace/providers/local",
                headers=headers,
                json={
                    "revision": selected.json()["revision"],
                    "profile": {"kind": "anthropic", "auth": "api-key"},
                },
            )
            assert local.status_code == 200, local.text
            assert local.json()["origins"]["local"] == "workspace"
            assert (
                await client.put(
                    "/api/provider-scopes/workspace/providers/personal",
                    headers=headers,
                    json={
                        "revision": local.json()["revision"],
                        "profile": profile,
                    },
                )
            ).status_code == 409
            active = await client.post(
                "/api/provider-scopes/workspace/providers/local/activate",
                headers=headers,
                json={
                    "revision": local.json()["revision"],
                },
            )
            assert active.status_code == 200, active.text
            assert harnesses[0].config.provider == "local"
            assert (await client.get("/api/live/settings", headers=headers)).json()["connection_scope"] == "workspace"
            restored = await client.post(
                "/api/providers/inherit",
                headers=headers,
                json={
                    "revision": active.json()["revision"],
                },
            )
            assert restored.status_code == 200, restored.text
            assert restored.json()["inherited_active"] and restored.json()["active"] == "personal"
            assert harnesses[0].config.provider == "personal"
            assert (await client.get("/api/live/settings", headers=headers)).json()["connection_scope"] == "global"
            assert (
                await client.post(
                    "/api/providers/import", headers=headers, json={"revision": restored.json()["revision"]}
                )
            ).status_code != 200

    asyncio.run(check())
