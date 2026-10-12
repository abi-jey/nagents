"""Context limits use exact models and trusted bounded provider metadata."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from unittest.mock import patch

import pytest
from aiohttp import web

from nagents.compaction import ModelCompaction
from nagents.compaction import describe_compaction
from nagents.compaction import get_model_context_limit
from nagents.extensions import CompactionRequest
from nagents.harness.providers import ProviderProfile
from nagents.harness.providers import ProviderRegistry
from nagents.harness.providers import ProviderRegistryStore
from nagents.provider import OpenAIProvider
from nagents.provider import Provider
from nagents.provider import ProviderType
from nagents.provider.context import ContextWindow
from nagents.provider.gateway import GatewayHTTPClient
from nagents.provider.openai import CodexCredentials
from tests.providers.test_gateway_provider import endpoint

if TYPE_CHECKING:
    from pathlib import Path

MODEL = "eu.anthropic.claude-opus-5-5"


def test_litellm_metadata_drives_auto_threshold_and_exact_model_cache() -> None:
    async def scenario() -> None:
        calls: list[str] = []

        async def handle(request: web.Request) -> web.Response:
            calls.append(request.path)
            assert request.path == "/proxy/model/info"
            assert request.headers["Authorization"] == "Bearer test-key"
            return web.json_response(
                {
                    "data": [
                        {
                            "model_name": MODEL,
                            "model_info": {"max_input_tokens": 1_000_000, "max_output_tokens": 128_000},
                        },
                        {"model_name": "other-model", "model_info": {"max_input_tokens": 64_000}},
                    ]
                }
            )

        async with (
            endpoint(handle) as url,
            Provider(ProviderType.LITELLM, "test-key", MODEL, base_url=url + "/proxy/v1") as provider,
        ):
            windows = await asyncio.gather(*(provider.resolve_context_window() for _ in range(8)))
            assert windows == [ContextWindow(1_000_000, "provider")] * 8
            assert calls == ["/proxy/model/info"]
            assert get_model_context_limit(provider) == 1_000_000
            request = CompactionRequest([], "s", provider, estimated_tokens=100, actual_input_tokens=699_999)
            assert not await ModelCompaction().should_compact(request)
            request.actual_input_tokens = 700_000
            assert await ModelCompaction().should_compact(request)
            assert describe_compaction(ModelCompaction(), provider) == {
                "trigger": "auto",
                "token_budget": 1_000_000,
                "token_threshold": 700_000,
            }
            provider.model = "other-model"
            assert await provider.resolve_context_window() == ContextWindow(64_000, "provider")
            assert len(calls) == 1
            provider.base_url = url + "/different"
            assert provider.cached_context_window().source == "heuristic"

    asyncio.run(scenario())


def test_overrides_round_trip_and_take_precedence_without_network(tmp_path: Path) -> None:
    profile = ProviderProfile(
        kind="litellm",
        base_url="https://example.invalid/v1",
        context_window=200_000,
        model_context_windows={MODEL: 1_000_000},
    )
    store = ProviderRegistryStore(tmp_path / "providers.json")
    store.save(ProviderRegistry(active="gateway", providers={"gateway": profile}), expected="0" * 64)
    assert store.load().providers["gateway"] == profile

    async def scenario() -> None:
        async with Provider(ProviderType.LITELLM, "test-key", MODEL, base_url=profile.base_url) as provider:
            provider.configure_context_window(profile.context_window, profile.model_context_windows)
            with patch.object(provider, "_fetch_context_metadata", AsyncMock()) as fetch:
                assert await provider.resolve_context_window() == ContextWindow(1_000_000, "configured")
                provider.model = "another-model"
                assert await provider.resolve_context_window() == ContextWindow(200_000, "configured")
                fetch.assert_not_awaited()

    asyncio.run(scenario())


@pytest.mark.parametrize("bad", [True, -1, 1.2, "1000", 1_000_000_001])
def test_context_override_validation(bad: object) -> None:
    from typing import cast

    with pytest.raises(ValueError, match="context_window"):
        ProviderProfile(kind="openai", context_window=cast("int", bad)).validate()
    with pytest.raises(ValueError, match="context_window"):
        ProviderProfile(kind="openai", model_context_windows={MODEL: cast("int", bad)}).validate()


def test_litellm_denied_metadata_is_cached_and_labelled_heuristic() -> None:
    async def scenario() -> None:
        calls = 0

        async def handle(request: web.Request) -> web.Response:
            nonlocal calls
            calls += 1
            return web.json_response({"error": "private upstream text"}, status=403)

        async with endpoint(handle) as url, Provider(ProviderType.LITELLM, "test-key", MODEL, base_url=url) as provider:
            for _ in range(3):
                assert await provider.resolve_context_window() == ContextWindow(100_000, "heuristic")
            assert calls == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "provider_type,model,body,header,path",
    [
        (
            ProviderType.ANTHROPIC,
            MODEL,
            {"id": MODEL, "max_input_tokens": 1_000_000, "max_tokens": 128_000},
            "x-api-key",
            "/v1/models/" + MODEL,
        ),
        (
            ProviderType.GEMINI_NATIVE,
            "gemini-example",
            {"name": "models/gemini-example", "inputTokenLimit": 1_048_576},
            "x-goog-api-key",
            "/v1/models/gemini-example",
        ),
        (
            ProviderType.OPENROUTER,
            "vendor/model",
            {"data": [{"id": "vendor/model", "context_length": 512_000}]},
            "Authorization",
            "/v1/models",
        ),
    ],
)
def test_native_provider_metadata(
    provider_type: ProviderType, model: str, body: dict[str, object], header: str, path: str
) -> None:
    async def scenario() -> None:
        async def handle(request: web.Request) -> web.Response:
            assert request.path == path
            assert header in request.headers
            assert not request.query
            return web.json_response(body)

        async with (
            endpoint(handle) as url,
            Provider(provider_type, "test-key", model, base_url=url + "/v1") as provider,
        ):
            window = await provider.resolve_context_window()
            assert window.source == "provider"
            assert window.tokens in {1_000_000, 1_048_576, 512_000}

    asyncio.run(scenario())


def test_explicit_catalog_metadata_is_consumed_without_another_request() -> None:
    async def scenario() -> None:
        async with Provider(ProviderType.OPENAI_COMPATIBLE, "test-key", "alias") as provider:
            with patch.object(
                GatewayHTTPClient,
                "get_json",
                AsyncMock(return_value={"data": [{"id": "alias", "context_window": 777_000}]}),
            ) as get:
                assert await provider.get_model_list() == ["alias"]
                assert await provider.resolve_context_window() == ContextWindow(777_000, "provider")
                get.assert_awaited_once()

    asyncio.run(scenario())


def test_compaction_snapshot_retains_exact_configured_budget() -> None:
    provider = Provider(ProviderType.OPENAI_COMPATIBLE, "test-key", "model", context_window=1_000_000)
    assert describe_compaction(ModelCompaction(token_budget=600_001), provider) == {
        "trigger": "tokens",
        "token_budget": 600_001,
        "token_threshold": 420_000,
    }
    assert describe_compaction(ModelCompaction(token_threshold=123), provider) == {
        "trigger": "tokens",
        "token_threshold": 123,
    }


def test_changed_credential_cannot_reuse_same_endpoint_alias_metadata() -> None:
    async def scenario() -> None:
        calls = 0

        async def handle(request: web.Request) -> web.Response:
            nonlocal calls
            calls += 1
            if request.headers["Authorization"] != "Bearer first-key":
                return web.Response(status=403)
            return web.json_response({"data": [{"model_name": MODEL, "model_info": {"max_input_tokens": 1_000_000}}]})

        async with (
            endpoint(handle) as url,
            Provider(ProviderType.LITELLM, "first-key", MODEL, base_url=url) as provider,
        ):
            assert (await provider.resolve_context_window()).tokens == 1_000_000
            provider.api_key = "second-key"
            assert await provider.resolve_context_window() == ContextWindow(100_000, "heuristic")
            assert calls == 2
            assert await provider.resolve_context_window() == ContextWindow(100_000, "heuristic")
            assert calls == 2

    asyncio.run(scenario())


def test_metadata_deadline_includes_deferred_credentials() -> None:
    async def scenario() -> None:
        async def credentials() -> CodexCredentials:
            await asyncio.Event().wait()
            return CodexCredentials("unreachable")

        async with OpenAIProvider(credentials, model="unknown", timeout=0.02) as provider:
            with patch.object(GatewayHTTPClient, "get_json", AsyncMock()) as get:
                result = await asyncio.wait_for(provider.resolve_context_window(), timeout=0.5)
                assert result.source == "heuristic"
                get.assert_not_awaited()

    asyncio.run(scenario())


def test_bearer_catalog_context_uses_only_the_actual_request_token() -> None:
    async def scenario() -> None:
        calls = 0

        async def credentials() -> str:
            nonlocal calls
            calls += 1
            return f"catalog-token-{calls}"

        async def handle(request: web.Request) -> web.Response:
            assert request.path == "/models"
            assert request.headers["Authorization"] == "Bearer catalog-token-1"
            return web.json_response({"data": [{"id": "alias", "max_input_tokens": 1_000_000}]})

        async with (
            endpoint(handle) as url,
            Provider(
                ProviderType.OPENAI_COMPATIBLE, model="alias", base_url=url, bearer_token_provider=credentials
            ) as provider,
        ):
            assert await provider.get_model_list() == ["alias"]
            for _ in range(3):
                assert await provider.resolve_context_window() == ContextWindow(1_000_000, "provider")
            assert calls == 1

    asyncio.run(scenario())
