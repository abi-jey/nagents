"""Provider login storage, precedence, and CLI flows; every value is synthetic."""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import stat
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from aiohttp import web

from nagents import cli
from nagents.cli import main
from nagents.harness import provider_login
from nagents.harness.config import HarnessConfig
from nagents.harness.config import load_config
from nagents.harness.credentials import ProviderLogin
from nagents.harness.credentials import ProviderLoginError
from nagents.harness.credentials import ProviderLoginStore
from nagents.harness.provider import HarnessProvider
from nagents.harness.runtime import Harness

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from collections.abc import Awaitable
    from collections.abc import Callable


SECRET = "sk-synthetic-provider-secret"
KEY_ENV = "NGN_TEST_PROVIDER_KEY"


async def verified(**kwargs: object) -> str:
    return "Verified: synthetic catalog."


def store() -> ProviderLoginStore:
    return ProviderLoginStore()


def login(**changes: object) -> ProviderLogin:
    values: dict[str, object] = {
        "provider": "openrouter",
        "model": "openrouter/auto",
        "api_key_env": "OPENROUTER_API_KEY",
        "api_key": SECRET,
    }
    values.update(changes)
    return ProviderLogin(**values)  # type: ignore[arg-type]


@pytest.mark.requires_posix
def test_store_round_trip_keeps_keys_out_of_selections(tmp_path: Path) -> None:
    provider_store = store()
    provider_store.save(login())
    assert stat.S_IMODE(provider_store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(provider_store.path.parent.stat().st_mode) == 0o700
    assert SECRET not in repr(login())
    selection = provider_store.selection()
    assert selection is not None
    assert (selection.provider, selection.model, selection.api_key) == ("openrouter", "openrouter/auto", "")
    assert SECRET not in repr(selection)
    assert provider_store.key_for("openrouter") == SECRET
    assert provider_store.key_for("openai") == ""
    assert provider_store.has_secret()
    assert "openrouter" in provider_store.status() and SECRET not in provider_store.status()
    provider_store.remove()
    assert provider_store.selection() is None
    assert provider_store.key_for("openrouter") == ""
    assert provider_store.status() == "Not signed in"
    provider_store.remove()


@pytest.mark.requires_posix
def test_store_treats_unsafe_or_corrupt_files_as_unusable() -> None:
    provider_store = store()
    provider_store.save(login())
    provider_store.path.write_text("{not json")
    assert provider_store.selection() is None
    assert provider_store.key_for("openrouter") == ""
    assert "unusable" in provider_store.status()
    provider_store.save(login())  # Overwriting a corrupt store recovers it.
    assert provider_store.key_for("openrouter") == SECRET
    provider_store.path.chmod(0o644)
    assert provider_store.selection() is None
    with pytest.raises(ProviderLoginError):
        provider_store.save(login())
    with pytest.raises(ProviderLoginError):
        provider_store.remove()


@pytest.mark.requires_posix
def test_store_rejects_invalid_fields_before_writing() -> None:
    provider_store = store()
    with pytest.raises(ProviderLoginError):
        provider_store.save(login(provider="../escape"))
    with pytest.raises(ProviderLoginError):
        provider_store.save(login(api_key_env="not a name"))
    with pytest.raises(ProviderLoginError):
        provider_store.save(login(api="responses; rm -rf"))
    with pytest.raises(ProviderLoginError):
        provider_store.save(login(api_key="line\nbreak"))
    assert provider_store.selection() is None


@pytest.mark.requires_posix
def test_saved_login_supplies_defaults_below_env_and_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store().save(login())
    config = load_config(tmp_path)
    assert (config.provider, config.model, config.auth, config.api_key_env) == (
        "openrouter",
        "openrouter/auto",
        "api-key",
        "OPENROUTER_API_KEY",
    )
    assert any("Applied saved provider login" in note for note in config.diagnostics)

    monkeypatch.setenv("NGN_PROVIDER", "anthropic")
    assert load_config(tmp_path).provider == "anthropic"
    monkeypatch.delenv("NGN_PROVIDER")

    user_config = Path(os.environ["XDG_CONFIG_HOME"]) / "ngn/config.toml"
    user_config.parent.mkdir(parents=True, exist_ok=True)
    user_config.write_text('model = "toml-model"\n')
    overridden = load_config(tmp_path)
    assert overridden.provider == "openrouter" and overridden.model == "toml-model"


@pytest.mark.requires_posix
def test_invalid_saved_login_is_ignored_with_a_diagnostic(tmp_path: Path) -> None:
    provider_store = store()
    provider_store.save(login())
    provider_store.path.write_text(
        json.dumps(
            {
                "version": 1,
                "provider": "not-a-provider",
                "model": "",
                "base_url": "",
                "api": "auto",
                "auth": "api-key",
                "api_key_env": "",
                "api_key": "",
            }
        )
    )
    config = load_config(tmp_path)
    assert config.provider == "openai" and config.model == "gpt-4.1"
    assert any("Ignored" in note for note in config.diagnostics)


@pytest.mark.requires_posix
def test_harness_provider_prefers_environment_key_over_stored_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider_store = store()
    provider_store.save(login())

    class Config:
        demo = False
        provider = "openrouter"
        model = "openrouter/auto"
        api_key_env = "OPENROUTER_API_KEY"
        base_url = ""
        api = "auto"
        api_version = ""

    provider = HarnessProvider(Config(), provider_store)  # type: ignore[arg-type]
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    provider.credentials()
    assert provider.api_key == SECRET
    monkeypatch.setenv("OPENROUTER_API_KEY", "environment-key")
    provider.credentials()
    assert provider.api_key == "environment-key"

    class Other:
        demo = False
        provider = "anthropic"
        model = "claude"
        api_key_env = "ANTHROPIC_API_KEY"
        base_url = ""
        api = "auto"
        api_version = ""

    mismatched = HarnessProvider(Other(), provider_store)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="sign in with ngn login"):
        mismatched.credentials()


