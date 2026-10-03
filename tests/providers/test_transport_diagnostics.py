"""Safe failure observations through native and shared provider generation paths."""

from __future__ import annotations

import asyncio
import errno
import json
import socket
import ssl
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web
from aiohttp.client_reqrep import ConnectionKey

from nagents import Agent
from nagents import SessionManager
from nagents.cli import _event_record
from nagents.events import DoneEvent
from nagents.events import ErrorEvent
from nagents.events import FinishReason
from nagents.events import RateLimitEvent
from nagents.events import TextChunkEvent
from nagents.events import ToolCallEvent
from nagents.http import HTTPError
from nagents.observation import scope
from nagents.provider import Provider
from nagents.provider import ProviderType
from nagents.provider import _diagnostics
from nagents.provider import base
from nagents.provider import openai
from nagents.provider.openai import CodexCredentials
from nagents.provider.openai import OpenAIProvider
from nagents.types import Message
from nagents.types import RetryConfig
from tests.support.hang_guard import HANG_GUARD

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from collections.abc import Awaitable
    from collections.abc import Callable
    from pathlib import Path

PRIVATE = "fixture-secret-url-header-body-value"
URL = f"https://private-upstream.invalid/{PRIVATE}"
KEY = ConnectionKey(f"{PRIVATE}.invalid", 443, True, True, None, None, None)
ROUTES = ["codex", "responses", "chat_completions", "messages", "anthropic", "gemini"]


async def credentials() -> CodexCredentials:
    return CodexCredentials(PRIVATE, "fixture-account")


def provider(route: str, *, url: str = URL, timeout: float = 120) -> Provider:
    if route == "codex":
        return OpenAIProvider(credentials, model="fixture", timeout=timeout)
    kind = {"anthropic": ProviderType.ANTHROPIC, "gemini": ProviderType.GEMINI_NATIVE}.get(
        route, ProviderType.OPENAI_COMPATIBLE
    )
    return Provider(
        kind,
        PRIVATE,
        "fixture",
        base_url=url,
        api=route if route not in {"anthropic", "gemini"} else "auto",
        timeout=timeout,
    )


def details(event: ErrorEvent) -> dict[str, object]:
    value = event.extra["transport"]
    assert isinstance(value, dict)
    assert set(value) <= {"category", "phase", "generation_elapsed_ms", "model_call_id"}
    assert isinstance(value["generation_elapsed_ms"], (float, int)) and value["generation_elapsed_ms"] >= 0
    assert PRIVATE not in json.dumps(asdict(event), default=str)
    return dict(value)


FAILURES: list[tuple[str, Callable[[], Exception], str]] = [
    ("dns", lambda: aiohttp.ClientConnectorError(KEY, socket.gaierror(socket.EAI_NONAME, PRIVATE)), "dns"),
    ("connect", lambda: aiohttp.ClientConnectorError(KEY, OSError(errno.ECONNREFUSED, PRIVATE)), "connect"),
    ("disconnect", lambda: aiohttp.ServerDisconnectedError(PRIVATE), "connection_lost"),
    ("total-timeout", lambda: TimeoutError(PRIVATE), "timeout"),
    ("decode", lambda: json.JSONDecodeError(PRIVATE, PRIVATE, 1), "decode"),
    ("validation", lambda: ValueError(PRIVATE), "invalid_data"),
    ("tls", lambda: aiohttp.ClientConnectorCertificateError(KEY, ssl.CertificateError(PRIVATE)), "tls"),
    ("payload", lambda: aiohttp.ClientPayloadError(PRIVATE), "response_payload"),
]


def failure_factory(error_type: type[Exception], *arguments: object) -> Callable[[], Exception]:
    def create() -> Exception:
        return error_type(*arguments)

    return create


