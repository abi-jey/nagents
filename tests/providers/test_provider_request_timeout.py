"""Named request deadlines use existing provider transports without model calls."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING
from typing import cast
from unittest.mock import AsyncMock

import aiohttp
import pytest
import yaml
from fastapi import HTTPException

from nagents.harness.auth import OpenAIAuth
from nagents.harness.config import AgentProfile
from nagents.harness.config import HarnessConfig
from nagents.harness.config import load_config
from nagents.harness.connection import build_live_provider
from nagents.harness.connection import build_provider
from nagents.harness.provider import HarnessProvider
from nagents.harness.providers import ProviderProfile
from nagents.harness.providers import ProviderRegistry
from nagents.harness.providers import ProviderRegistryStore
from nagents.harness.providers import ScopedProviderRegistryStore
from nagents.harness.providers import validate_request_timeout
from nagents.harness.runtime import Harness
from nagents.live import LiveConfig
from nagents.provider import OpenAIProvider
from nagents.provider import Provider
from nagents.types import RetryConfig
from nagents.web.app import _provider_profile
from tests.providers.test_openai_local import write_api_key_auth
from tests.providers.test_openai_local import write_oauth_auth
from tests.support.web import client_app

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def no_external_requests(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-fixture"))
    monkeypatch.setattr(OpenAIAuth, "logged_in", lambda self: False)
    monkeypatch.setattr(OpenAIAuth, "credentials", AsyncMock(side_effect=AssertionError("No credential read")))
    monkeypatch.setattr(aiohttp.ClientSession, "_request", AsyncMock(side_effect=AssertionError("No network request")))


@pytest.mark.parametrize("value", [1, 19.5, 120, 0.001, 5e-324, 1e308])
def test_request_timeout_accepts_positive_finite_numbers(value: float) -> None:
    profile = ProviderProfile(kind="openai", request_timeout=value)
    profile.validate()
    assert validate_request_timeout(value) == value
    assert _provider_profile({"kind": "openai", "request_timeout": value}).request_timeout == value


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(False, id="false"),
        pytest.param(True, id="true"),
        pytest.param(None, id="null"),
        pytest.param("120", id="string"),
        pytest.param(0, id="zero"),
        pytest.param(-1, id="negative"),
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="infinity"),
        pytest.param(float("-inf"), id="negative-infinity"),
        pytest.param(10**400, id="overflowing-integer"),
    ],
)
def test_request_timeout_rejects_invalid_numbers_in_profiles_and_web(value: object) -> None:
    with pytest.raises(ValueError, match="positive finite"):
        ProviderProfile(kind="openai", request_timeout=cast("float", value)).validate()
    with pytest.raises(HTTPException) as rejected:
        _provider_profile({"kind": "openai", "request_timeout": value})
    assert rejected.value.status_code == 422


def test_legacy_yaml_omission_defaults_to_120_and_round_trips_numeric_seconds(tmp_path: Path) -> None:
    store = ProviderRegistryStore(tmp_path / "providers.yaml")
    store.path.write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "revision": "0" * 64,
                "active": "work",
                "providers": {"work": {"kind": "openai"}},
            }
        )
    )
    previous = store.load()
    assert previous.providers["work"].request_timeout == 120
    assert _provider_profile({"kind": "openai"}).request_timeout == 120
    updated = store.save(
        replace(previous, providers={"work": replace(previous.providers["work"], request_timeout=275.5)}),
        expected=previous.revision,
    )
    assert store.load() == updated
    assert yaml.safe_load(store.path.read_text())["providers"]["work"]["request_timeout"] == 275.5


ROUTES = [
    "api-key",
    "chatgpt",
    "auto-login",
    "auto-empty",
    "codex-oauth",
    "codex-key",
    "auto-codex",
    "openai_compatible",
    "foundry-key",
    "foundry-entra",
    "azure-v1-key",
    "azure-v1-entra",
    "azure-versioned",
    "openrouter",
    "anthropic",
    "gemini",
    "litellm",
]


def selected_profile(route: str, timeout: float) -> ProviderProfile:
    kind, auth, api, endpoint, version = "openai", "api-key", "auto", "", ""
    if route == "chatgpt":
        auth = "chatgpt"
    elif route.startswith("auto-"):
        auth = "auto"
    elif route.startswith("codex-"):
        auth = "codex"
    elif route.startswith(("foundry-", "azure-v1-")):
        kind = "foundry" if route.startswith("foundry") else "azure_openai_compatible_v1"
        auth = "entra" if route.endswith("entra") else "api-key"
        api, endpoint = "responses", "http://127.0.0.1:9999/openai/v1"
    elif route == "azure-versioned":
        kind, endpoint, version = "azure_openai_compatible", "http://127.0.0.1:9999", "2024-10-21"
    elif route in {"openai_compatible", "litellm"}:
        kind, endpoint, api = route, "http://127.0.0.1:9999/v1", "responses"
    elif route in {"openrouter", "anthropic", "gemini"}:
        kind = route
    return ProviderProfile(
        kind=kind, auth=auth, api=api, base_url=endpoint, api_version=version, request_timeout=timeout
    )


def assert_timeout(provider: Provider, seconds: float) -> None:
    assert provider._http._timeout.total == seconds
    assert provider._model_list_timeout == min(seconds, 30)
    if isinstance(provider, OpenAIProvider) and provider.uses_chatgpt_auth:
        assert provider._timeout == seconds


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("timeout", [19.5, 275.0])
def test_all_named_factories_preserve_selected_request_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    timeout: float,
) -> None:
    class Credential:
        closed = False

        async def get_token(self, *scopes: str) -> SimpleNamespace:
            raise AssertionError("No Entra credential read")

        async def close(self) -> None:
            self.closed = True

    credential = Credential()
    monkeypatch.setitem(sys.modules, "azure.identity.aio", SimpleNamespace(DefaultAzureCredential=lambda: credential))
    if route == "auto-login":
        monkeypatch.setattr(OpenAIAuth, "logged_in", lambda self: True)
    if route in {"codex-oauth", "auto-codex"}:
        write_oauth_auth(tmp_path / "codex-fixture")
    elif route == "codex-key":
        write_api_key_auth(tmp_path / "codex-fixture", "synthetic-key")
    profile = selected_profile(route, timeout)
    config = HarnessConfig(
        workspace=tmp_path,
        provider=profile.kind,
        auth=profile.auth,
        base_url=profile.base_url,
        api=profile.api,
        api_version=profile.api_version,
        shell_timeout=47,
    )

    async def check() -> None:
        provider = build_provider(profile, config, OpenAIAuth())
        try:
            assert_timeout(provider, timeout)
            assert provider.retry_config == RetryConfig()
            assert config.shell_timeout == 47 and config.max_tool_rounds == 30
        finally:
            await provider.close()
        if route.endswith("entra"):
            assert credential.closed

    asyncio.run(check())


def test_scope_selection_children_and_timeout_only_rebuilds(tmp_path: Path) -> None:
    store = ScopedProviderRegistryStore(tmp_path)
    global_profile = ProviderProfile(kind="openai", auth="api-key", request_timeout=45)
    local_profile = replace(global_profile, request_timeout=9)
    store.global_store.save(ProviderRegistry(active="main", providers={"main": global_profile}), expected="0" * 64)
    store.workspace_store.save(ProviderRegistry(providers={"local": local_profile}), expected="0" * 64)
    config = load_config(tmp_path)
    config.profiles["review"] = AgentProfile(mode="reviewer", provider="local")

    async def check() -> None:
        harness = Harness(config)
        children: list[Harness] = []
        try:
            await harness.initialize(create_session=False)
            assert_timeout(harness.agent.provider, 45)
            for name, expected in [("assistant", 45), ("review", 9)]:
                child = harness.tasks._create_child(name)
                children.append(child)
                await child.initialize(create_session=False)
                assert_timeout(child.agent.provider, expected)
            current = harness.agent.provider
            old_session = await current._http._get_session()  # Construct a session without sending a request.
            global_state = store.load_scope("global")
            changed = await harness.save_provider(
                "main", replace(global_profile, request_timeout=275), global_state.revision, "global"
            )
            assert old_session.closed
            assert harness.agent.provider is not current
            assert_timeout(harness.agent.provider, 275)
            assert store.load().providers["main"].request_timeout == 275
            current = harness.agent.provider
            local_state = store.load_scope("workspace")
            await harness.save_provider(
                "local", replace(local_profile, request_timeout=11), local_state.revision, "workspace"
            )
            assert harness.agent.provider is current, (
                "editing an inactive connection must not replace the active provider"
            )
            await harness.set_agent("review")
            assert_timeout(harness.agent.provider, 11)
            await harness.set_agent("assistant")
            assert_timeout(harness.agent.provider, 275)
            local_state = store.load_scope("workspace")
            selected = await harness.activate_provider("local", local_state.revision, "workspace")
            assert_timeout(harness.agent.provider, 11)
            await harness.inherit_provider(selected.revision)
            assert_timeout(harness.agent.provider, 275)
            assert store.load_scope("global").revision == changed.revision
            restarted = Harness(load_config(tmp_path))
            try:
                assert_timeout(restarted.agent.provider, 275)
            finally:
                await restarted.close()
        finally:
            for child in children:
                await child.close()
            await harness.close()

    asyncio.run(check())


@pytest.mark.parametrize("auth", ["chatgpt", "api-key"])
def test_named_chatgpt_login_rebuild_and_logout_keep_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    auth: str,
) -> None:
    store = ScopedProviderRegistryStore(tmp_path)
    store.global_store.save(
        ProviderRegistry(
            active="main", providers={"main": ProviderProfile(kind="openai", auth=auth, request_timeout=77)}
        ),
        expected="0" * 64,
    )
    monkeypatch.setattr(OpenAIAuth, "logout", lambda self: None)

    async def check() -> None:
        harness = Harness(load_config(tmp_path))
        try:
            await harness.initialize(create_session=False)
            await harness._use_chatgpt()
            assert isinstance(harness.agent.provider, OpenAIProvider) and harness.agent.provider.uses_chatgpt_auth
            assert_timeout(harness.agent.provider, 77)
            await harness.logout()
            assert_timeout(harness.agent.provider, 77)
            assert isinstance(harness.agent.provider, OpenAIProvider if auth == "chatgpt" else HarnessProvider)
        finally:
            await harness.close()

    asyncio.run(check())


@pytest.mark.parametrize("route", ["api-key", "openai_compatible", "foundry-key", "azure-v1-key"])
def test_voice_provider_http_setting_does_not_change_live_budgets(route: str) -> None:
    profile = selected_profile(route, 17)
    options = LiveConfig(delegation="client", backend_timeout=73, close_timeout=13)

    async def check() -> None:
        provider = build_live_provider(profile, options, "synthetic-key", "gpt-live-1")
        try:
            assert_timeout(provider, 17)
            assert provider.live_config is options
            assert options.backend_timeout == 73 and options.close_timeout == 13
            assert provider.retry_config.max_retries == 0
        finally:
            await provider.close()

    asyncio.run(check())


def test_web_profile_updates_round_trip_and_rebuild_without_a_timeout_env_alias(tmp_path: Path) -> None:
    async def check() -> None:
        config = HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "data", auth="api-key")
        async with client_app(tmp_path, config=config) as (_, client, headers, harnesses):
            current = (await client.get("/api/provider-scopes/workspace/providers", headers=headers)).json()
            profile = {"kind": "openai", "auth": "api-key", "request_timeout": 13.25}
            for value in [False, None, "120", 0, -1]:
                rejected = await client.put(
                    "/api/provider-scopes/workspace/providers/work",
                    headers=headers,
                    json={"revision": current["revision"], "profile": {**profile, "request_timeout": value}},
                )
                assert rejected.status_code == 422
            saved = await client.put(
                "/api/provider-scopes/workspace/providers/work",
                headers=headers,
                json={"revision": current["revision"], "profile": profile},
            )
            assert saved.status_code == 200, saved.text
            current = saved.json()
            assert current["providers"]["work"]["request_timeout"] == 13.25
            assert_timeout(harnesses[0].agent.provider, 13.25)
            changed = await client.put(
                "/api/provider-scopes/workspace/providers/work",
                headers=headers,
                json={"revision": current["revision"], "profile": {**profile, "request_timeout": 401}},
            )
            assert changed.status_code == 200, changed.text
            assert changed.json()["providers"]["work"]["request_timeout"] == 401
            assert_timeout(harnesses[0].agent.provider, 401)
            assert "request_timeout" not in vars(harnesses[0].config)
            stale = await client.put(
                "/api/provider-scopes/workspace/providers/work",
                headers=headers,
                json={"revision": current["revision"], "profile": profile},
            )
            assert stale.status_code == 409

    asyncio.run(check())


def test_request_timeout_is_not_a_global_harness_or_environment_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NGN_REQUEST_TIMEOUT", "999")
    config = load_config(tmp_path)
    assert "request_timeout" not in vars(config)
    provider = build_provider(ProviderProfile(kind="openai"), config, OpenAIAuth())
    assert_timeout(provider, 120)
    explicit = tmp_path / "config.yaml"
    explicit.write_text("request_timeout: 999\n")
    with pytest.raises(ValueError, match="request_timeout"):
        load_config(tmp_path, explicit)
