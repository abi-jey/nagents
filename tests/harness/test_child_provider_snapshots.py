"""Active task trees retain accepted routing across operator settings edits."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from nagents.harness import runtime
from nagents.harness.config import AgentProfile
from nagents.harness.config import HarnessConfig
from nagents.harness.provider import HarnessProvider
from nagents.harness.providers import ProviderProfile
from nagents.harness.providers import ProviderRegistry
from nagents.harness.providers import ScopedProviderRegistryStore
from nagents.harness.runtime import Harness
from nagents.harness.subagents import TaskInfo
from nagents.provider.openai import OpenAIProvider

if TYPE_CHECKING:
    from pathlib import Path

    from nagents.harness.auth import OpenAIAuth


class RecordedProvider(HarnessProvider):
    def __init__(self, profile: ProviderProfile, config: HarnessConfig, auth: OpenAIAuth) -> None:
        super().__init__(config)
        self.accepted_profile = profile
        self.accepted_auth = auth
        self.closed = False

    async def close(self) -> None:
        self.closed = True
        await super().close()


@pytest.mark.parametrize("removed", [False, True])
@pytest.mark.parametrize("agent", ["assistant", "auditor"])
def test_children_and_retained_followups_use_accepted_provider_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, removed: bool, agent: str
) -> None:
    profiles = {
        "main": ProviderProfile(
            kind="openai_compatible", base_url="https://original.example/v1", api_key_env="ORIGINAL_KEY"
        ),
        "audit": ProviderProfile(
            kind="openai_compatible", base_url="https://audit.example/v1", api_key_env="AUDIT_KEY"
        ),
    }
    registry = ProviderRegistry(active="main", providers=profiles)
    monkeypatch.setattr(ScopedProviderRegistryStore, "load", lambda _store: registry)
    built: list[RecordedProvider] = []

    def build(profile: ProviderProfile, config: HarnessConfig, auth: OpenAIAuth) -> RecordedProvider:
        provider = RecordedProvider(profile, config, auth)
        built.append(provider)
        return provider

    monkeypatch.setattr(runtime, "build_provider", build)

    async def check() -> None:
        nonlocal registry
        parent = Harness(
            HarnessConfig(
                workspace=tmp_path,
                data_dir=tmp_path / "state",
                provider="main",
                providers=dict(profiles),
                model="original-model",
                profiles={"auditor": AgentProfile(mode="reviewer", provider="audit", model="audit-model")},
            )
        )
        auth_close = AsyncMock(wraps=parent.openai_auth.close)
        monkeypatch.setattr(parent.openai_auth, "close", auth_close)
        children: list[Harness] = []
        fresh: list[Harness] = []
        try:
            info = TaskInfo(
                "s1",
                "Original child",
                profile=agent,
                root_session_id=parent.session_id,
                _storage_id="retained-session",
            )
            retained = parent.tasks._create_child(agent, info)
            children.append(retained)
            parent.tasks._children[info.session_id] = retained
            await retained.close()
            # Simulate another configuration owner editing or deleting routes.
            changed = {
                name: replace(profile, base_url=f"https://new-{name}.example/v1", api_key_env="NEW_KEY")
                for name, profile in profiles.items()
            }
            registry = ProviderRegistry(
                active="replacement" if removed else "main",
                providers={
                    **({"replacement": changed["main"]} if removed else changed),
                    "newly-saved": ProviderProfile(kind="openai", auth="api-key"),
                },
            )
            late = parent.tasks._create_child(agent)
            children.append(late)
            resumed = parent.tasks._create_child(agent, info, continuing=True)
            children.append(resumed)
            expected = profiles["audit" if agent == "auditor" else "main"]
            expected_model = "audit-model" if agent == "auditor" else "original-model"
            for child, provider in zip(children, built[1:], strict=True):
                accepted_provider = child.agent.provider
                child.activate_agent_definitions()
                assert child.providers.providers == registry.providers
                assert child.agent.provider is accepted_provider
                assert provider.accepted_profile == expected
                assert child.config.provider_profile() == expected
                assert provider.model == expected_model
                assert provider.accepted_auth is parent.openai_auth
                assert child.openai_auth is parent.openai_auth and not child._owns_auth
                assert child.mode == ("reviewer" if agent == "auditor" else "build")
            assert resumed.session_id == retained.session_id
            assert parent.config.providers == profiles
            # The next independent root still loads current settings by default.
            next_root = Harness(
                replace(
                    parent.config,
                    provider=registry.active,
                    providers=dict(registry.providers),
                    model="next-root-model",
                )
            )
            fresh.append(next_root)
            assert built[-1].accepted_profile == changed["main"]
            assert built[-1].model == "next-root-model"
            assert next_root.providers.providers == registry.providers
            assert next_root.openai_auth is not parent.openai_auth
            assert not built[0].closed
        finally:
            for child in children:
                await child.close()
            for root in fresh:
                await root.close()
            auth_close.assert_not_awaited()
            assert not built[0].closed
            await parent.close()
            auth_close.assert_awaited_once()
            assert built[0].closed

    asyncio.run(check())


def test_named_chatgpt_child_binds_shared_auth_before_provider_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = ProviderProfile(kind="openai", auth="chatgpt")
    registry = ProviderRegistry(active="main", providers={"main": profile})
    monkeypatch.setattr(ScopedProviderRegistryStore, "load", lambda _store: registry)

    async def check() -> None:
        parent = Harness(
            HarnessConfig(
                workspace=tmp_path,
                data_dir=tmp_path / "state",
                provider="main",
                providers={"main": profile},
                model="accepted-model",
            )
        )
        child = parent.tasks._create_child("assistant")
        try:
            assert isinstance(child.agent.provider, OpenAIProvider)
            assert child.agent.provider is not parent.agent.provider
            assert child.agent.provider._credentials == parent.openai_auth.credentials
            assert child.agent.provider.model == "accepted-model"
            assert child.openai_auth is parent.openai_auth and not child._owns_auth
        finally:
            await child.close()
            await parent.close()

    asyncio.run(check())
