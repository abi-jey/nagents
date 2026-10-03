"""Model changes retain the selected contract while refreshing OpenAI auto routing."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from nagents.harness.config import AgentProfile
from nagents.harness.config import HarnessConfig
from nagents.harness.provider import HarnessProvider
from nagents.harness.runtime import Harness
from nagents.provider.gateway import GatewayHTTPClient
from nagents.provider.openai import CodexCredentials
from nagents.provider.openai import OpenAIProvider
from nagents.web.settings import SettingsValues
from nagents.web.settings import WebSettings
from tests.support.web import client_app

if TYPE_CHECKING:
    from pathlib import Path


def configuration(path: Path, *, kind: str = "openai", api: str = "auto", base_url: str = "") -> HarnessConfig:
    return HarnessConfig(
        workspace=path,
        data_dir=path / "state",
        demo=True,
        provider=kind,
        auth="api-key",
        api=api,
        base_url=base_url,
        model="gpt-6-luna",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,api,endpoint,expected",
    [
        ("openai", "auto", "", "responses"),
        ("openai_compatible", "auto", "", "responses"),
        ("openai_compatible", "chat_completions", "", "chat_completions"),
        ("openai_compatible", "auto", "https://api.openai.com/v1", "chat_completions"),
        ("openai", "chat_completions", "", "chat_completions"),
        ("openai", "responses", "", "responses"),
        ("openai", "auto", "https://example.test/v1", "chat_completions"),
        ("openai_compatible", "auto", "https://example.test/v1", "chat_completions"),
        ("openai_compatible", "responses", "https://example.test/v1", "responses"),
        ("anthropic", "auto", "", "messages"),
        ("gemini", "auto", "", "auto"),
    ],
)
async def test_model_assignment_refreshes_only_automatic_openai_routing(
    tmp_path: Path, kind: str, api: str, endpoint: str, expected: str
) -> None:
    config = configuration(tmp_path, kind=kind, api=api, base_url=endpoint)
    provider = HarnessProvider(config)
    original_api = provider.api
    if kind in {"openai", "openai_compatible"} and api == "auto" and not endpoint:
        # Both initial and future Responses calls need the bounded no-redirect
        # transport, even when this provider starts with another model.
        assert isinstance(provider._http, GatewayHTTPClient)
    transport = provider._http
    try:
        for _ in range(2):
            provider.model = "gpt-6-astra"
            assert provider.api == expected and provider._http is transport
            assert provider.model == "gpt-6-astra"
            provider.model = "gpt-6-luna"
            assert provider.api == original_api
        assert config.model == "gpt-6-luna" and config.api == api
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["model", "profile", "reconfigure", "settings", "sync"])
async def test_harness_model_selection_paths_recompute_the_automatic_contract(tmp_path: Path, path: str) -> None:
    config = configuration(tmp_path)
    config.profiles = {"reasoning": AgentProfile(model="gpt-6-astra")}
    harness = Harness(config)
    provider = harness.agent.provider
    try:
        assert provider.api == "chat_completions"
        for model, expected in (("gpt-6-astra", "responses"), ("gpt-6-luna", "chat_completions")):
            if path == "model":
                await harness.set_model(model)
            elif path == "profile":
                await harness.set_agent("reasoning" if model == "gpt-6-astra" else "assistant")
            elif path == "reconfigure":
                await harness.reconfigure_provider(replace(harness.config, model=model))
            elif path == "settings":
                SettingsValues.current(harness).model_copy(update={"model": model}).apply(harness)
            else:
                settings = WebSettings(harness)
                settings.values = settings.values.model_copy(update={"model": model})
                settings.sync_provider()
            assert harness.agent.provider is provider
            assert (provider.model, provider.api) == (model, expected)
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_initial_agent_profile_model_selects_its_automatic_contract(tmp_path: Path) -> None:
    config = configuration(tmp_path)
    config.agent = "reasoning"
    config.profiles = {"reasoning": AgentProfile(model="gpt-6-astra")}
    harness = Harness(config)
    try:
        assert (harness.agent.provider.model, harness.agent.provider.api) == ("gpt-6-astra", "responses")
        await harness.set_agent("assistant")
        assert (harness.agent.provider.model, harness.agent.provider.api) == ("gpt-6-luna", "chat_completions")
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_saved_web_model_changes_keep_the_contract_after_restart(tmp_path: Path) -> None:
    expected_model, expected_api = "gpt-6-luna", "chat_completions"
    for model, api in (("gpt-6-astra", "responses"), ("gpt-6-luna", "chat_completions")):
        async with client_app(tmp_path, config=configuration(tmp_path)) as (_, client, headers, harnesses):
            provider = harnesses[0].agent.provider
            assert (provider.model, provider.api) == (expected_model, expected_api)
            before = (await client.get("/api/settings", headers=headers)).json()
            saved = await client.post(
                "/api/settings",
                headers=headers,
                json={"revision": before["revision"], "values": {**before["values"], "model": model}},
            )
            assert saved.status_code == 200, saved.text
            assert (provider.model, provider.api) == (model, api)
            expected_model, expected_api = model, api
    async with client_app(tmp_path, config=configuration(tmp_path)) as (_, _, _, harnesses):
        assert (harnesses[0].agent.provider.model, harnesses[0].agent.provider.api) == (
            expected_model,
            expected_api,
        )


@pytest.mark.asyncio
async def test_native_chatgpt_provider_retains_its_responses_contract() -> None:
    async def credentials() -> CodexCredentials:
        raise AssertionError("Selecting a model must not read credentials")

    provider = OpenAIProvider(credentials, model="gpt-6-luna")
    try:
        for model in ("gpt-6-astra", "gpt-6-luna"):
            provider.model = model
            assert provider.api == "responses" and provider.uses_chatgpt_auth
    finally:
        await provider.close()
