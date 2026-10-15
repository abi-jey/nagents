"""Catalog browsing keeps scope, public metadata, and saved routing separate."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from unittest.mock import Mock

import pytest

from nagents.harness import runtime
from nagents.harness.config import HarnessConfig
from nagents.harness.providers import ProviderProfile
from nagents.harness.providers import ProviderRegistry
from nagents.harness.providers import ScopedProviderRegistryStore
from nagents.provider import Provider
from nagents.provider import ProviderType
from tests.support.web import client_app

if TYPE_CHECKING:
    from pathlib import Path

    from nagents.harness.auth import OpenAIAuth
    from nagents.provider.context import ContextMetadata


class CatalogProvider(Provider):
    def __init__(self, profile: ProviderProfile) -> None:
        super().__init__(
            ProviderType.OPENAI_COMPATIBLE,
            api_key="SECRET-catalog-key",
            model="exact",
            base_url=profile.base_url,
        )
        self.catalog_calls = 0
        self.snapshots = 0
        self.closed = False

    async def get_model_list(self) -> list[str]:
        self.catalog_calls += 1
        self._context_metadata.select(self._context_metadata_identity())
        self._context_metadata.limits = {"exact": 111_000, "advertised": 800_000, "hidden": 999_000}
        self._context_metadata.reasoning_efforts = {
            "exact": ("low", "high"),
            "no-reasoning": (),
            "hidden": ("max",),
        }
        self._context_metadata.retry_at = {"SECRET-private-metadata": 999.0}
        return ["exact", "advertised", "unknown", "no-reasoning"]

    def context_metadata_snapshot(self) -> ContextMetadata:
        self.snapshots += 1
        return super().context_metadata_snapshot()

    async def close(self) -> None:
        self.closed = True
        await super().close()


@pytest.mark.parametrize("scope", ["global", "workspace"])
@pytest.mark.parametrize("connection_limit", [0, 222_000])
def test_scoped_catalog_uses_exact_route_and_public_metadata_without_changing_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: str, connection_limit: int
) -> None:
    store = ScopedProviderRegistryStore(tmp_path)
    profiles = {
        selected: ProviderProfile(
            kind="openai_compatible",
            base_url=f"https://{selected}.example/v1",
            api_key_env=f"{selected.upper()}_KEY",
            context_window=connection_limit,
            model_context_windows={"exact": 333_000, "hidden": 444_000},
        )
        for selected in ("global", "workspace")
    }
    for selected, profile in profiles.items():
        store.store(selected).save(ProviderRegistry(active="shared", providers={"shared": profile}), expected="0" * 64)
    built: list[CatalogProvider] = []

    def build(profile: ProviderProfile, candidate: HarnessConfig, auth: OpenAIAuth) -> CatalogProvider:
        assert profile == profiles[scope]
        assert candidate.provider == "shared"
        assert candidate.provider_profile() == profile
        provider = CatalogProvider(profile)
        built.append(provider)
        return provider

    async def check() -> None:
        config = HarnessConfig(
            workspace=tmp_path,
            data_dir=tmp_path / "data",
            provider="shared",
            providers=dict(store.load().providers),
        )
        async with client_app(tmp_path, config=config) as (_, client, headers, harnesses):
            harness = harnesses[0]
            saved_files = {path: path.read_bytes() for path in (store.global_store.path, store.workspace_store.path)}
            before = (await client.get("/api/settings", headers=headers)).json()
            accepted_provider = harness.agent.provider
            restore_metadata = Mock(wraps=accepted_provider.restore_context_metadata)
            monkeypatch.setattr(accepted_provider, "restore_context_metadata", restore_metadata)
            monkeypatch.setattr(runtime, "build_provider", build)
            extra_context = AsyncMock(side_effect=AssertionError("Catalog must not fetch context metadata again"))
            extra_reasoning = AsyncMock(side_effect=AssertionError("Catalog must not fetch reasoning metadata again"))
            monkeypatch.setattr(Provider, "resolve_context_window", extra_context)
            monkeypatch.setattr(Provider, "resolve_reasoning_efforts", extra_reasoning)
            response = await client.get(f"/api/provider-scopes/{scope}/providers/shared/models", headers=headers)
            assert response.status_code == 200, response.text
            metadata: dict[str, object] = {
                "exact": {
                    "context_window": 333_000,
                    "context_window_source": "configured",
                    "reasoning_efforts": ["low", "high"],
                },
                "advertised": {
                    "context_window": connection_limit or 800_000,
                    "context_window_source": "configured" if connection_limit else "provider",
                },
                "no-reasoning": {"reasoning_efforts": []},
            }
            if connection_limit:
                window = {"context_window": connection_limit, "context_window_source": "configured"}
                metadata["unknown"] = window
                metadata["no-reasoning"] = {**window, "reasoning_efforts": []}
            assert response.json() == {
                "models": ["exact", "advertised", "unknown", "no-reasoning"],
                "source": "shared",
                "scope": scope,
                "metadata": metadata,
            }
            assert "SECRET" not in response.text and "hidden" not in response.text
            assert len(built) == 1
            assert built[0].catalog_calls == 1 and built[0].snapshots == 1 and built[0].closed
            assert restore_metadata.call_count == (1 if scope == "workspace" else 0)
            assert harness.agent.provider is accepted_provider
            assert harness.config.provider == "shared"
            assert harness.config.provider_profile() == profiles["workspace"]
            assert (await client.get("/api/settings", headers=headers)).json() == before
            assert all(path.read_bytes() == original for path, original in saved_files.items())
            extra_context.assert_not_called()
            extra_reasoning.assert_not_called()
            # The public list API remains a list and uses the requested scope.
            assert await harness.provider_models("shared", scope=scope) == response.json()["models"]
            assert len(built) == 2 and built[1].catalog_calls == 1 and built[1].snapshots == 1 and built[1].closed
            assert restore_metadata.call_count == (2 if scope == "workspace" else 0)

    asyncio.run(check())


def test_scoped_catalog_rejects_unknown_scopes_and_connections_without_building_a_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def check() -> None:
        config = HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "data")
        async with client_app(tmp_path, config=config) as (_, client, headers, harnesses):
            builder = Mock(side_effect=AssertionError("Invalid selectors must not build a provider"))
            monkeypatch.setattr(runtime, "build_provider", builder)
            for scope, name in (("assistant", "fixture"), ("global", "fixture"), ("workspace", "missing")):
                response = await client.get(f"/api/provider-scopes/{scope}/providers/{name}/models", headers=headers)
                assert response.status_code == 422, response.text
            with pytest.raises(ValueError, match="scope"):
                await harnesses[0].provider_catalog("fixture", scope="assistant")
            builder.assert_not_called()

    asyncio.run(check())


def test_scoped_catalog_closes_temporary_provider_when_discovery_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def check() -> None:
        config = HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "data")
        async with client_app(tmp_path, config=config) as (_, client, headers, _):
            provider = CatalogProvider(ProviderProfile(kind="openai_compatible", base_url="https://failed.example/v1"))
            monkeypatch.setattr(runtime, "build_provider", lambda *_args: provider)
            failed = AsyncMock(side_effect=RuntimeError("SECRET-upstream-body"))
            monkeypatch.setattr(provider, "get_model_list", failed)
            response = await client.get("/api/provider-scopes/workspace/providers/fixture/models", headers=headers)
            assert response.status_code == 502
            assert "SECRET" not in response.text
            failed.assert_awaited_once()
            assert provider.closed and provider.snapshots == 0

    asyncio.run(check())


@pytest.mark.parametrize("failure,status", [(NotImplementedError, 501), (ValueError, 422), (RuntimeError, 502)])
def test_scoped_catalog_keeps_provider_failures_out_of_browser_and_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: type[Exception],
    status: int,
) -> None:
    async def check() -> None:
        config = HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "data")
        with caplog.at_level(logging.INFO, logger="uvicorn.error"):
            async with client_app(tmp_path, config=config) as (_, client, headers, harnesses):
                catalog = AsyncMock(side_effect=failure("SECRET-upstream-body"))
                monkeypatch.setattr(harnesses[0], "provider_catalog", catalog)
                response = await client.get("/api/provider-scopes/workspace/providers/fixture/models", headers=headers)
                assert response.status_code == status
                catalog.assert_awaited_once_with("fixture", scope="workspace")
                assert "SECRET" not in response.text + caplog.text
                assert "Model catalog requested: scope=workspace connection=fixture" in caplog.text

    asyncio.run(check())
