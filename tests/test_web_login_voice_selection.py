"""Voice shares provider selection, defaults and authentication with the Harness."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi import HTTPException

from nagents.harness.auth import OpenAIAuth
from nagents.harness.providers import KINDS
from nagents.harness.providers import ProviderProfile
from nagents.harness.providers import ProviderRegistry
from nagents.harness.providers import ScopedProviderRegistryStore
from nagents.provider.openai import CodexConfigError
from nagents.provider.openai import CodexCredentials
from nagents.provider.openai import _CodexConfig
from nagents.web.live_auth import resolve_voice_auth
from nagents.web.live_settings import GlobalVoiceInput
from nagents.web.live_settings import LiveSettings
from nagents.web.voice_preferences import VoicePreferences

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Literal
    from typing import Never


@pytest.fixture
def login(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    available = [False]

    def logged_in(auth: OpenAIAuth) -> bool:
        return available[0]

    async def credentials(auth: OpenAIAuth) -> CodexCredentials:
        assert available[0]
        return CodexCredentials("synthetic-login-token", "synthetic-account")

    def absent_codex(*, model: str) -> Never:
        raise CodexConfigError("No saved fixture login")

    monkeypatch.setattr(OpenAIAuth, "logged_in", logged_in)
    monkeypatch.setattr(OpenAIAuth, "credentials", credentials)
    monkeypatch.setattr("nagents.web.live_auth._load_config", absent_codex)
    return available


@pytest.mark.parametrize("mode", ["chatgpt", "codex"])
def test_missing_selected_login_never_falls_back_to_environment_key(
    monkeypatch: pytest.MonkeyPatch, login: list[bool], mode: Literal["chatgpt", "codex"]
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-environment-key")
    profile = ProviderProfile(kind="openai", auth=mode)
    assert resolve_voice_auth(profile, OpenAIAuth(), "gpt-live-1-codex") == ("chatgpt", None)


def test_auto_auth_uses_normal_key_default_but_does_not_bypass_a_broken_selected_login(
    monkeypatch: pytest.MonkeyPatch, login: list[bool]
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-environment-key")
    profile = ProviderProfile(kind="openai", auth="auto")
    assert resolve_voice_auth(profile, OpenAIAuth(), "gpt-live-1") == ("api-key", None)
    monkeypatch.setattr("nagents.web.live_auth._codex_files_exist", lambda: True)
    assert resolve_voice_auth(profile, OpenAIAuth(), "gpt-live-1") == ("chatgpt", None)


@pytest.mark.asyncio
async def test_explicit_codex_login_keeps_its_own_server_credential_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, login: list[bool]
) -> None:
    class SavedCodex(_CodexConfig):
        async def credentials(self) -> CodexCredentials:
            return CodexCredentials("synthetic-codex-login", "codex-account")

    def selected(*, model: str) -> _CodexConfig:
        return SavedCodex(model=model, api="responses", home=tmp_path, oauth=True)

    monkeypatch.setattr("nagents.web.live_auth._load_config", selected)
    monkeypatch.setattr("nagents.web.live_auth._codex_files_exist", lambda: True)
    login[0] = True
    auth, credentials = resolve_voice_auth(ProviderProfile(kind="openai", auth="codex"), OpenAIAuth(), "voice")
    assert auth == "chatgpt" and credentials is not None
    assert (await credentials()).access_token == "synthetic-codex-login"
    auth, credentials = resolve_voice_auth(ProviderProfile(kind="openai", auth="auto"), OpenAIAuth(), "voice")
    assert auth == "chatgpt" and credentials is not None
    assert (await credentials()).access_token == "synthetic-login-token"


@pytest.mark.asyncio
async def test_voice_choices_share_capabilities_and_support_default_or_explicit_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, login: list[bool]
) -> None:
    monkeypatch.setenv("VOICE_FIXTURE_KEY", "synthetic-private-api-key")
    profiles = {
        "api": ProviderProfile(kind="openai", auth="api-key", api_key_env="VOICE_FIXTURE_KEY"),
        "automatic": ProviderProfile(kind="openai", auth="auto"),
        "chatgpt": ProviderProfile(kind="openai", auth="chatgpt"),
        "codex": ProviderProfile(kind="openai", auth="codex"),
        "compatible": ProviderProfile(kind="openai_compatible", base_url="https://voice.invalid/v1"),
        "foundry": ProviderProfile(
            kind="foundry", auth="entra", api="responses", base_url="https://foundry.invalid/v1"
        ),
        "anthropic": ProviderProfile(kind="anthropic"),
    }
    registry = ScopedProviderRegistryStore(tmp_path)
    registry.save_scope(ProviderRegistry(active="api", providers=profiles), expected="0" * 64, scope="global")
    settings = LiveSettings(tmp_path / "sessions.db", demo=False, active=lambda: "", providers=registry)
    await settings.load()
    try:
        current = await settings.scoped("global")
        assert {name for name, _, _ in current.connections} == {
            name for name, profile in profiles.items() if KINDS[profile.kind].live
        }
        assert current.values.connection_id == "" and current.profile_name == "api"
        assert current.snapshot()["live_supported"] is True
        assert current.voice_auth == "api-key" and current.key_configured
        assert "synthetic-private-api-key" not in repr(current.snapshot())
        with pytest.raises(HTTPException) as error:
            await settings.change(
                GlobalVoiceInput(
                    scope="global",
                    revision=current.revision,
                    preferences=VoicePreferences(connection_id="anthropic"),
                )
            )
        assert error.value.status_code == 422
        assert (await settings.scoped("global")).revision == current.revision
        await settings.change(
            GlobalVoiceInput(
                scope="global",
                revision=current.revision,
                preferences=VoicePreferences(connection_id="chatgpt", enabled=True),
            )
        )
        missing = await settings.scoped("global")
        assert missing.voice_auth == "chatgpt" and not missing.key_configured
        assert missing.profile_name == "chatgpt" and registry.load().active == "api"
        login[0] = True
        ready = await settings.scoped("global")
        assert ready.voice_auth == "chatgpt" and ready.key_configured
        assert ready.login_credentials is not None
        assert (await ready.login_credentials()).access_token == "synthetic-login-token"
        assert "synthetic-login-token" not in repr(ready.snapshot())
        await settings.change(
            GlobalVoiceInput(scope="global", revision=ready.revision, preferences=VoicePreferences(enabled=True))
        )
        default = await settings.scoped("global")
        assert default.profile_name == "api" and default.voice_auth == "api-key"
        before = registry.load_scope("global")
        registry.save_scope(
            ProviderRegistry(active="foundry", providers=profiles), expected=before.revision, scope="global"
        )
        changed = await settings.scoped("global")
        assert changed.profile_name == "foundry" and changed.voice_auth == "entra"
        assert changed.revision != default.revision
    finally:
        await settings.shutdown()
