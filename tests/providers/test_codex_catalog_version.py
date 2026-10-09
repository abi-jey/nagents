"""Stable-version freshness is independent of model credentials and availability."""

from __future__ import annotations

import asyncio
import gc
import logging
from contextlib import asynccontextmanager
from time import monotonic
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from weakref import ref

import pytest
from aiohttp import web

from nagents.provider import _codex_catalog as catalog
from nagents.provider import openai
from nagents.provider.openai import CodexCredentials
from nagents.provider.openai import OpenAIProvider
from tests.support.hang_guard import HANG_GUARD

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from collections.abc import Awaitable
    from collections.abc import Callable


@asynccontextmanager
async def endpoint(handler: Callable[[web.Request], Awaitable[web.StreamResponse]]) -> AsyncIterator[str]:
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handler)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        yield f"http://127.0.0.1:{runner.addresses[0][1]}"
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_each_catalog_read_revalidates_metadata_and_uses_new_visible_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata_requests: list[str] = []
    model_versions: list[str] = []
    release = "0.163.0"
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")

    async def handle(request: web.Request) -> web.Response:
        if request.path == "/metadata":
            assert (
                not {"Authorization", "ChatGPT-Account-Id", "x-openai-internal-codex-residency", "x-api-key", "Cookie"}
                & request.headers.keys()
            )
            assert request.headers["User-Agent"] == "ngn-catalog-version"
            etag = request.headers.get("If-None-Match", "")
            metadata_requests.append(etag)
            if etag == f'"{release}"':
                return web.Response(status=304)
            return web.json_response(
                {"name": "@openai/codex", "version": release},
                headers={"ETag": f'"{release}"', "Set-Cookie": "metadata=discard-me"},
            )
        assert request.path == "/models"
        assert request.headers["Authorization"] == "Bearer fake-private-token"
        assert request.headers["ChatGPT-Account-Id"] == "fake-account"
        assert request.headers["x-openai-internal-codex-residency"] == "eu"
        assert request.headers["originator"] == "ngn"
        assert request.headers["User-Agent"] == openai.USER_AGENT
        assert "Cookie" not in request.headers
        version = request.query["client_version"]
        assert request.headers["version"] == version == release
        model_versions.append(version)
        return web.json_response({"models": [{"slug": f"model-for-{version}", "visibility": "list"}]})

    async with endpoint(handle) as base:
        monkeypatch.setattr(catalog, "METADATA_URL", base + "/metadata")
        monkeypatch.setattr(catalog, "_versions", catalog.CatalogVersion())
        monkeypatch.setattr(openai, "CODEX_MODELS_ENDPOINT", base + "/models")
        credentials = AsyncMock(return_value=CodexCredentials("fake-private-token", "fake-account", "eu"))
        async with OpenAIProvider(credentials, model="keep-selected") as provider:
            assert await provider.get_model_list() == ["model-for-0.163.0"]
            release = "0.164.0"
            assert await provider.get_model_list() == ["model-for-0.164.0"]
            assert await provider.get_model_list() == ["model-for-0.164.0"]
            assert provider.model == "keep-selected"
        assert credentials.await_count == 3
    assert metadata_requests == ["", '"0.163.0"', '"0.164.0"']
    assert model_versions == ["0.163.0", "0.164.0", "0.164.0"]


@pytest.mark.asyncio
async def test_overlapping_requests_share_one_refresh_but_later_calls_do_not_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    versions = catalog.CatalogVersion()
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def fetch() -> tuple[str, str]:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return "0.163.0", ""

    monkeypatch.setattr(versions, "_fetch", fetch)
    first = asyncio.create_task(versions.resolve())
    await asyncio.wait_for(entered.wait(), HANG_GUARD)
    others = [asyncio.create_task(versions.resolve()) for _ in range(10)]
    await asyncio.sleep(0)
    release.set()
    assert await asyncio.gather(first, *others) == ["0.163.0"] * 11
    assert calls == 1
    assert await versions.resolve() == "0.163.0"
    assert calls == 2


