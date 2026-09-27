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

from nagents.harness.auth import OpenAIAuth
from nagents.harness.config import AgentProfile
from nagents.harness.config import HarnessConfig
from nagents.harness.config import load_config
from nagents.harness.connection import build_provider
from nagents.harness.provider import HarnessProvider
from nagents.harness.providers import LiveProfile
from nagents.harness.providers import ProviderProfile
from nagents.harness.providers import ProviderRegistry
from nagents.harness.providers import ProviderRegistryStore
from nagents.harness.runtime import Harness
from nagents.provider.openai import CodexConfigError
from nagents.web.live import create_agent
from nagents.web.live_settings import LiveSettings
from nagents.web.live_settings import LiveSettingsInput
from nagents.web.live_settings import LiveValues


def sample(**changes: object) -> ProviderProfile:
    profile = ProviderProfile(kind="openai", model="gpt-4.1", auth="api-key", api_key_env="${TEST_NGN_KEY}")
    return replace(profile, **changes)  # type: ignore[arg-type]


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
        store.save(ProviderRegistry(active="work", providers={"work": sample(model="other")}), expected="0" * 64)
    second = store.save(
        ProviderRegistry(active="work", providers={"work": sample(model="other")}),
        expected=selected.revision,
    )
    assert ProviderRegistryStore().load() == second


def test_concurrent_provider_saves_reject_stale_revision(tmp_path: Path) -> None:
    store = ProviderRegistryStore(tmp_path / "providers.yaml")
    barrier = Barrier(2)

    def save(model: str) -> str:
        barrier.wait()
        try:
            result = store.save(
                ProviderRegistry(active="chosen", providers={"chosen": sample(model=model)}),
                expected="0" * 64,
            )
        except ValueError as error:
            return str(error)
        return result.revision

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(save, "first")
        second = pool.submit(save, "second")
        outcomes = [first.result(), second.result()]
    assert sum(value == store.load().revision for value in outcomes) == 1
    assert sum("changed" in value for value in outcomes) == 1
    assert store.load().providers["chosen"].model in {"first", "second"}


def test_provider_id_override_resolves_its_model_unless_explicitly_overridden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ProviderRegistryStore()
    store.save(
        ProviderRegistry(
            active="first",
            providers={
                "first": sample(model="first-model"),
                "second": sample(
                    kind="anthropic", model="second-model", auth="api-key", api_key_env="ANTHROPIC_API_KEY"
                ),
            },
        ),
        expected="0" * 64,
    )
    monkeypatch.setenv("NGN_PROVIDER_ID", "second")
    selected = load_config(tmp_path)
    assert (selected.provider_id, selected.provider, selected.model) == ("second", "anthropic", "second-model")
    monkeypatch.setenv("NGN_MODEL", "explicit-model")
    assert load_config(tmp_path).model == "explicit-model"


@pytest.mark.parametrize(
    "fields",
    [
        {"api_key_env": "literal-key!"},
        {"auth": "chatgpt", "base_url": "https://example.com/v1"},
        {"kind": "foundry", "base_url": ""},
        {"kind": "foundry", "base_url": "http://example.com/openai/v1", "auth": "entra"},
        {"kind": "anthropic", "live": LiveProfile(enabled=True)},
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
                "second": sample(kind="openrouter", model="openrouter/auto", api_key_env="ALT_NGN_KEY"),
            },
        ),
        expected="0" * 64,
    )

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
            assert (harness.config.provider_id, harness.config.model) == ("second", "openrouter/auto")
            monkeypatch.setenv("ALT_NGN_KEY", "second-token")
            harness.agent.provider.credentials()  # type: ignore[attr-defined]
            assert harness.agent.provider.api_key == "second-token"
            await harness.set_agent("assistant")
            assert harness.config.provider_id == "first"
        finally:
            await harness.close()

    asyncio.run(check())


def test_named_live_reads_same_env_and_revisions_without_key_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ProviderRegistryStore()
    saved = store.save(
        ProviderRegistry(active="voice", providers={"voice": sample(live=LiveProfile(enabled=True))}), expected="0" * 64
    )
    monkeypatch.setenv("TEST_NGN_KEY", "voice-key-value")

    async def check() -> None:
        settings = LiveSettings(tmp_path / "sessions.db", demo=False, active=lambda: "")
        await settings.load()
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
        values = LiveValues.model_validate({**before.values.model_dump(), "model": "gpt-live-2"})
        result = await settings.change(LiveSettingsInput(revision=before.revision, values=values))
        assert result["values"]["model"] == "gpt-live-2"  # type: ignore[index]
        assert "voice-key-value" not in store.path.read_text()
        with pytest.raises(Exception, match="changed"):
            await settings.change(LiveSettingsInput(revision=before.revision, values=values))
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
        model="deployment",
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
