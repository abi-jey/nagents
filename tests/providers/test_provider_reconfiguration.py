"""Saved registry changes must replace the client that was built from old fields."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import aiohttp
import pytest

from nagents.harness import Harness
from nagents.harness.config import HarnessConfig
from nagents.harness.providers import ProviderProfile
from nagents.harness.providers import ProviderRegistry
from nagents.harness.providers import ScopedProviderRegistryStore
from nagents.provider import OpenAIProvider

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    "changed",
    [
        ProviderProfile(kind="openai", auth="chatgpt"),
        ProviderProfile(kind="openai", api="responses"),
        ProviderProfile(kind="openai", api_key_env="OTHER_PROVIDER_KEY"),
        ProviderProfile(kind="anthropic"),
        ProviderProfile(kind="openai_compatible", base_url="https://changed.invalid/v1"),
    ],
)
def test_save_selected_profile_rebuilds_auth_api_key_source_kind_and_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: ProviderProfile
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setattr(aiohttp.ClientSession, "_request", AsyncMock(side_effect=AssertionError("No network calls")))
    profile = ProviderProfile(kind="openai", auth="api-key")
    store = ScopedProviderRegistryStore(tmp_path)
    saved = store.workspace_store.save(ProviderRegistry(active="main", providers={"main": profile}), expected="0" * 64)

    async def check() -> None:
        harness = Harness(
            HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "state", provider="main", providers={"main": profile})
        )
        previous = harness.agent.provider
        close = AsyncMock(wraps=previous.close)
        monkeypatch.setattr(previous, "close", close)
        session_id, session_owner, approvals = harness.session_id, harness.agent.session, harness.approval_handler
        try:
            after = await harness.save_provider("main", changed, saved.revision)
            assert harness.agent.provider is not previous
            close.assert_awaited_once()
            assert harness._built_provider_profile == changed
            assert harness.config.provider_profile() == changed
            assert harness.session_id == session_id and harness.agent.session is session_owner
            assert harness.approval_handler is approvals
            if changed.auth == "chatgpt":
                assert isinstance(harness.agent.provider, OpenAIProvider)
                assert harness.agent.provider.uses_chatgpt_auth
            current = harness.agent.provider
            # An unchanged save and a model-only change reuse the new client.
            await harness.save_provider("main", changed, after.revision)
            await harness.reconfigure_provider(replace(harness.config, model="other-model"))
            assert harness.agent.provider is current
            assert current.model == "other-model"
        finally:
            await harness.close()

    asyncio.run(check())
