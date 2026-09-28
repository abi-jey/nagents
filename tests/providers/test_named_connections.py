"""Shared provider routing stays secret-free across CLI, Harness, and voice."""

from __future__ import annotations

import asyncio
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest
import yaml

from nagents.harness.auth import OpenAIAuth
from nagents.harness.config import AgentProfile
from nagents.harness.config import HarnessConfig
from nagents.harness.config import load_config
from nagents.harness.connection import build_provider
from nagents.harness.provider import HarnessProvider
from nagents.harness.providers import ProviderProfile
from nagents.harness.providers import ProviderRegistry
from nagents.harness.providers import ProviderRegistryStore
from nagents.harness.providers import ScopedProviderRegistryStore
from nagents.harness.runtime import Harness
from nagents.provider.openai import CODEX_ENDPOINT
from nagents.provider.openai import CodexConfigError
from nagents.web.live import create_agent
from nagents.web.live_settings import GlobalVoiceInput
from nagents.web.live_settings import LiveSettings
from nagents.web.live_settings import WorkspaceVoiceInput
from nagents.web.voice_preferences import VoiceOverrides
from nagents.web.voice_preferences import VoicePreferences


def sample(**changes: object) -> ProviderProfile:
    profile = ProviderProfile(kind="openai", auth="api-key", api_key_env="${TEST_NGN_KEY}")
    return replace(profile, **changes)  # type: ignore[arg-type]


def test_v2_rejects_a_chat_model_inside_provider_connections(tmp_path: Path) -> None:
    store = ProviderRegistryStore(tmp_path / "providers.yaml")
    store.path.write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "revision": "0" * 64,
                "active": "named",
                "providers": {"named": {"kind": "openai", "model": "should-not-be-here"}},
            }
        )
    )
    with pytest.raises(ValueError, match="Invalid provider entry"):
        store.load()
    store.path.write_text(yaml.safe_dump({"version": 1, "revision": "0" * 64, "active": "", "providers": {}}))
    with pytest.raises(ValueError, match="Invalid provider configuration format"):
        store.load()


@pytest.mark.parametrize(
    ("kind", "endpoint"),
    [
        ("openai", "https://api.openai.com/v1"),
        ("openrouter", "https://openrouter.ai/api/v1"),
        ("anthropic", "https://api.anthropic.com/v1"),
        ("gemini", "https://generativelanguage.googleapis.com/v1beta"),
    ],
)
def test_connection_snapshot_explains_fixed_endpoints_without_keys(kind: str, endpoint: str) -> None:
    profile = ProviderProfile(kind=kind)
    profile.validate()
    visible = ProviderRegistry(active="account", providers={"account": profile}).snapshot()["providers"]
    assert isinstance(visible, dict)
    account = visible["account"]
    assert isinstance(account, dict)
    assert account["effective_endpoint"] == endpoint
    assert account["credential_source"] == f"API key ${profile.key_env}"
    assert "model" not in account


def test_openai_connection_endpoint_reflects_authentication_route() -> None:
    chatgpt = sample(auth="chatgpt")
    assert chatgpt.effective_endpoint == CODEX_ENDPOINT
    codex = sample(auth="codex")
    assert "local Codex configuration" in codex.effective_endpoint
    assert codex.effective_endpoint != "https://api.openai.com/v1"
    auto = sample(auth="auto")
    assert CODEX_ENDPOINT in auto.effective_endpoint
    assert "https://api.openai.com/v1" in auto.effective_endpoint


def test_registry_is_shared_revisioned_and_contains_no_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_NGN_KEY", "synthetic-secret-never-on-disk")
    store = ProviderRegistryStore()
    selected = store.save(ProviderRegistry(active="work", providers={"work": sample()}), expected="0" * 64)
    assert store.path == Path(os.environ["XDG_CONFIG_HOME"]) / "ngn/providers.yaml"
    assert "synthetic-secret-never-on-disk" not in store.path.read_text()
    assert "TEST_NGN_KEY" in store.path.read_text()
    assert selected.snapshot()["providers"]["work"]["key_configured"] is True  # type: ignore[index]
    assert load_config(tmp_path).provider_id == "work"
    with pytest.raises(ValueError, match="changed"):
        store.save(ProviderRegistry(active="work", providers={"work": sample(auth="auto")}), expected="0" * 64)
    second = store.save(
        ProviderRegistry(active="work", providers={"work": sample(auth="auto")}),
        expected=selected.revision,
    )
    assert ProviderRegistryStore().load() == second