@pytest.mark.requires_posix
def test_harness_login_api_persists_and_logout_removes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        harness = Harness(HarnessConfig(workspace=tmp_path))
        try:
            await harness.initialize()
            await harness.login_api(login())
            assert harness.config.provider == "openrouter" and harness.config.model == "openrouter/auto"
            assert isinstance(harness.agent.provider, HarnessProvider)
            harness.agent.provider.credentials()
            assert harness.agent.provider.api_key == SECRET
            assert "private credential store" in harness.auth_status()
            assert SECRET not in harness.describe() and SECRET not in harness.login_status()
            saved = harness.login_store.selection()
            assert saved is not None and saved.auth == "api-key"
            assert harness.login_store.key_for("openrouter") == SECRET
            await harness.logout()
            assert harness.config.auth == "api-key"
            assert harness.login_store.selection() is None
            assert harness.login_store.key_for("openrouter") == ""
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.requires_posix
def test_harness_login_api_requires_credentials_and_rejects_demo(tmp_path: Path) -> None:
    async def scenario() -> None:
        harness = Harness(HarnessConfig(workspace=tmp_path))
        try:
            await harness.initialize()
            with pytest.raises(ValueError, match="No API key or key environment variable"):
                await harness.login_api(login(api_key="", api_key_env=""))
            assert harness.config.provider == "openai"
            assert harness.login_store.selection() is None
        finally:
            await harness.close()
        demo = Harness(HarnessConfig(workspace=tmp_path, demo=True))
        try:
            with pytest.raises(ValueError, match="disabled in offline demo"):
                await demo.login_api(login())
        finally:
            await demo.close()

    asyncio.run(scenario())


def test_pkce_matches_rfc7636_and_builds_headless_url() -> None:
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert provider_login.code_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    url = provider_login.authorization_url(verifier)
    assert url.startswith("https://openrouter.ai/auth?")
    assert "code_challenge=" in url and "code_challenge_method=S256" in url and "key_label=ngn" in url
    assert len(provider_login.code_verifier()) >= 43
    assert not provider_login.valid_code("short")
    assert not provider_login.valid_code("has space in it")
    assert provider_login.valid_code("synthetic-code-1234")


@asynccontextmanager
async def openrouter_issuer(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
) -> AsyncIterator[str]:
    app = web.Application()
    app.router.add_post("/api/v1/auth/keys", handler)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    monkeypatch.setattr(
        provider_login, "OPENROUTER_KEYS_URL", f"http://127.0.0.1:{runner.addresses[0][1]}/api/v1/auth/keys"
    )
    try:
        yield "http://127.0.0.1"
    finally:
        await runner.cleanup()


