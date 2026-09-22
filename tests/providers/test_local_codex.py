"""Offline tests for CodexProvider local discovery and voice auth selection."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from nagents import LiveConfig
from nagents import Message
from nagents.events import TextDoneEvent
from nagents.provider import Provider
from nagents.provider.codex import CODEX_ENDPOINT
from nagents.provider.codex import DEFAULT_CODEX_MODEL
from nagents.provider.codex import CodexConfigError
from nagents.provider.codex import CodexCredentials
from nagents.provider.codex import CodexProvider

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from nagents.events import Event
    from nagents.types import GenerationConfig
    from nagents.types import ToolDefinition


@pytest.fixture(autouse=True)
def isolate_codex_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


def segment(value: dict[str, object]) -> str:
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")


def signed_token(claims: dict[str, object]) -> str:
    return f"{segment({'alg': 'none', 'typ': 'JWT'})}.{segment(claims)}.signature"


def write_config(home: Path, body: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(body, encoding="utf-8")


def write_api_key_auth(home: Path, key: str = "sk-cached-secret") -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(json.dumps({"auth_mode": "apikey", "OPENAI_API_KEY": key}), encoding="utf-8")


def write_oauth_auth(
    home: Path,
    *,
    account_id: str = "acct-1",
    residency: str = "",
    expires: float | None = None,
    api_key: str | None = None,
    id_token_claims: dict[str, object] | None = None,
) -> str:
    claims: dict[str, object] = {
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account_id,
            "chatgpt_compute_residency": residency,
        }
    }
    if expires is not None:
        claims["exp"] = expires
    access = signed_token(claims)
    tokens: dict[str, object] = {"access_token": access, "account_id": account_id}
    if id_token_claims is not None:
        tokens["id_token"] = signed_token(id_token_claims)
    auth: dict[str, object] = {"auth_mode": "chatgpt", "tokens": tokens}
    if api_key is not None:
        auth["OPENAI_API_KEY"] = api_key
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(json.dumps(auth), encoding="utf-8")
    return access


def snapshot(directory: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(directory)): path.read_bytes() for path in sorted(directory.rglob("*")) if path.is_file()
    }


def test_missing_credentials_raise(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(CodexConfigError, match="API credential is missing"):
        CodexProvider(home=empty)


def test_explicit_home_overrides_codex_home_and_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    explicit = tmp_path / "explicit"
    write_config(explicit, 'model = "explicit-model"\n')
    write_api_key_auth(explicit, "sk-explicit")

    environment = tmp_path / "environment"
    write_config(environment, 'model = "env-model"\n')
    write_api_key_auth(environment, "sk-env")

    user = tmp_path / ".codex"
    write_config(user, 'model = "user-model"\n')
    write_api_key_auth(user, "sk-user")

    monkeypatch.setenv("CODEX_HOME", str(environment))
    assert CodexProvider(home=explicit).model == "explicit-model"
    assert CodexProvider().model == "env-model"

    monkeypatch.delenv("CODEX_HOME")
    assert CodexProvider().model == "user-model"

    assert CodexProvider(home=explicit, model="override-model").model == "override-model"


def test_profiles_new_file_legacy_table_and_paths(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    write_config(
        home,
        'model = "base-model"\nprofile = "work"\n\n[profiles.work]\nmodel = "legacy-model"\n',
    )
    write_api_key_auth(home)
    assert CodexProvider(home=home).model == "legacy-model"

    (home / "work.config.toml").write_text('model = "file-model"\n', encoding="utf-8")
    assert CodexProvider(home=home).model == "file-model"

    with pytest.raises(CodexConfigError, match="Invalid Codex profile name"):
        CodexProvider(home=home, profile="bad name")
    with pytest.raises(CodexConfigError, match="profile was not found"):
        CodexProvider(home=home, profile="ghost")


def test_custom_provider_chat_wire_and_env_key_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "codex"
    write_config(
        home,
        'model = "custom-model"\nmodel_provider = "myprov"\n\n'
        '[model_providers.myprov]\nname = "Mine"\nbase_url = "https://example.test/v1"\n'
        'wire_api = "chat"\nenv_key = "CUSTOM_KEY"\n',
    )
    monkeypatch.setenv("CUSTOM_KEY", "custom-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-openai")

    provider = CodexProvider(home=home)
    assert provider._local_api is True
    assert provider.model == "custom-model"
    assert provider.api == "chat_completions"
    assert provider.base_url == "https://example.test/v1"
    assert provider.api_key == "custom-secret"

    write_config(
        home,
        'model_provider = "myprov"\n\n[model_providers.myprov]\n'
        'wire_api = "responses"\nexperimental_bearer_token = "bearer-secret"\n',
    )
    bearer = CodexProvider(home=home)
    assert bearer.api_key == "bearer-secret" and bearer.api == "responses"


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ('model_provider = "nope"\n', "provider is not configured"),
        (
            'model_provider = "p"\n\n[model_providers.p]\nwire_api = "grpc"\n',
            "Unsupported Codex wire_api",
        ),
        (
            'model_provider = "p"\n\n[model_providers.p]\nbase_url = "https://x.test"\nauth = "command"\n',
            "Command auth and custom headers",
        ),
        (
            'model_provider = "p"\n\n[model_providers.p]\nbase_url = "https://x.test"\nhttp_headers = { X = "1" }\n',
            "Command auth and custom headers",
        ),
        (
            'model_provider = "p"\n\n[model_providers.p]\nbase_url = "https://x.test"\nquery_params = { q = "1" }\n',
            "Command auth and custom headers",
        ),
        (
            'model_provider = "p"\n\n[model_providers.p]\nbase_url = "https://x.test"\nrequires_openai_auth = "yes"\n',
            "requires_openai_auth must be boolean",
        ),
        (
            'model_provider = "p"\n\n[model_providers.p]\nbase_url = "https://x.test"\nrequires_openai_auth = false\n',
            "has no key",
        ),
    ],
)
def test_unsupported_local_configuration(tmp_path: Path, body: str, message: str) -> None:
    home = tmp_path / "codex"
    write_config(home, body)
    with pytest.raises(CodexConfigError, match=message):
        CodexProvider(home=home)


def test_keyring_and_forced_login_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "codex"
    write_config(home, 'cli_auth_credentials_store = "keyring"\n')
    write_api_key_auth(home)
    with pytest.raises(CodexConfigError, match="file credentials"):
        CodexProvider(home=home)

    write_config(home, 'forced_login_method = "api"\n')
    write_oauth_auth(home)
    with pytest.raises(CodexConfigError, match="requires API-key login"):
        CodexProvider(home=home)

    write_config(home, 'forced_login_method = "chatgpt"\n')
    write_api_key_auth(home)
    with pytest.raises(CodexConfigError, match="requires ChatGPT login"):
        CodexProvider(home=home)


def test_chatgpt_login_rejects_custom_base_url(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    write_config(home, 'openai_base_url = "https://custom.test/v1"\n')
    write_oauth_auth(home)
    with pytest.raises(CodexConfigError, match="standard Codex endpoint"):
        CodexProvider(home=home)


def test_oauth_credentials_and_redaction(tmp_path: Path) -> None:
    async def scenario() -> None:
        home = tmp_path / "codex"
        secret = write_oauth_auth(home, account_id="acct-1", residency="eu")
        provider = CodexProvider(home=home)
        assert provider._local_api is False
        assert provider.base_url == CODEX_ENDPOINT
        assert provider.api == "responses"
        assert provider.api_key == "oauth-not-an-api-key"

        credentials = await provider._credentials()
        assert credentials.access_token == secret
        assert credentials.account_id == "acct-1"
        assert credentials.residency == "eu"
        assert secret not in repr(credentials)
        assert "acct-1" not in repr(credentials)

        # Credentials are reread for each request, so a Codex refresh is picked up.
        rotated = write_oauth_auth(home, account_id="acct-2")
        refreshed = await provider._credentials()
        assert refreshed.access_token == rotated and refreshed.account_id == "acct-2"

        write_oauth_auth(home, account_id="acct-1", residency="no_constraint")
        assert (await provider._credentials()).residency == ""

        write_oauth_auth(
            home,
            account_id="ignored",
            id_token_claims={"https://api.openai.com/auth": {"chatgpt_account_id": "from-id-token"}},
        )
        assert (await provider._credentials()).account_id == "ignored"

    asyncio.run(scenario())


def test_expired_token_and_workspace_mismatch(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    write_oauth_auth(home, expires=time.time() - 60)
    with pytest.raises(CodexConfigError, match="access token expired"):
        CodexProvider(home=home)

    write_config(home, 'forced_chatgpt_workspace_id = "other-workspace"\n')
    write_oauth_auth(home, account_id="acct-1")
    with pytest.raises(CodexConfigError, match="does not match the configured workspace"):
        CodexProvider(home=home)


def test_invalid_credential_files_are_redacted(tmp_path: Path) -> None:
    home = tmp_path / "codex"
    home.mkdir()
    (home / "auth.json").write_text('{"tokens": "super-secret-token"', encoding="utf-8")
    with pytest.raises(CodexConfigError) as caught:
        CodexProvider(home=home)
    assert "super-secret-token" not in str(caught.value)
    assert str(caught.value) == "Cannot read Codex configuration or credentials"

    (home / "auth.json").write_text("x" * (1024 * 1024 + 1), encoding="utf-8")
    with pytest.raises(CodexConfigError, match="Cannot read"):
        CodexProvider(home=home)


def test_callback_construction_remains_oauth(tmp_path: Path) -> None:
    async def scenario() -> None:
        callback = AsyncMock(return_value=CodexCredentials("callback-token"))
        provider = CodexProvider(callback, model="callback-model")
        assert provider._local_api is False
        assert provider.base_url == CODEX_ENDPOINT
        assert provider.model == "callback-model"
        assert provider.api_key == "oauth-not-an-api-key"
        assert provider._credentials is callback
        assert await provider.verify_model() is True
        callback.assert_not_awaited()  # verify_model stays local on the OAuth route.

        with pytest.raises(ValueError, match="GPT-Live's public API requires"):
            CodexProvider(callback, live_config=LiveConfig())
        assert not hasattr(CodexProvider, "from_local")

    asyncio.run(scenario())


def test_voice_live_config_uses_api_key_never_oauth_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cached = tmp_path / "cached"
    write_oauth_auth(cached, api_key="sk-cached-voice")
    provider = CodexProvider(home=cached, live_config=LiveConfig(delegation="responses"))
    assert provider._local_api is True
    assert provider.api_key == "sk-cached-voice"
    assert provider.live_config is not None and provider.live_config.delegation == "responses"
    assert provider.base_url == "https://api.openai.com/v1"  # Standard OpenAI voice endpoint, not Codex.

    from_env = tmp_path / "from-env"
    write_oauth_auth(from_env)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-voice")
    assert CodexProvider(home=from_env, live_config=LiveConfig()).api_key == "sk-env-voice"

    missing = tmp_path / "missing-voice-key"
    write_oauth_auth(missing)
    monkeypatch.delenv("OPENAI_API_KEY")
    with pytest.raises(CodexConfigError, match="GPT-Live voice requires an OpenAI API key"):
        CodexProvider(home=missing, live_config=LiveConfig())

    # An expired ChatGPT token is irrelevant to voice when a cached API key exists.
    expired = tmp_path / "expired"
    write_oauth_auth(expired, expires=time.time() - 60, api_key="sk-still-valid")
    assert CodexProvider(home=expired, live_config=LiveConfig()).api_key == "sk-still-valid"


def test_voice_live_config_rejects_custom_base_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "codex"
    write_config(
        home,
        'model_provider = "p"\n\n[model_providers.p]\nbase_url = "https://example.test/v1"\n'
        'wire_api = "responses"\nenv_key = "CUSTOM_KEY"\n',
    )
    monkeypatch.setenv("CUSTOM_KEY", "sk-custom")
    with pytest.raises(ValueError, match="GPT-Live uses the OpenAI voice endpoint"):
        CodexProvider(home=home, live_config=LiveConfig())


def test_no_file_writes_and_project_config_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        home = tmp_path / "codex"
        write_config(home, 'model = "home-model"\n')
        token = write_oauth_auth(home)

        project = tmp_path / "project"
        write_config(project, 'model = "project-model"\n')
        (project / ".codex").mkdir(parents=True)
        (project / ".codex" / "config.toml").write_text('model = "project-codex-model"\n', encoding="utf-8")
        monkeypatch.chdir(project)

        before = snapshot(home)
        provider = CodexProvider(home=home)
        assert provider.model == "home-model"
        assert (await provider._credentials()).access_token == token
        assert snapshot(home) == before
        assert not (project / "auth.json").exists()

    asyncio.run(scenario())


def test_local_api_dispatches_to_base_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        home = tmp_path / "codex"
        write_api_key_auth(home, "sk-dispatch")
        provider = CodexProvider(home=home, model="local-model")
        assert provider._local_api is True

        seen: list[list[Message]] = []

        async def fake_generate(
            self: Provider,
            messages: list[Message],
            tools: list[ToolDefinition] | None = None,
            config: GenerationConfig | None = None,
            stream: bool = True,
            verify_model: bool = False,
        ) -> AsyncGenerator[Event, None]:
            seen.append(messages)
            yield TextDoneEvent(text="local")

        async def fake_verify(self: Provider, force: bool = False) -> bool:
            return True

        async def fake_models(self: Provider) -> list[str]:
            return ["local-model"]

        monkeypatch.setattr(Provider, "generate", fake_generate)
        monkeypatch.setattr(Provider, "verify_model", fake_verify)
        monkeypatch.setattr(Provider, "get_model_list", fake_models)

        events = [event async for event in provider.generate([Message(role="user", content="hi")])]
        assert isinstance(events[0], TextDoneEvent) and events[0].text == "local"
        assert seen and seen[0][0].content == "hi"
        assert await provider.verify_model() is True
        assert await provider.get_model_list() == ["local-model"]

    asyncio.run(scenario())


def test_oauth_provider_model_list_uses_fixed_endpoint(tmp_path: Path) -> None:
    async def scenario() -> None:
        home = tmp_path / "codex"
        write_oauth_auth(home)
        provider = CodexProvider(home=home)
        assert provider._local_api is False
        assert provider.base_url == CODEX_ENDPOINT
        assert provider.model == DEFAULT_CODEX_MODEL

    asyncio.run(scenario())