for _name, _category, _arguments in [
    ("ConnectionTimeoutError", "connect_timeout", (PRIVATE,)),
    ("SocketTimeoutError", "read_timeout", (PRIVATE,)),
    # AsyncResolver can wrap DNS failures in plain OSError instead of gaierror.
    ("ClientConnectorDNSError", "dns", (KEY, OSError(None, PRIVATE))),
    ("ServerFingerprintMismatch", "tls", (PRIVATE.encode(), b"different", PRIVATE, 443)),
]:
    _error_type = getattr(aiohttp, _name, None)
    if isinstance(_error_type, type) and issubclass(_error_type, Exception):
        FAILURES.append((_name, failure_factory(_error_type, *_arguments), _category))


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize(("_name", "failure", "category"), FAILURES, ids=[case[0] for case in FAILURES])
def test_generation_failure_categories_are_private_and_do_not_change_recovery(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    route: str,
    _name: str,
    failure: Callable[[], Exception],
    category: str,
) -> None:
    request = AsyncMock(side_effect=failure())
    monkeypatch.setattr(aiohttp.ClientSession, "_request", request)
    monkeypatch.setattr(base, "monotonic", lambda: 10.0)
    monkeypatch.setattr(openai, "monotonic", lambda: 10.0)
    monkeypatch.setattr(_diagnostics, "monotonic", lambda: 12.5)

    async def scenario() -> None:
        token = scope.set({"model_call_id": "a" * 32, "private": PRIVATE})
        try:
            async with provider(route) as client:
                events = [event async for event in client.generate([Message(role="user", content=PRIVATE)])]
            assert len(events) == 1 and isinstance(events[0], ErrorEvent)
            error = events[0]
            assert error.code == ("CODEX_CONNECTION" if route == "codex" else "PROVIDER_REQUEST_FAILED")
            assert error.recoverable is False
            assert details(error) == {
                "category": category,
                "phase": "request" if route == "codex" else "unknown",
                "generation_elapsed_ms": 2500.0,
                "model_call_id": "a" * 32,
            }
            assert request.await_count == 1
        finally:
            scope.reset(token)

    asyncio.run(scenario())
    assert PRIVATE not in caplog.text


@pytest.mark.parametrize("route", ROUTES)
def test_cancellation_stays_cancellation_without_a_failure_event(monkeypatch: pytest.MonkeyPatch, route: str) -> None:
    request = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr(aiohttp.ClientSession, "_request", request)

    async def scenario() -> None:
        async with provider(route) as client:
            with pytest.raises(asyncio.CancelledError):
                _ = [event async for event in client.generate([Message(role="user", content="cancel")])]
        assert request.await_count == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [401, 429, 503])
def test_http_retry_counts_codes_and_recoverability_stay_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    clock = [10.0]

    async def fail(*args: object, **kwargs: object) -> None:
        clock[0] += 2.0
        raise HTTPError(status, PRIVATE, PRIVATE, URL, {"Authorization": PRIVATE})

    request = AsyncMock(side_effect=fail)
    monkeypatch.setattr(aiohttp.ClientSession, "_request", request)
    monkeypatch.setattr(base, "monotonic", lambda: clock[0])
    monkeypatch.setattr(_diagnostics, "monotonic", lambda: clock[0])

    async def scenario() -> None:
        async with provider("responses") as client:
            client.retry_config = RetryConfig(max_retries=1, base_delay=0, max_delay=0)
            events = [event async for event in client.generate([Message(role="user", content="test")])]
        error = events[-1]
        assert isinstance(error, ErrorEvent) and error.code == str(status)
        assert error.recoverable is (status != 401)
        assert details(error)["category"] == "http"
        assert request.await_count == (1 if status == 401 else 2)
        assert details(error)["generation_elapsed_ms"] == request.await_count * 2000
        assert sum(isinstance(event, RateLimitEvent) for event in events) == (0 if status == 401 else 1)
        assert not any(isinstance(event, ToolCallEvent) for event in events)

    asyncio.run(scenario())


@asynccontextmanager
async def endpoint(handler: Callable[[web.Request], Awaitable[web.StreamResponse]]) -> AsyncIterator[str]:
    app = web.Application()
    app.router.add_post("/{path:.*}", handler)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        yield f"http://127.0.0.1:{runner.addresses[0][1]}"
    finally:
        await runner.cleanup()


