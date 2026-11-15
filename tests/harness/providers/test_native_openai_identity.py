"""Native named connections retain identity without changing auth boundaries."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from nagents.harness.config import HarnessConfig
from nagents.harness.providers.connection import build_live_provider
from nagents.harness.providers.connection import build_provider
from nagents.harness.providers.factory import HarnessProvider
from nagents.harness.providers.registry import PROVIDERS
from nagents.harness.providers.registry import ProviderProfile
from nagents.live import LiveConfig
from nagents.provider import OpenAIProvider
from nagents.provider import ProviderType
from nagents.provider.codex.openai import CodexConfigError

if TYPE_CHECKING:
    from pathlib import Path


def test_registry_distinguishes_native_and_compatible() -> None:
    assert PROVIDERS["openai"] is ProviderType.OPENAI
    assert PROVIDERS["openai_compatible"] is ProviderType.OPENAI_COMPATIBLE


@pytest.mark.asyncio
@pytest.mark.parametrize("auth_mode", ["api-key", "auto"])
@pytest.mark.parametrize("api", ["auto", "chat_completions", "responses"])
async def test_native_api_key_connection_retains_options_and_lazy_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, auth_mode: str, api: str
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing"))
    monkeypatch.delenv("NAMED_OPENAI_KEY", raising=False)
    profile = ProviderProfile(
        kind="openai",
        auth=auth_mode,
        api=api,
        api_key_env="NAMED_OPENAI_KEY",
        request_timeout=37,
        session_id_header="X-Session-ID",
        context_window=12345,
        model_context_windows={"gpt-4o": 9876},
    )
    config = HarnessConfig(workspace=tmp_path, provider="native", providers={"native": profile}, model="gpt-4o")
    auth = Mock()
    auth.logged_in.return_value = False
    provider = build_provider(profile, config, auth)
    try:
        assert isinstance(provider, OpenAIProvider)
        assert provider.provider_type is ProviderType.OPENAI
        assert not provider.uses_chatgpt_auth
        assert provider.api == ("chat_completions" if api == "auto" else api)
        assert provider.session_id_header == "X-Session-ID"
        assert provider.context_window == 12345
        assert provider.model_context_windows == {"gpt-4o": 9876}
        assert provider._http._timeout.total == 37
        with pytest.raises(ValueError, match="NAMED_OPENAI_KEY"):
            await provider.verify_model()
        monkeypatch.setenv("NAMED_OPENAI_KEY", "first-key")
        assert await provider.verify_model()
        assert provider.api_key == "first-key"
        monkeypatch.setenv("NAMED_OPENAI_KEY", "rotated-key")
        assert await provider.verify_model()
        assert provider.api_key == "rotated-key"
        provider.model = "gpt-6-astra"
        assert provider.api == ("responses" if api == "auto" else api)
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_compatible_connection_is_not_native(tmp_path: Path) -> None:
    profile = ProviderProfile(kind="openai_compatible", base_url="https://third-party.example/v1")
    config = HarnessConfig(workspace=tmp_path, provider="other", providers={"other": profile}, model="model")
    provider = build_provider(profile, config, Mock())
    try:
        assert isinstance(provider, HarnessProvider)
        assert not isinstance(provider, OpenAIProvider)
        assert provider.provider_type is ProviderType.OPENAI_COMPATIBLE
        assert provider.base_url == profile.base_url
    finally:
        await provider.close()


def test_broken_codex_does_not_fall_back_to_api_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-api-key")
    (tmp_path / "auth.json").write_text("invalid json")
    profile = ProviderProfile(kind="openai", auth="auto")
    config = HarnessConfig(workspace=tmp_path, provider="native", providers={"native": profile}, model="gpt-4o")
    auth = Mock()
    auth.logged_in.return_value = False
    with pytest.raises(CodexConfigError):
        build_provider(profile, config, auth)


@pytest.mark.parametrize("auth_mode", ["api-key", "auto", "chatgpt", "codex"])
def test_native_connection_rejects_custom_endpoint(auth_mode: str) -> None:
    with pytest.raises(ValueError, match="fixed API host"):
        ProviderProfile(kind="openai", auth=auth_mode, base_url="https://third-party.example/v1").validate()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["openai", "openai_compatible"])
async def test_live_connection_identity(kind: str) -> None:
    profile = ProviderProfile(
        kind=kind, base_url="https://third-party.example/v1" if kind == "openai_compatible" else ""
    )
    provider = build_live_provider(profile, LiveConfig(), "explicit-key", "gpt-live")
    try:
        assert isinstance(provider, OpenAIProvider) is (kind == "openai")
        assert provider.provider_type is PROVIDERS[kind]
        assert provider.api_key == "explicit-key"
    finally:
        await provider.close()
