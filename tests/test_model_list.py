"""Explicit model discovery, using only fake keys and loopback HTTP fixtures."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from unittest.mock import Mock
from unittest.mock import patch

import pytest
from aiohttp import web

from nagents import ModelListError
from nagents import NagentsError
from nagents.harness.config import HarnessConfig
from nagents.harness.provider import HarnessProvider
from nagents.http import HTTPClient
from nagents.http import HTTPLogger
from nagents.provider import PlaceholderProvider
from nagents.provider import Provider
from nagents.provider import ProviderType
from nagents.provider import gateway
from nagents.provider.gateway import GatewayHTTPClient
from tests.test_gateway_provider import KEY
from tests.test_gateway_provider import LEAK
from tests.test_gateway_provider import endpoint
from tests.test_web import ControlledHarness

if TYPE_CHECKING:
    from pathlib import Path


COMPATIBLE = [ProviderType.OPENAI_COMPATIBLE, ProviderType.OPENROUTER, ProviderType.LITELLM]


def test_default_openai_catalog_route() -> None:
    async def scenario() -> None:
        async with Provider(ProviderType.OPENAI_COMPATIBLE, KEY, "selected") as provider:
            with patch.object(GatewayHTTPClient, "get_json", AsyncMock(return_value={"data": []})) as get:
                assert await provider.get_model_list() == []
                get.assert_awaited_once_with("https://api.openai.com/v1/models", {"Authorization": f"Bearer {KEY}"})
            assert provider.model == "selected" and provider.is_model_verified is None

    asyncio.run(scenario())


@pytest.mark.parametrize("provider_type", COMPATIBLE)
@pytest.mark.parametrize("prefix", ["", "/v1/", "/proxy/custom/v2/"])
def test_catalog_prefix_headers_freshness_and_identity(provider_type: ProviderType, prefix: str) -> None:
    async def scenario() -> None:
        seen: list[str] = []

        async def handle(request: web.Request) -> web.Response:
            assert request.method == "GET" and not request.query
            assert request.headers["Authorization"] == f"Bearer {KEY}"
            assert not {"ChatGPT-Account-Id", "originator", "x-api-key"} & request.headers.keys()
            if provider_type == ProviderType.OPENROUTER:
                assert request.headers["HTTP-Referer"] == "https://github.com/nagents"
            else:
                assert "HTTP-Referer" not in request.headers
            seen.append(request.path)
            ids = ["vendor/z:free", "models/exact", "vendor/z:free", "  unchanged  ", f"fresh-{len(seen)}"]
            return web.json_response({"data": [{"id": model, "private_metadata": LEAK} for model in ids]})

        async with (
            endpoint(handle) as url,
            Provider(provider_type, KEY, "selected", base_url=url + prefix) as provider,
        ):
            before = provider.base_url
            assert await provider.get_model_list() == ["vendor/z:free", "models/exact", "  unchanged  ", "fresh-1"]
            assert provider.base_url == before and provider.model == "selected"
            provider.base_url = url + "/changed/prefix/"
            provider.model = "new-selection"
            assert (await provider.get_model_list())[-1] == "fresh-2"
            assert seen == [prefix.rstrip("/") + "/models", "/changed/prefix/models"]
            assert provider.base_url == url + "/changed/prefix/" and provider.model == "new-selection"
            assert provider.is_model_verified is None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "body",
    [
        {},
        [],
        {"data": None},
        {"data": {}},
        {"data": [None]},
        {"data": ["model"]},
        {"data": [{}]},
        {"data": [{"id": 123}]},
        {"data": [{"id": True}]},
        {"data": [{"id": ""}]},
        {"data": [{"id": "  "}]},
        {"data": [{"id": "bad\x00id"}]},
        {"data": [{"id": "bad\nid"}]},
        {"data": [{"id": "bad\x7fid"}]},
        {"data": [{"id": "bad\u202eid"}]},
        {"data": [{"id": "bad\ud800id"}]},
        {"error": {"message": LEAK}},
        {"data": [], "error": {"message": LEAK}},
        {"data": [], "error": None},
    ],
)
def test_malformed_catalog_is_not_an_empty_success(body: object, caplog: pytest.LogCaptureFixture) -> None:
    async def scenario() -> None:
        async def handle(request: web.Request) -> web.Response:
            return web.json_response(body)

        async with (
            endpoint(handle) as url,
            Provider(ProviderType.OPENAI_COMPATIBLE, KEY, "selected", base_url=url) as provider,
        ):
            with pytest.raises(ModelListError) as error:
                await provider.get_model_list()
            assert isinstance(error.value, NagentsError)
            assert LEAK not in str(error.value) + caplog.text and KEY not in str(error.value) + caplog.text
            assert provider.is_model_verified is None and provider.model == "selected"

    asyncio.run(scenario())


@pytest.mark.parametrize("body", [b"not-json", b"\xff", b'{"data":[],"data":[]}', b'{"data":[],"extra":NaN}'])
def test_invalid_json_encoding_or_duplicate_keys(body: bytes) -> None:
    async def scenario() -> None:
        async def handle(request: web.Request) -> web.Response:
            return web.Response(body=body)

        async with (
            endpoint(handle) as url,
            Provider(ProviderType.OPENAI_COMPATIBLE, KEY, "selected", base_url=url) as provider,
        ):
            with pytest.raises(ModelListError):
                await provider.get_model_list()

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [301, 302, 307, 308, 401, 403, 429, 500])
def test_catalog_errors_redacted_no_redirects_or_raw_logging(status: int, caplog: pytest.LogCaptureFixture) -> None:
    async def scenario() -> None:
        paths: list[str] = []

        async def handle(request: web.Request) -> web.Response:
            paths.append(request.path)
            return web.Response(status=status, reason=LEAK, text=LEAK, headers={"Location": "/sink", "Secret": KEY})

        async with (
            endpoint(handle) as url,
            Provider(ProviderType.OPENAI_COMPATIBLE, KEY, "selected", base_url=url) as provider,
        ):
            http_logger = Mock(spec=HTTPLogger)
            provider.set_http_logger(http_logger)
            with pytest.raises(ModelListError) as error:
                await provider.get_model_list()
            assert paths == ["/models"] and not http_logger.mock_calls
            assert KEY not in repr(error.value) + caplog.text and LEAK not in repr(error.value) + caplog.text
            assert error.value.__suppress_context__

    asyncio.run(scenario())


def test_catalog_ignores_proxy_and_cookies(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(name, "")

    async def scenario() -> None:
        async def handle(request: web.Request) -> web.Response:
            assert "Cookie" not in request.headers
            return web.json_response({"data": []}, headers={"Set-Cookie": "catalog=not-a-credential; Path=/"})

        async with (
            endpoint(handle) as url,
            Provider(ProviderType.OPENAI_COMPATIBLE, KEY, "selected", base_url=url) as provider,
        ):
            assert await provider.get_model_list() == []
            assert await provider.get_model_list() == []

    asyncio.run(scenario())


def test_catalog_response_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gateway, "MAX_RESPONSE_BYTES", 128)

    async def scenario() -> None:
        async def handle(request: web.Request) -> web.StreamResponse:
            response = web.StreamResponse()
            await response.prepare(request)
            await response.write(json.dumps({"data": [{"id": "x" * 256}]}).encode())
            return response

        async with (
            endpoint(handle) as url,
            Provider(ProviderType.OPENAI_COMPATIBLE, KEY, "selected", base_url=url) as provider,
        ):
            with pytest.raises(ModelListError):
                await provider.get_model_list()

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel", [False, True])
def test_catalog_timeout_or_cancellation_closes_connection(cancel: bool) -> None:
    async def scenario() -> None:
        started, disconnected, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def handle(request: web.Request) -> web.StreamResponse:
            response = web.StreamResponse()
            await response.prepare(request)
            await response.write(b'{"data":[')
            started.set()
            while not release.is_set():
                if request.transport is None or request.transport.is_closing():
                    disconnected.set()
                    break
                await asyncio.sleep(0.01)
            return response

        async with (
            endpoint(handle) as url,
            Provider(ProviderType.OPENAI_COMPATIBLE, KEY, "selected", base_url=url, timeout=2) as provider,
        ):
            task = asyncio.create_task(provider.get_model_list())
            try:
                await asyncio.wait_for(started.wait(), 5)
                if cancel:
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    with pytest.raises(ModelListError):
                        await task
                await asyncio.wait_for(disconnected.wait(), 5)
                assert provider.is_model_verified is None
            finally:
                release.set()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "invalid",
    [
        "https://user:secret@example.invalid/v1",
        "https://example.invalid/v1?key=secret",
        "https://example.invalid/v1#fragment",
        "https://example.invalid/v1/responses",
        "file:///models",
        "https://example.invalid/\npath",
    ],
)
def test_mutable_prefix_is_validated_before_http(invalid: str) -> None:
    async def scenario() -> None:
        async with Provider(ProviderType.OPENAI_COMPATIBLE, KEY, "selected") as provider:
            provider.base_url = invalid
            with patch.object(GatewayHTTPClient, "get_json", AsyncMock()) as get:
                with pytest.raises(ModelListError) as error:
                    await provider.get_model_list()
                get.assert_not_called()
                assert invalid not in str(error.value)

    asyncio.run(scenario())


@pytest.mark.parametrize("provider_type", COMPATIBLE)
def test_verification_cache_force_and_prefix_matching(provider_type: ProviderType) -> None:
    async def scenario() -> None:
        catalog: dict[str, object] = {"data": [{"id": "models/selected"}]}
        requests = 0

        async def handle(request: web.Request) -> web.Response:
            nonlocal requests
            requests += 1
            return web.json_response(catalog)

        async with endpoint(handle) as url, Provider(provider_type, KEY, "selected", base_url=url) as provider:
            assert await provider.verify_model() is True and requests == 1
            catalog["data"] = []
            assert await provider.get_model_list() == [] and requests == 2
            assert provider.is_model_verified is True
            assert await provider.verify_model() is True and requests == 2
            assert await provider.verify_model(force=True) is False and requests == 3
            catalog["data"] = [{"id": "selected"}]
            assert await provider.get_model_list() == ["selected"] and provider.is_model_verified is False
            assert await provider.verify_model() is False and requests == 4
            assert await provider.verify_model(force=True) is True and requests == 5
            catalog["data"] = [{"id": 4}]
            assert await provider.verify_model(force=True) is False
            assert provider.model == "selected"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "provider_type",
    [
        ProviderType.ANTHROPIC,
        ProviderType.GEMINI_NATIVE,
        ProviderType.AZURE_OPENAI_COMPATIBLE,
        ProviderType.AZURE_OPENAI_COMPATIBLE_V1,
    ],
)
def test_unsupported_native_catalog_does_not_change_verification(provider_type: ProviderType) -> None:
    async def scenario() -> None:
        async with Provider(
            provider_type, KEY, "selected", base_url="http://127.0.0.1:1", api_version="fixture"
        ) as provider:
            with patch.object(GatewayHTTPClient, "get_json", AsyncMock()) as get:
                with pytest.raises(NotImplementedError):
                    await provider.get_model_list()
                get.assert_not_called()
            with patch.object(
                provider._http, "get_json", AsyncMock(return_value={"models": [{"name": "models/selected"}]})
            ) as native:
                assert await provider.verify_model() is True
                assert native.await_count == int(provider_type == ProviderType.GEMINI_NATIVE)

    asyncio.run(scenario())


def test_placeholder_catalog_rejected_without_http() -> None:
    with patch.object(GatewayHTTPClient, "get_json", AsyncMock()) as get:
        with pytest.raises(NotImplementedError, match="PlaceholderProvider"):
            asyncio.run(PlaceholderProvider().get_model_list())
        get.assert_not_called()


def test_harness_explicit_discovery_resolves_current_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        keys: list[str] = []

        async def handle(request: web.Request) -> web.Response:
            keys.append(request.headers["Authorization"])
            return web.json_response({"data": [{"id": "fixture"}]})

        async with endpoint(handle) as url:
            config = HarnessConfig(
                workspace=tmp_path, demo=False, auth="api-key", base_url=url, api_key_env="TEST_CATALOG_KEY"
            )
            async with HarnessProvider(config) as provider:
                assert provider.api_key == "deferred-until-live-request"
                monkeypatch.setenv("TEST_CATALOG_KEY", KEY)
                assert await provider.get_model_list() == ["fixture"]
                monkeypatch.setenv("TEST_CATALOG_KEY", KEY + "-rotated")
                assert await provider.get_model_list() == ["fixture"]
                assert keys == [f"Bearer {KEY}", f"Bearer {KEY}-rotated"]
                monkeypatch.delenv("TEST_CATALOG_KEY")
                with pytest.raises(ModelListError):
                    await provider.get_model_list()
                assert len(keys) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("demo", [False, True])
def test_harness_startup_no_catalog_and_demo_explicitly_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, demo: bool
) -> None:
    monkeypatch.setenv("TEST_CATALOG_KEY", KEY)

    async def scenario() -> None:
        config = HarnessConfig(
            workspace=tmp_path, data_dir=tmp_path / "data", demo=demo, auth="api-key", api_key_env="TEST_CATALOG_KEY"
        )
        harness = ControlledHarness(config)
        with (
            patch.object(GatewayHTTPClient, "get_json", AsyncMock()) as secure,
            patch.object(HTTPClient, "get_json", AsyncMock()) as ordinary,
        ):
            try:
                await harness.initialize()
                assert await harness.agent.provider.verify_model() is True
                if demo:
                    with pytest.raises(NotImplementedError, match="demo"):
                        await harness.agent.provider.get_model_list()
                secure.assert_not_called()
                ordinary.assert_not_called()
            finally:
                await harness.close()

    asyncio.run(scenario())
