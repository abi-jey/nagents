"""Auxiliary operations retain the accepted named OpenAI environment source."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from nagents.events import TextDoneEvent
from nagents.harness import Harness
from nagents.harness.config import HarnessConfig
from nagents.harness.providers.connection import _EnvOpenAIProvider
from nagents.harness.providers.registry import ProviderProfile
from nagents.provider import OpenAIProvider
from nagents.provider import Provider
from nagents.types import Message
from nagents.web.settings.provider_setup import provider_setup

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from nagents.events import Event
    from nagents.types import GenerationConfig
    from nagents.types import ToolDefinition

pytestmark = pytest.mark.usefixtures("workspace_discovery_only")


@pytest.mark.asyncio
@pytest.mark.parametrize("auth", ["api-key", "auto"])
@pytest.mark.parametrize("rotated", [False, True])
async def test_title_resolves_current_named_key_without_auth_rediscovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, auth: str, rotated: bool
) -> None:
    profile = ProviderProfile(kind="openai", auth=auth, api_key_env="TITLE_NAMED_KEY")
    harness = Harness(
        HarnessConfig(
            workspace=tmp_path,
            data_dir=tmp_path / "state",
            provider="named",
            providers={"named": profile},
            model="gpt-4o",
            demo=True,
        )
    )
    await harness.initialize()
    active = _EnvOpenAIProvider(profile, "gpt-4o")
    await harness.agent.provider.close()
    harness.agent.provider = active
    harness.config.demo = False
    monkeypatch.setenv("TITLE_NAMED_KEY", "first-fixture-key")
    if rotated:
        active.credentials()
        monkeypatch.setenv("TITLE_NAMED_KEY", "rotated-fixture-key")
    expected = "rotated-fixture-key" if rotated else "first-fixture-key"
    original_key = active.api_key
    calls: list[str] = []

    def rediscover(*args: object, **kwargs: object) -> Provider:
        raise AssertionError("Title generation must not rediscover auto authentication")

    async def catalog(provider: Provider) -> list[str]:
        assert provider is not active
        assert provider.api_key == expected
        calls.append("catalog")
        return ["gpt-4o-mini"]

    async def generate(
        provider: OpenAIProvider,
        messages: list[Message],
        tools: list[ToolDefinition] | None = None,
        config: GenerationConfig | None = None,
        stream: bool = True,
        verify_model: bool = False,
    ) -> AsyncGenerator[Event, None]:
        assert provider.api_key == expected
        assert provider.model == "gpt-4o-mini"
        calls.append("generate")
        yield TextDoneEvent(text="Named source title")

    monkeypatch.setattr("nagents.harness.runtime.session_titles.build_provider", rediscover)
    monkeypatch.setattr(Provider, "get_model_list", catalog)
    monkeypatch.setattr(OpenAIProvider, "generate", generate)
    try:
        await harness.agent.session.add_message(harness.session_id, Message(role="user", content="Name this chat"))
        assert await harness.rename_session() == "Named source title"
        assert calls == ["catalog", "generate"]
        assert active.api_key == original_key
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_auto_environment_fallback_readiness_checks_named_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = ProviderProfile(kind="openai", auth="auto", api_key_env="READINESS_NAMED_KEY")
    harness = Harness(
        HarnessConfig(
            workspace=tmp_path,
            data_dir=tmp_path / "state",
            provider="named",
            providers={"named": profile},
            demo=True,
        )
    )
    await harness.initialize()
    await harness.agent.provider.close()
    harness.agent.provider = _EnvOpenAIProvider(profile, "gpt-4o")
    harness.config.provider = "named"
    harness.config.providers = {"named": profile}
    harness.config.demo = False
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-fixture-key")
    monkeypatch.delenv("READINESS_NAMED_KEY", raising=False)
    try:
        missing = provider_setup(harness)
        assert not missing["configured"]
        assert "$READINESS_NAMED_KEY" in missing["message"]
        monkeypatch.setenv("READINESS_NAMED_KEY", "selected-fixture-key")
        assert provider_setup(harness) == {"configured": True, "message": ""}
        monkeypatch.setenv("READINESS_NAMED_KEY", "   ")
        assert not provider_setup(harness)["configured"]
    finally:
        await harness.close()