def test_concurrent_provider_saves_reject_stale_revision(tmp_path: Path) -> None:
    store = ProviderRegistryStore(tmp_path / "providers.yaml")
    barrier = Barrier(2)

    def save(auth: str) -> str:
        barrier.wait()
        try:
            result = store.save(
                ProviderRegistry(active="chosen", providers={"chosen": sample(auth=auth)}),
                expected="0" * 64,
            )
        except ValueError as error:
            return str(error)
        return result.revision

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(save, "api-key")
        second = pool.submit(save, "auto")
        outcomes = [first.result(), second.result()]
    assert sum(value == store.load().revision for value in outcomes) == 1
    assert sum("changed" in value for value in outcomes) == 1
    assert store.load().providers["chosen"].auth in {"api-key", "auto"}


def test_provider_id_override_preserves_workspace_model_unless_explicitly_overridden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ProviderRegistryStore()
    store.save(
        ProviderRegistry(
            active="first",
            providers={
                "first": sample(),
                "second": sample(kind="anthropic", auth="api-key", api_key_env="ANTHROPIC_API_KEY"),
            },
        ),
        expected="0" * 64,
    )
    ScopedProviderRegistryStore(tmp_path).model_store("workspace").save("workspace-model")
    monkeypatch.setenv("NGN_PROVIDER_ID", "second")
    selected = load_config(tmp_path)
    assert (selected.provider_id, selected.provider, selected.model) == ("second", "anthropic", "workspace-model")
    monkeypatch.setenv("NGN_MODEL", "explicit-model")
    assert load_config(tmp_path).model == "explicit-model"