@pytest.mark.asyncio
async def test_outage_retains_last_known_version_backs_off_and_recovers(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    versions = catalog.CatalogVersion()
    now = 100.0
    monkeypatch.setattr(catalog, "monotonic", lambda: now)
    fetch = AsyncMock(side_effect=[("0.163.0", ""), ValueError("private-upstream-error"), ("0.164.0", "")])
    monkeypatch.setattr(versions, "_fetch", fetch)
    assert await versions.resolve() == "0.163.0"
    assert await versions.resolve() == "0.163.0"
    assert await versions.resolve() == "0.163.0"
    assert fetch.await_count == 2
    now += catalog.FAILURE_BACKOFF
    assert await versions.resolve() == "0.164.0"
    assert fetch.await_count == 3
    assert "private-upstream-error" not in caplog.text


@pytest.mark.parametrize(
    "document",
    [
        {},
        {"name": "another/package", "version": "0.163.0"},
        *(
            {"name": "@openai/codex", "version": value}
            for value in (
                None,
                163,
                "0.0.0",
                "0.153.4",
                "0.163.0-alpha.1",
                "0.163.0+local",
                "v0.163.0",
                "0.163",
                "0.163.0.1",
                "00.163.0",
                "0.163.0\r\nsecret",
                "0.163.0 ",
                "9999999.0.0",
            )
        ),
    ],
)
@pytest.mark.asyncio
async def test_invalid_or_old_release_metadata_uses_bundled_fallback(
    monkeypatch: pytest.MonkeyPatch, document: object
) -> None:
    async def handle(request: web.Request) -> web.Response:
        return web.json_response(document)

    async with endpoint(handle) as base:
        monkeypatch.setattr(catalog, "METADATA_URL", base)
        versions = catalog.CatalogVersion()
        assert await versions.resolve() == catalog.FALLBACK_VERSION
        assert versions.source == "bundled" and versions.retry_at > monotonic()


@pytest.mark.parametrize("status", [301, 302, 307, 308, 304, 401, 403, 429, 500])
@pytest.mark.asyncio
async def test_errors_never_redirect_or_log_metadata_body(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, status: int
) -> None:
    paths: list[str] = []

    async def handle(request: web.Request) -> web.Response:
        paths.append(request.path)
        return web.Response(status=status, text="private-response", headers={"Location": "/sink"})

    async with endpoint(handle) as base:
        monkeypatch.setattr(catalog, "METADATA_URL", base + "/metadata")
        assert await catalog.CatalogVersion().resolve() == catalog.FALLBACK_VERSION
    assert paths == ["/metadata"]
    assert "private-response" not in caplog.text


@pytest.mark.parametrize(
    "body", [b"\xff", b"not-json", b'{"name":"@openai/codex","version":"0.163.0","version":"0.164.0"}', b"x" * 129]
)
@pytest.mark.asyncio
async def test_metadata_decoding_and_body_are_bounded(monkeypatch: pytest.MonkeyPatch, body: bytes) -> None:
    async def handle(request: web.Request) -> web.Response:
        return web.Response(body=body)

    async with endpoint(handle) as base:
        monkeypatch.setattr(catalog, "METADATA_URL", base)
        monkeypatch.setattr(catalog, "MAX_METADATA_BYTES", 128)
        assert await catalog.CatalogVersion().resolve() == catalog.FALLBACK_VERSION


@pytest.mark.asyncio
async def test_deep_metadata_within_byte_limit_is_a_sanitized_fallback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Size validation alone cannot protect the JSON parser's recursion depth.
    body = b'{"name":"@openai/codex","version":"0.163.0","extra":' + b"[" * 10000 + b'"private"' + b"]" * 10000 + b"}"
    assert len(body) < catalog.MAX_METADATA_BYTES
    calls = 0

    async def handle(request: web.Request) -> web.Response:
        nonlocal calls
        calls += 1
        return web.Response(body=body)

    async with endpoint(handle) as base:
        monkeypatch.setattr(catalog, "METADATA_URL", base)
        versions = catalog.CatalogVersion()
        assert await versions.resolve() == catalog.FALLBACK_VERSION
        assert await versions.resolve() == catalog.FALLBACK_VERSION
        assert calls == 1
        assert versions.retry_at > monotonic()
    assert "private" not in caplog.text


@pytest.mark.asyncio
async def test_catalog_log_scope_is_task_local_and_restored_on_cancellation(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    entered, release = asyncio.Event(), asyncio.Event()

    async def fetch() -> tuple[str, str]:
        entered.set()
        await release.wait()
        return "0.163.0", ""

    versions = catalog.CatalogVersion()
    monkeypatch.setattr(versions, "_fetch", fetch)

    async def lookup(name: str) -> None:
        with catalog.catalog_logging(logging.getLogger(name)):
            await versions.resolve()

    first = asyncio.create_task(lookup("catalog.host.first"))
    await asyncio.wait_for(entered.wait(), HANG_GUARD)
    second = asyncio.create_task(lookup("catalog.host.second"))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    await second
    await versions.resolve()
    records = [record for record in caplog.records if record.getMessage().startswith("Codex catalog compatibility:")]
    assert [record.name for record in records] == ["catalog.host.second", catalog.__name__]


@pytest.mark.asyncio
async def test_metadata_timeout_is_bounded_and_cancellation_is_not_an_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    versions = catalog.CatalogVersion()
    entered = asyncio.Event()

    async def fetch() -> tuple[str, str]:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("Unreachable")

    monkeypatch.setattr(versions, "_fetch", fetch)
    async with asyncio.timeout(HANG_GUARD):
        assert await versions.resolve(0.01) == catalog.FALLBACK_VERSION
    assert versions.retry_at > monotonic()
    versions.retry_at = 0
    entered.clear()
    task = asyncio.create_task(versions.resolve())
    await asyncio.wait_for(entered.wait(), HANG_GUARD)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert versions.retry_at == 0
    monkeypatch.setattr(versions, "_fetch", AsyncMock(return_value=("0.163.0", "")))
    assert await versions.resolve() == "0.163.0"


def test_short_lived_event_loops_are_not_retained(monkeypatch: pytest.MonkeyPatch) -> None:
    versions = catalog.CatalogVersion()

    async def fetch() -> tuple[str, str]:
        await asyncio.sleep(0)
        return "0.163.0", ""

    monkeypatch.setattr(versions, "_fetch", fetch)
    loops = []

    async def scenario() -> None:
        loops.append(ref(asyncio.get_running_loop()))
        results = await asyncio.gather(versions.resolve(), versions.resolve())
        assert list(results) == ["0.163.0", "0.163.0"]

    for _ in range(3):
        asyncio.run(scenario())
    gc.collect()
    assert all(loop() is None for loop in loops)
    assert not versions._locks


@pytest.mark.asyncio
async def test_cancelling_metadata_body_read_closes_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    entered, disconnected, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def handle(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse()
        await response.prepare(request)
        await response.write(b'{"name":"@openai/codex",')
        entered.set()
        while not release.is_set():
            if request.transport is None or request.transport.is_closing():
                disconnected.set()
                break
            await asyncio.sleep(0.01)
        return response

    async with endpoint(handle) as base:
        monkeypatch.setattr(catalog, "METADATA_URL", base)
        monkeypatch.setattr(catalog, "METADATA_TIMEOUT", HANG_GUARD)
        versions = catalog.CatalogVersion()
        task = asyncio.create_task(versions.resolve(HANG_GUARD))
        try:
            await asyncio.wait_for(entered.wait(), HANG_GUARD)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(disconnected.wait(), HANG_GUARD)
            assert versions.retry_at == 0
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
