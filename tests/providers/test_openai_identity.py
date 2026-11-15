"""Provider identity is distinct from the shared OpenAI wire protocols."""

import asyncio
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest

from nagents.provider import CodexCredentials
from nagents.provider import OpenAIProvider
from nagents.provider import Provider
from nagents.provider import ProviderType
from nagents.provider.openai import OpenAIProvider as NativeOpenAIProvider


def test_native_import_preserves_existing_class() -> None:
    assert NativeOpenAIProvider is OpenAIProvider


def test_native_rejects_anthropic_protocol() -> None:
    with pytest.raises(ValueError, match="not messages"):
        Provider(ProviderType.OPENAI, api_key="test-key", model="gpt-4.1", api="messages")


def test_api_key_identity() -> None:
    provider = OpenAIProvider(api_key="test-key", model="gpt-4.1")
    assert provider.provider_type is ProviderType.OPENAI
    assert not provider.uses_chatgpt_auth
    assert provider.base_url == "https://api.openai.com/v1"


def test_subscription_identity() -> None:
    async def credentials() -> CodexCredentials:
        return CodexCredentials("test-token", "test-account")

    provider = OpenAIProvider(credentials, model="gpt-4.1")
    assert provider.provider_type is ProviderType.OPENAI
    assert provider.uses_chatgpt_auth
    assert provider.base_url == "https://chatgpt.com/backend-api/codex/responses"


def test_legacy_custom_endpoint_remains_compatible() -> None:
    provider = OpenAIProvider(api_key="test-key", model="third-party-model", base_url="https://example.test/v1")
    assert provider.provider_type is ProviderType.OPENAI_COMPATIBLE
    assert not provider.uses_chatgpt_auth


def test_generic_native_api_and_compatible_identity() -> None:
    native = Provider(ProviderType.OPENAI, api_key="test-key", model="gpt-4.1", api="responses")
    compatible = Provider(
        ProviderType.OPENAI_COMPATIBLE,
        api_key="test-key",
        model="third-party-model",
        base_url="https://example.test/v1",
    )
    assert native.provider_type is ProviderType.OPENAI
    assert native.base_url == "https://api.openai.com/v1"
    assert compatible.provider_type is ProviderType.OPENAI_COMPATIBLE
    assert compatible.base_url == "https://example.test/v1"


@pytest.mark.parametrize("provider_type", [ProviderType.OPENAI, ProviderType.OPENAI_COMPATIBLE])
def test_shared_model_discovery(provider_type: ProviderType) -> None:
    provider = Provider(provider_type, api_key="test-key", model="gpt-4.1")

    catalog = AsyncMock(return_value={"data": [{"id": "gpt-4.1"}]})
    with patch("nagents.provider.base.GatewayHTTPClient.get_json", catalog):
        assert asyncio.run(provider.get_model_list()) == ["gpt-4.1"]
    catalog.assert_awaited_once_with("https://api.openai.com/v1/models", {"Authorization": "Bearer test-key"})


@pytest.mark.parametrize("api", ["auto", "chat_completions", "responses", "messages"])
@pytest.mark.parametrize("base_url", ["https://example.test/v1", "https://example.test/v1/"])
def test_generic_custom_endpoint_matches_native_wrapper(api: str, base_url: str) -> None:
    generic = Provider(ProviderType.OPENAI, api_key="test-key", model="third-party", base_url=base_url, api=api)
    native = OpenAIProvider(api_key="test-key", model="third-party", base_url=base_url, api=api)
    assert generic.provider_type is native.provider_type is ProviderType.OPENAI_COMPATIBLE
    assert generic.base_url == native.base_url


@pytest.mark.parametrize("base_url", ["https://api.openai.com/v1", "https://api.openai.com/v1/"])
def test_generic_explicit_native_endpoint_keeps_identity(base_url: str) -> None:
    provider = Provider(ProviderType.OPENAI, api_key="test-key", model="gpt-4.1", base_url=base_url)
    assert provider.provider_type is ProviderType.OPENAI