@pytest.mark.parametrize(
    "fields",
    [
        {"api_key_env": "literal-key!"},
        {"auth": "chatgpt", "base_url": "https://example.com/v1"},
        {"kind": "foundry", "base_url": ""},
        {"kind": "foundry", "base_url": "http://example.com/openai/v1", "auth": "entra"},
        {"kind": "anthropic", "auth": "codex"},
    ],
)
def test_provider_shapes_refuse_invalid_or_secret_values(fields: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        sample(**fields).validate()


@pytest.mark.requires_posix
def test_agent_binding_uses_named_provider_and_environment_at_request_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ProviderRegistryStore()
    store.save(
        ProviderRegistry(
            active="first",
            providers={
                "first": sample(),
                "second": sample(kind="openrouter", api_key_env="ALT_NGN_KEY"),
            },
        ),
        expected="0" * 64,
    )
    ScopedProviderRegistryStore(tmp_path).model_store("workspace").save("workspace-choice")

    async def check() -> None:
        config = load_config(tmp_path)
        config.profiles["review"] = AgentProfile(mode="reviewer", provider="second")
        harness = Harness(config)
        try:
            await harness.initialize(create_session=False)
            assert harness.config.provider_id == "first"
            monkeypatch.setenv("TEST_NGN_KEY", "first-token")
            harness.agent.provider.credentials()  # type: ignore[attr-defined]
            assert harness.agent.provider.api_key == "first-token"
            await harness.set_agent("review")
            assert (harness.config.provider_id, harness.config.model) == ("second", "workspace-choice")
            monkeypatch.setenv("ALT_NGN_KEY", "second-token")
            harness.agent.provider.credentials()  # type: ignore[attr-defined]
            assert harness.agent.provider.api_key == "second-token"
            await harness.set_agent("assistant")
            assert harness.config.provider_id == "first"
            assert harness.agent.provider.model == "workspace-choice"
        finally:
            await harness.close()

    asyncio.run(check())


def test_explicit_agent_model_does_not_replace_shared_workspace_choice(tmp_path: Path) -> None:
    scoped = ScopedProviderRegistryStore(tmp_path)
    scoped.global_store.save(ProviderRegistry(active="named", providers={"named": sample()}), expected="0" * 64)
    scoped.model_store("workspace").save("shared-choice")

    async def check() -> None:
        config = load_config(tmp_path)
        config.demo = True
        config.profiles["review"] = AgentProfile(mode="reviewer", model="review-only", provider="named")
        harness = Harness(config)
        try:
            await harness.set_agent("review")
            assert harness.agent.provider.model == "review-only"
            await harness.set_agent("assistant")
            assert harness.agent.provider.model == "shared-choice"
            assert scoped.model() == "shared-choice"
        finally:
            await harness.close()

    asyncio.run(check())


def test_named_live_reads_same_env_and_revisions_without_key_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ProviderRegistryStore()
    saved = store.save(ProviderRegistry(active="voice", providers={"voice": sample()}), expected="0" * 64)
    monkeypatch.setenv("TEST_NGN_KEY", "voice-key-value")

    async def check() -> None:
        settings = LiveSettings(tmp_path / "sessions.db", demo=False, active=lambda: "")
        await settings.load()
        global_snapshot = await settings.snapshot("global")
        revision = global_snapshot["revision"]
        assert isinstance(revision, str)
        await settings.change(
            GlobalVoiceInput(
                scope="global",
                revision=revision,
                preferences=VoicePreferences(enabled=True, backend_mode="hosted"),
            )
        )
        before = await settings.connection()
        assert before.revision != saved.revision and before.key_configured
        assert (await settings.connection()).revision == before.revision
        assert (await settings.snapshot())["profile_name"] == "voice"
        agent = create_agent(before)
        try:
            assert agent.provider.api_key == "voice-key-value"
            assert agent.provider.model == "gpt-live-1"
        finally:
            await agent.close()
        body = WorkspaceVoiceInput(
            scope="workspace", revision=before.revision, overrides=VoiceOverrides(model="gpt-live-2")
        )
        result = await settings.change(body)
        assert result["values"]["model"] == "gpt-live-2"  # type: ignore[index]
        assert "voice-key-value" not in store.path.read_text()
        assert set(yaml.safe_load(store.path.read_text())["providers"]["voice"]) == set(
            ProviderProfile.__dataclass_fields__
        )
        with pytest.raises(Exception, match="changed"):
            await settings.change(body)
        await settings.shutdown()

    asyncio.run(check())


def test_foundry_entra_resolves_sdk_credential_and_closes_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Credential:
        closed = False

        async def get_token(self, *scopes: str) -> SimpleNamespace:
            assert scopes == ("https://ai.azure.com/.default",)
            return SimpleNamespace(token="synthetic-entra-token")

        async def close(self) -> None:
            self.closed = True

    credential = Credential()
    monkeypatch.setitem(sys.modules, "azure.identity.aio", SimpleNamespace(DefaultAzureCredential=lambda: credential))
    profile = ProviderProfile(
        kind="foundry",
        auth="entra",
        api="responses",
        base_url="http://127.0.0.1:8888/openai/v1",
    )
    config = HarnessConfig(
        workspace=tmp_path,
        provider="foundry",
        model="deployment",
        auth="entra",
        api="responses",
        base_url=profile.base_url,
    )

    async def check() -> None:
        provider = build_provider(profile, config, OpenAIAuth())
        assert await provider.auth_headers(profile.base_url + "/responses") == {
            "Authorization": "Bearer synthetic-entra-token"
        }
        await provider.close()
        assert credential.closed

    asyncio.run(check())


def test_openai_auto_prefers_named_environment_without_codex_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = sample(auth="auto", api_key_env="${MY_OPENAI_KEY}")
    config = HarnessConfig(workspace=tmp_path, api_key_env=profile.key_env)
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-account")
    monkeypatch.setenv("MY_OPENAI_KEY", "selected-account")
    provider = build_provider(profile, config, OpenAIAuth())
    assert isinstance(provider, HarnessProvider)
    provider.credentials()
    assert provider.api_key == "selected-account"
    monkeypatch.setenv("MY_OPENAI_KEY", "rotated-account")
    provider.credentials()
    assert provider.api_key == "rotated-account"
    with pytest.raises(CodexConfigError):
        build_provider(sample(auth="codex", api_key_env=""), config, OpenAIAuth())
