"""Voice factories honor the selected Codex connection without external requests."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import aiohttp
import httpx
import pytest
from fastapi import HTTPException

from nagents.harness.auth import OpenAIAuth
from nagents.harness.config import HarnessConfig
from nagents.harness.connection import build_live_provider
from nagents.harness.connection import build_provider
from nagents.harness.connection import live_auth_available
from nagents.harness.providers import ProviderProfile
from nagents.harness.providers import ProviderRegistry
from nagents.harness.providers import ScopedProviderRegistryStore
from nagents.live import LiveConfig
from nagents.provider.openai import CodexConfigError
from nagents.provider.openai import CodexCredentials
from nagents.web.live import create_agent
from nagents.web.live import create_login_config
from nagents.web.live import unavailable_reason
from nagents.web.live_settings import GlobalVoiceInput
from nagents.web.live_settings import LiveSettings
from nagents.web.voice_preferences import VoicePreferences
from tests.providers.test_openai_local import write_api_key_auth
from tests.providers.test_openai_local import write_config
from tests.providers.test_openai_local import write_oauth_auth

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.web.live_settings import LiveConnection

SELECTED_KEY = "synthetic-selected-codex-key"
AMBIENT_KEY = "synthetic-ambient-openai-key"
PROFILE_KEY = "synthetic-named-profile-key"
SELECTED_ENDPOINT = "https://selected.invalid/v1"


@pytest.fixture(autouse=True)
def isolated_auth(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("OPENAI_API_KEY", AMBIENT_KEY)
    monkeypatch.setenv("PROFILE_VOICE_KEY", PROFILE_KEY)
    monkeypatch.delenv("ABSENT_CODEX_KEY", raising=False)
    monkeypatch.setattr(OpenAIAuth, "logged_in", lambda self: False)
    monkeypatch.setattr(OpenAIAuth, "credentials", AsyncMock(side_effect=AssertionError("No real login read")))
    monkeypatch.setattr(aiohttp.ClientSession, "_request", AsyncMock(side_effect=AssertionError("No network")))
    monkeypatch.setattr(httpx.AsyncClient, "send", AsyncMock(side_effect=AssertionError("No network")))


def selected_codex(home: Path) -> None:
    write_config(home, f'openai_base_url = "{SELECTED_ENDPOINT}"\n')
    write_api_key_auth(home, SELECTED_KEY)


@asynccontextmanager
async def connection(
    workspace: Path, profile: ProviderProfile, *, explicit_selection: bool = True
) -> AsyncIterator[LiveConnection]:
    registry = ScopedProviderRegistryStore(workspace)
    registry.save_scope(
        ProviderRegistry(active="selected", providers={"selected": profile}), expected="0" * 64, scope="global"
    )
    settings = LiveSettings(workspace / "sessions.db", demo=False, active=lambda: "", providers=registry)
    await settings.load()
    try:
        current = await settings.scoped("global")
        await settings.change(
            GlobalVoiceInput(
                scope="global",
                revision=current.revision,
                preferences=VoicePreferences(enabled=True, connection_id="selected" if explicit_selection else ""),
            )
        )
        yield await settings.scoped("global")
    finally:
        await settings.shutdown()


async def backend(_transcript: str) -> str:
    raise AssertionError("No backend inference")


@pytest.mark.asyncio
@pytest.mark.parametrize("auth", ["codex", "auto"])
@pytest.mark.parametrize("key_env", ["", "PROFILE_VOICE_KEY"])
@pytest.mark.parametrize("explicit_selection", [False, True])
async def test_selected_codex_keeps_endpoint_and_key_in_text_and_voice(
    tmp_path: Path, auth: str, key_env: str, explicit_selection: bool
) -> None:
    selected_codex(tmp_path / "codex")
    profile = ProviderProfile(kind="openai", auth=auth, api_key_env=key_env, request_timeout=37.5)
    normal = build_provider(profile, HarnessConfig(workspace=tmp_path, auth=auth), OpenAIAuth())
    try:
        async with connection(tmp_path, profile, explicit_selection=explicit_selection) as selected:
            assert selected.profile_name == "selected" and selected.voice_auth == "api-key"
            assert selected.key_configured and live_auth_available(profile, "gpt-live-1")
            assert unavailable_reason(selected, demo=False) == ""
            voice = create_agent(selected, client_handler=backend)
            try:
                assert normal.base_url == voice.provider.base_url == SELECTED_ENDPOINT
                assert normal.api_key == voice.provider.api_key == SELECTED_KEY
                assert voice.provider._http._timeout.total == 37.5
                assert selected.api_key.get_secret_value() == "", "Do not capture an unrelated environment key"
                visible = repr(selected.snapshot())
                assert all(key not in visible for key in [SELECTED_KEY, AMBIENT_KEY, PROFILE_KEY])
            finally:
                await voice.close()
    finally:
        await normal.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("auth", ["codex", "auto"])
async def test_selected_codex_provider_env_reference_wins_over_profile_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, auth: str
) -> None:
    write_config(
        tmp_path / "codex",
        'model_provider = "selected"\n[model_providers.selected]\n'
        f'base_url = "{SELECTED_ENDPOINT}"\nwire_api = "responses"\nenv_key = "CODEX_PROVIDER_KEY"\n',
    )
    monkeypatch.setenv("CODEX_PROVIDER_KEY", SELECTED_KEY)
    profile = ProviderProfile(kind="openai", auth=auth, api_key_env="PROFILE_VOICE_KEY")
    async with connection(tmp_path, profile) as selected:
        assert selected.key_configured
        voice = create_agent(selected, client_handler=backend)
        try:
            assert voice.provider.base_url == SELECTED_ENDPOINT
            assert voice.provider.api_key == SELECTED_KEY
        finally:
            await voice.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("auth", "broken"),
    [
        ("codex", "missing"),
        ("codex", "invalid"),
        ("auto", "invalid"),
        ("codex", "missing-key"),
        ("auto", "missing-key"),
    ],
)
async def test_selected_missing_or_invalid_codex_never_uses_an_ambient_key(
    tmp_path: Path, auth: str, broken: str
) -> None:
    home = tmp_path / "codex"
    if broken == "invalid":
        write_config(home, "invalid = [")
    elif broken == "missing-key":
        write_config(home, 'model_provider = "custom"\n[model_providers.custom]\nenv_key = "ABSENT_CODEX_KEY"\n')
    profile = ProviderProfile(kind="openai", auth=auth, api_key_env="PROFILE_VOICE_KEY")
    assert not live_auth_available(profile, "gpt-live-1")
    with pytest.raises(CodexConfigError):
        build_live_provider(profile, LiveConfig(), PROFILE_KEY, "gpt-live-1")
    async with connection(tmp_path, profile) as selected:
        assert not selected.key_configured
        assert unavailable_reason(selected, demo=False)
        with pytest.raises(HTTPException) as error:
            create_agent(selected, client_handler=backend)
        assert error.value.status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [False, True])
async def test_auto_without_codex_uses_only_the_named_environment_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured: bool
) -> None:
    if not configured:
        monkeypatch.delenv("PROFILE_VOICE_KEY")
    profile = ProviderProfile(kind="openai", auth="auto", api_key_env="PROFILE_VOICE_KEY")
    assert live_auth_available(profile, "gpt-live-1") is configured
    async with connection(tmp_path, profile) as selected:
        assert selected.key_configured is configured
        if not configured:
            with pytest.raises(HTTPException):
                create_agent(selected, client_handler=backend)
            return
        voice = create_agent(selected, client_handler=backend)
        try:
            assert voice.provider.base_url == "https://api.openai.com/v1"
            assert voice.provider.api_key == PROFILE_KEY
        finally:
            await voice.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("auth", ["codex", "auto", "chatgpt"])
async def test_login_voice_retains_codex_or_ngn_oauth_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, auth: str
) -> None:
    codex_token = write_oauth_auth(tmp_path / "codex")
    monkeypatch.setattr(OpenAIAuth, "logged_in", lambda self: True)
    monkeypatch.setattr(OpenAIAuth, "credentials", AsyncMock(return_value=CodexCredentials("synthetic-ngn", "ngn")))
    profile = ProviderProfile(kind="openai", auth=auth, api_key_env="PROFILE_VOICE_KEY")
    async with connection(tmp_path, profile) as selected:
        assert selected.voice_auth == "chatgpt" and selected.key_configured
        assert selected.api_key.get_secret_value() == ""
        login = create_login_config(selected, "", backend)
        assert login is not None
        assert (await login.credentials()).access_token == (codex_token if auth == "codex" else "synthetic-ngn")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["openai", "openai_compatible", "foundry", "azure_openai_compatible_v1"])
async def test_explicit_api_profiles_remain_independent_of_codex(tmp_path: Path, kind: str) -> None:
    selected_codex(tmp_path / "codex")
    endpoint = "" if kind == "openai" else "https://explicit.invalid/v1"
    profile = ProviderProfile(
        kind=kind,
        auth="api-key",
        api="responses" if kind == "foundry" else "auto",
        api_key_env="PROFILE_VOICE_KEY",
        base_url=endpoint,
    )
    async with connection(tmp_path, profile) as selected:
        assert selected.voice_auth == "api-key" and selected.key_configured
        voice = create_agent(selected, client_handler=backend)
        try:
            assert voice.provider.base_url == (endpoint or "https://api.openai.com/v1")
            assert voice.provider.api_key == PROFILE_KEY
        finally:
            await voice.close()