def sse(events: list[dict[str, object]]) -> bytes:
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()


@pytest.mark.parametrize("route", ["codex", "responses"])
@pytest.mark.parametrize("failure", ["timeout", "reset", "decode"])
def test_failure_after_stream_headers_keeps_tools_buffered_and_reports_only_observed_phase(
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    failure: str,
) -> None:
    async def scenario() -> None:
        release = asyncio.Event()
        text_received = asyncio.Event()
        requests = 0

        async def handle(request: web.Request) -> web.StreamResponse:
            nonlocal requests
            requests += 1
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream", "X-Private": PRIVATE})
            await response.prepare(request)
            await response.write(
                sse(
                    [
                        {
                            "type": "response.output_text.delta",
                            "output_index": 0,
                            "content_index": 0,
                            "delta": "partial",
                        },
                        {
                            "type": "response.output_item.added",
                            "output_index": 1,
                            "item": {
                                "id": "pending",
                                "type": "function_call",
                                "call_id": "call",
                                "name": "work",
                                "arguments": "{}",
                            },
                        },
                    ]
                )
            )
            if failure == "reset":
                # write() only queues bytes on Windows' Proactor transport.
                # Reset after the client consumed text, so this is a midstream
                # failure rather than a race that discards the queued payload.
                await text_received.wait()
                assert request.transport is not None
                request.transport.abort()
            elif failure == "decode":
                await response.write(f"data: {PRIVATE}\n\n".encode())
            else:
                await release.wait()
            return response

        async with endpoint(handle) as url, asyncio.timeout(HANG_GUARD):
            monkeypatch.setattr(openai, "CODEX_ENDPOINT", url + "/responses")
            async with provider(route, url=url, timeout=2) as client:
                try:
                    events = []
                    async for event in client.generate([Message(role="user", content="test")]):
                        events.append(event)
                        if isinstance(event, TextChunkEvent):
                            text_received.set()
                finally:
                    release.set()
                    # Also let fixture cleanup finish if no text was yielded;
                    # the unchanged text assertion below still fails that case.
                    text_received.set()
            error = events[-1]
            assert isinstance(error, ErrorEvent) and error.recoverable is False
            expected = {"timeout": "timeout", "reset": "response_payload", "decode": "protocol"}[failure]
            assert details(error)["category"] == expected
            assert details(error)["phase"] == ("response" if route == "codex" else "unknown")
            assert error.code == (
                ("CODEX_STREAM_INVALID" if route == "codex" else "PROVIDER_PROTOCOL_ERROR")
                if failure == "decode"
                else ("CODEX_CONNECTION" if route == "codex" else "PROVIDER_REQUEST_FAILED")
            )
            assert requests == 1
            assert any(isinstance(event, TextChunkEvent) for event in events)
            assert not any(isinstance(event, ToolCallEvent) for event in events)

    asyncio.run(scenario())


def test_real_agent_calls_keep_distinct_existing_correlation_and_fatal_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail(*args: object, **kwargs: object) -> None:
        await asyncio.sleep(0)
        raise TimeoutError(PRIVATE)

    monkeypatch.setattr(aiohttp.ClientSession, "_request", AsyncMock(side_effect=fail))
    # Catalog verification is separate from the generation failure under test.
    monkeypatch.setattr(Provider, "verify_model", AsyncMock(return_value=True))

    async def run_one(route: str) -> str:
        agent = Agent(provider(route), SessionManager(tmp_path / f"{route}.db"), compactor=None)
        try:
            events = [event async for event in agent.run("test", session_id=route)]
            errors = [event for event in events if isinstance(event, ErrorEvent)]
            assert len(errors) == 1
            record = details(errors[0])
            wire = _event_record(errors[0])
            assert wire["extra"] == {"transport": record}
            assert "transport" not in wire
            call_id = record["model_call_id"]
            assert isinstance(call_id, str) and len(call_id) == 32
            assert isinstance(events[-1], DoneEvent) and events[-1].finish_reason is FinishReason.UNKNOWN
            assert events[-1].session_id == route
            return call_id
        finally:
            await agent.close()

    async def scenario() -> None:
        ids = await asyncio.gather(run_one("codex"), run_one("responses"))
        assert len(set(ids)) == 2

    asyncio.run(scenario())