def test_exchange_code_posts_pkce_and_returns_key(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async def handler(request: web.Request) -> web.Response:
            assert request.headers["User-Agent"].startswith("ngn/")
            assert await request.json() == {
                "code": "synthetic-code-1234",
                "code_verifier": "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk",
                "code_challenge_method": "S256",
            }
            return web.json_response({"key": SECRET})

        async with openrouter_issuer(monkeypatch, handler):
            assert (
                await provider_login.exchange_code("synthetic-code-1234", "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")
                == SECRET
            )

    asyncio.run(scenario())


def test_exchange_code_failures_are_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async def denied(request: web.Request) -> web.Response:
            return web.json_response({"error": SECRET}, status=403)

        async with openrouter_issuer(monkeypatch, denied):
            with pytest.raises(ProviderLoginError) as caught:
                await provider_login.exchange_code("synthetic-code-1234", "v" * 64)
            assert SECRET not in str(caught.value)
        with pytest.raises(ProviderLoginError):
            await provider_login.exchange_code("bad code", "v" * 64)
        with pytest.raises(ProviderLoginError):
            await provider_login.exchange_code("synthetic-code-1234", "short")

    asyncio.run(scenario())


def test_verify_credentials_reports_without_echoing_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async def catalog(self: object) -> list[str]:
            return ["openrouter/auto", "other/model"]

        async def close(self: object) -> None:
            return None

        monkeypatch.setattr("nagents.provider.base.Provider.get_model_list", catalog)
        monkeypatch.setattr("nagents.provider.base.Provider.close", close)
        note = await provider_login.verify_credentials(
            provider="openrouter", model="openrouter/auto", base_url="", api="auto", api_key=SECRET
        )
        assert "2 models" in note and SECRET not in note
        missing = await provider_login.verify_credentials(
            provider="openrouter", model="missing/model", base_url="", api="auto", api_key=SECRET
        )
        assert "not found" in missing and SECRET not in missing

    asyncio.run(scenario())


def _drive_login(args: list[str], monkeypatch: pytest.MonkeyPatch, *, key_input: str = "") -> None:
    if key_input:
        monkeypatch.setattr(sys, "stdin", io.StringIO(key_input))
    monkeypatch.setattr(provider_login, "verify_credentials", verified)


@pytest.mark.requires_posix
def test_cli_login_stores_key_from_stdin_without_rendering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _drive_login([], monkeypatch, key_input=f"{SECRET}\n")
    assert main(["login", "openai", "--workspace", str(tmp_path), "--key-stdin"]) == 0
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "Signed in to openai" in output and SECRET not in output
    assert store().key_for("openai") == SECRET
    config = load_config(tmp_path)
    assert (config.provider, config.model) == ("openai", "gpt-4.1")


@pytest.mark.requires_posix
def test_cli_login_custom_uses_base_url_and_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _drive_login([], monkeypatch, key_input=f"{SECRET}\n")
    args = [
        "login",
        "custom",
        "--workspace",
        str(tmp_path),
        "--key-stdin",
        "--base-url",
        "https://gateway.example/v1",
        "--model",
        "local-model",
    ]
    assert main(args) == 0
    output = capsys.readouterr().out
    assert "openai_compatible" in output and SECRET not in output
    saved = store().selection()
    assert saved is not None
    assert (saved.provider, saved.model, saved.base_url) == (
        "openai_compatible",
        "local-model",
        "https://gateway.example/v1",
    )


@pytest.mark.requires_posix
def test_cli_login_openrouter_exchanges_pasted_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def exchange(code: str, verifier: str) -> str:
        assert code == "synthetic-code-1234"
        return SECRET

    _drive_login([], monkeypatch, key_input="synthetic-code-1234\n")
    monkeypatch.setattr(provider_login, "exchange_code", exchange)
    assert main(["login", "openrouter", "--workspace", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert "openrouter.ai/auth" in output and SECRET not in output and "synthetic-code-1234" not in output
    assert store().key_for("openrouter") == SECRET


@pytest.mark.requires_posix
def test_cli_login_can_reference_a_key_environment_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(KEY_ENV, raising=False)
    assert (
        main(
            [
                "login",
                "anthropic",
                "--workspace",
                str(tmp_path),
                "--model",
                "synthetic-model",
                "--api-key-env",
                KEY_ENV,
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert f"${KEY_ENV}" in output
    provider_store = store()
    assert provider_store.key_for("anthropic") == ""
    config = load_config(tmp_path)
    assert (config.provider, config.model, config.api_key_env) == ("anthropic", "synthetic-model", KEY_ENV)
    assert main(["login", "--status", "--workspace", str(tmp_path)]) == 0
    status = capsys.readouterr().out
    assert "anthropic" in status and KEY_ENV in status
    assert main(["logout", "--workspace", str(tmp_path)]) == 0
    assert "Removed ngn's saved logins" in capsys.readouterr().out
    assert provider_store.selection() is None


def test_cli_login_requires_key_stdin_when_not_a_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert main(["login", "openai", "--workspace", str(tmp_path)]) == 2
    assert "--key-stdin" in capsys.readouterr().err
    assert store().selection() is None


def test_cli_login_rejects_unknown_methods(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["login", "bogus", "--workspace", str(tmp_path)]) == 2
    assert "Unknown provider" in capsys.readouterr().err


def test_interactive_menu_accepts_numbers_tokens_and_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    answers = iter(["4", "openrouter", "", "nope", "2"])
    monkeypatch.setattr(cli, "_read_line", lambda prompt: next(answers))
    assert cli._interactive_login_choice().provider == "anthropic"
    assert cli._interactive_login_choice().provider == "openrouter"
    assert cli._interactive_login_choice().device
    assert cli._interactive_login_choice().provider == "openrouter"  # Recovers from invalid input.
    assert "Enter a number" in capsys.readouterr().out


def test_non_interactive_login_defaults_to_chatgpt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    args = argparse.Namespace(method="", provider="", device_auth=False)
    assert cli._login_choice(args).device
    assert (
        cli._login_choice(argparse.Namespace(method="", provider="anthropic", device_auth=False)).provider
        == "anthropic"
    )
    assert cli._login_choice(argparse.Namespace(method="", provider="litellm", device_auth=False)).provider == "litellm"
    with pytest.raises(ValueError):
        cli._login_choice(argparse.Namespace(method="", provider="no-such-provider", device_auth=False))
