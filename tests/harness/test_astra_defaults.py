"""Astra defaults retain explicit model and provider contract choices."""

import asyncio
from pathlib import Path

import pytest

from nagents.harness.config import HarnessConfig
from nagents.harness.provider import HarnessProvider
from nagents.harness.providers import ProviderProfile
from nagents.live import LiveConfig


def test_new_assistant_and_hosted_voice_reasoning_default_to_astra(tmp_path: Path) -> None:
    assert HarnessConfig(workspace=tmp_path).model == "gpt-6-astra"
    assert LiveConfig().backend_model == "gpt-6-astra"
    assert HarnessConfig(workspace=tmp_path, model="gpt-6-luna").model == "gpt-6-luna"
    assert LiveConfig(backend_model="gpt-5.6-luna").backend_model == "gpt-5.6-luna"


@pytest.mark.parametrize(
    "kind,api,base_url,model,expected",
    [
        ("openai", "auto", "", "gpt-6-astra", "responses"),
        ("openai_compatible", "auto", "", "gpt-6-astra", "responses"),
        ("openai", "chat_completions", "", "gpt-6-astra", "chat_completions"),
        ("openai", "responses", "", "gpt-6-astra", "responses"),
        ("openai", "auto", "https://example.test/v1", "gpt-6-astra", "chat_completions"),
        ("openai_compatible", "auto", "https://example.test/v1", "gpt-6-astra", "chat_completions"),
        ("openai", "auto", "", "gpt-6-luna", "chat_completions"),
    ],
)
def test_astra_implicit_openai_tool_route_respects_explicit_contracts(
    tmp_path: Path, kind: str, api: str, base_url: str, model: str, expected: str
) -> None:
    config = HarnessConfig(
        workspace=tmp_path,
        model=model,
        provider="chosen",
        providers={
            "chosen": ProviderProfile(kind=kind, auth="api-key", api=api, base_url=base_url),
        },
    )
    provider = HarnessProvider(config)
    try:
        assert provider.api == expected
        assert provider.model == model
        assert config.provider_profile().api == api
    finally:
        asyncio.run(provider.close())