def test_invalid_external_correlation_is_omitted_and_unknown_failures_remain_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = AsyncMock(side_effect=RuntimeError(PRIVATE))
    monkeypatch.setattr(aiohttp.ClientSession, "_request", request)

    async def scenario() -> None:
        token = scope.set({"model_call_id": PRIVATE})
        try:
            async with provider("responses") as client:
                events = [event async for event in client.generate([Message(role="user", content="test")])]
            error = events[-1]
            assert isinstance(error, ErrorEvent) and error.code == "PROVIDER_REQUEST_FAILED"
            assert details(error)["category"] == "unknown" and "model_call_id" not in details(error)
            # Native Codex never caught this class; diagnostics do not expand that policy.
            async with provider("codex") as client:
                with pytest.raises(RuntimeError, match=PRIVATE):
                    _ = [event async for event in client.generate([Message(role="user", content="test")])]
        finally:
            scope.reset(token)

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["auth", "request"])
def test_native_preflight_errors_keep_existing_specific_codes_without_transport_details(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    request = AsyncMock(side_effect=AssertionError("Preflight must not send a request"))
    monkeypatch.setattr(aiohttp.ClientSession, "_request", request)

    async def unavailable() -> CodexCredentials:
        raise ValueError(PRIVATE)

    async def scenario() -> None:
        async with OpenAIProvider(unavailable if failure == "auth" else credentials, model="fixture") as client:
            message = Message(role="tool" if failure == "request" else "user", content="test")
            events = [event async for event in client.generate([message])]
        assert len(events) == 1 and isinstance(events[0], ErrorEvent)
        assert events[0].code == ("CODEX_AUTH" if failure == "auth" else "CODEX_REQUEST_INVALID")
        assert events[0].extra == {} and events[0].recoverable is False
        assert PRIVATE not in repr(events)
        request.assert_not_called()

    asyncio.run(scenario())


def test_native_http_rejection_keeps_its_specific_code_without_retry_or_transport_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        requests = 0

        async def handle(request: web.Request) -> web.Response:
            nonlocal requests
            requests += 1
            return web.Response(status=429, text=PRIVATE, headers={"Authorization": PRIVATE})

        async with endpoint(handle) as url:
            monkeypatch.setattr(openai, "CODEX_ENDPOINT", url)
            async with provider("codex") as client:
                events = [event async for event in client.generate([Message(role="user", content="test")])]
        assert len(events) == 1 and isinstance(events[0], ErrorEvent)
        assert events[0].code == "CODEX_HTTP_429" and events[0].recoverable is False
        assert events[0].extra == {} and PRIVATE not in repr(events)
        assert requests == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("route", ["anthropic", "gemini"])
@pytest.mark.parametrize("invalid", ["json", "utf8"])
def test_nonstream_decoding_errors_are_categorized_without_response_contents(route: str, invalid: str) -> None:
    async def scenario() -> None:
        requests = 0

        async def handle(request: web.Request) -> web.Response:
            nonlocal requests
            requests += 1
            body = PRIVATE.encode() if invalid == "json" else b"\xff" + PRIVATE.encode()
            return web.Response(
                body=body, headers={"Content-Type": "application/json; charset=utf-8", "X-Private": PRIVATE}
            )

        async with endpoint(handle) as url, provider(route, url=url) as client:
            events = [event async for event in client.generate([Message(role="user", content="test")], stream=False)]
        error = events[-1]
        assert len(events) == 1 and isinstance(error, ErrorEvent)
        assert error.code == "PROVIDER_REQUEST_FAILED" and error.recoverable is False
        assert details(error)["category"] == "decode" and details(error)["phase"] == "unknown"
        assert requests == 1

    asyncio.run(scenario())
