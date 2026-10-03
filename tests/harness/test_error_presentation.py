"""Human transport explanations leave provider events and machine records intact."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import TYPE_CHECKING
from typing import ClassVar
from unittest.mock import AsyncMock
from unittest.mock import Mock

import aiohttp
import pytest
from textual.widgets import Static

from nagents import cli
from nagents.error_presentation import transport_error_message
from nagents.events import ErrorEvent
from nagents.harness import Harness
from nagents.harness import HarnessConfig
from nagents.harness.auth import OpenAIAuth
from nagents.harness.tools import CodingTools
from nagents.provider.openai import CodexCredentials
from nagents.tui import NagentsApp
from nagents.web.provider_setup import provider_error
from tests.support.tui import idle
from tests.support.web import client_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

PRIVATE = "PRIVATE-UPSTREAM-URL-HEADERS-PAYLOAD"
RESPONSE_TIMEOUT = "The provider request timed out while receiving the response."
ORIGINAL_TIMEOUT = "Codex connection failed or timed out; retry the request."


@pytest.mark.parametrize("code", ["CODEX_CONNECTION", "PROVIDER_REQUEST_FAILED"])
@pytest.mark.parametrize(
    "category,phase,expected",
    [
        ("timeout", "request", "The provider request timed out before a response arrived."),
        ("timeout", "response", RESPONSE_TIMEOUT),
        ("timeout", "unknown", "The provider request timed out."),
        ("connect_timeout", "request", "The connection to the provider timed out."),
        ("read_timeout", "response", "Timed out waiting for data from the provider."),
        ("dns", "unknown", "The provider address could not be resolved."),
        ("tls", "request", "A secure connection to the provider could not be established."),
        ("connect", "request", "Could not connect to the provider."),
        ("connection_lost", "response", "The connection to the provider was interrupted."),
        ("connection", "unknown", "The provider connection failed."),
        ("response_payload", "response", "The provider response could not be read completely."),
    ],
)
def test_known_transport_details_use_only_fixed_explanations(
    code: str, category: str, phase: str, expected: str
) -> None:
    event = ErrorEvent(
        message=PRIVATE,
        code=code,
        recoverable=True,
        extra={
            "transport": {
                "category": category,
                "phase": phase,
                "generation_elapsed_ms": PRIVATE,
                "model_call_id": PRIVATE,
                "headers": PRIVATE,
            },
            "payload": PRIVATE,
        },
    )
    before = asdict(event)
    assert transport_error_message(event) == expected
    reason, message = provider_error(event)
    assert reason == ("chatgpt_network" if code == "CODEX_CONNECTION" else "provider_failure")
    assert message == expected and PRIVATE not in message
    assert asdict(event) == before


@pytest.mark.parametrize(
    "details",
    [
        None,
        PRIVATE,
        [],
        {},
        {"category": "timeout"},
        {"phase": "response"},
        {"category": PRIVATE, "phase": "response"},
        {"category": "timeout", "phase": PRIVATE},
        {"category": ["timeout"], "phase": "response"},
        {"category": "timeout", "phase": {"value": "response"}},
        {"category": True, "phase": "response"},
        {"category": "unknown", "phase": "unknown"},
        {"category": "protocol", "phase": "response"},
        {"category": "http", "phase": "unknown"},
    ],
)
def test_malformed_or_unknown_metadata_preserves_existing_fallbacks(details: object) -> None:
    event = ErrorEvent(message="Existing safe fallback", code="CODEX_CONNECTION", extra={"transport": details})
    assert transport_error_message(event) == ""
    assert provider_error(event) == provider_error(ErrorEvent(code="CODEX_CONNECTION"))
    assert event.message == "Existing safe fallback"


@pytest.mark.parametrize(
    "code",
    [
        None,
        "",
        PRIVATE,
        "CODEX_AUTH",
        "CODEX_HTTP_401",
        "401",
        "429",
        "PROVIDER_PROTOCOL_ERROR",
        "CODEX_STREAM_INVALID",
    ],
)
def test_other_errors_keep_their_existing_presentation(code: str | None) -> None:
    event = ErrorEvent(
        message="Existing safe message", code=code, extra={"transport": {"category": "timeout", "phase": "response"}}
    )
    assert transport_error_message(event) == ""
    assert provider_error(event) == provider_error(ErrorEvent(message=event.message, code=code))


class TimeoutResponse:
    status = 200
    headers: ClassVar[dict[str, str]] = {"Content-Type": "text/event-stream"}
    content_type = "text/event-stream"

    @property
    def content(self) -> TimeoutResponse:
        return self

    async def __aenter__(self) -> TimeoutResponse:
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    async def iter_chunked(self, size: int) -> AsyncIterator[bytes]:
        # aiohttp's total deadline can raise a TimeoutError with no message.
        raise TimeoutError()
        yield b""  # pragma: no cover


@pytest.fixture
def native_timeout(monkeypatch: pytest.MonkeyPatch) -> Mock:
    # Keep the real provider, Agent, Harness lifecycle, and UI; replace only
    # credentials, HTTP I/O, and unrelated platform-specific file discovery.
    monkeypatch.setattr(Harness, "load_project_instructions", lambda self: None)
    monkeypatch.setattr(CodingTools, "discover_skills", lambda self: None)
    monkeypatch.setattr(OpenAIAuth, "logged_in", lambda self: True)
    monkeypatch.setattr(OpenAIAuth, "credentials", AsyncMock(return_value=CodexCredentials("synthetic", "fixture")))
    request = Mock(return_value=TimeoutResponse())
    monkeypatch.setattr(aiohttp.ClientSession, "post", request)
    monkeypatch.setattr(aiohttp.ClientSession, "_request", AsyncMock(side_effect=AssertionError("No network calls")))
    return request


def native_config(workspace: Path) -> HarnessConfig:
    return HarnessConfig(workspace=workspace, data_dir=workspace / "sessions", auth="chatgpt", model="fixture")


@pytest.mark.asyncio
@pytest.mark.parametrize("json_mode", [False, True])
async def test_actual_harness_cli_explains_timeout_without_changing_json_or_outcome(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], native_timeout: Mock, json_mode: bool
) -> None:
    harness = Harness(native_config(tmp_path))
    args = cli._parser().parse_args(["run", *(["--json"] if json_mode else []), "offline fixture"])
    assert await cli._headless(harness, args) == 1
    assert harness._closed and native_timeout.call_count == 1
    captured = capsys.readouterr()
    if json_mode:
        records = [json.loads(line) for line in captured.out.splitlines()]
        error = next(record for record in records if record["event"] == "error")
        assert error["message"] == ORIGINAL_TIMEOUT
        assert error["code"] == "CODEX_CONNECTION" and error["recoverable"] is False
        assert error["extra"]["transport"]["category"] == "timeout"
        assert error["extra"]["transport"]["phase"] == "response"
        assert records[-1]["event"] == "done" and records[-1]["finish_reason"] == "unknown"
        assert not any(record["event"] == "tool_call" for record in records)
        assert RESPONSE_TIMEOUT not in captured.out
    else:
        assert f"Error: {RESPONSE_TIMEOUT}" in captured.err
        assert ORIGINAL_TIMEOUT not in captured.err


@pytest.mark.asyncio
async def test_actual_harness_web_uses_safe_explanation_without_extra_or_egress_advice(
    tmp_path: Path, native_timeout: Mock
) -> None:
    async with client_app(tmp_path, config=native_config(tmp_path), controlled=False) as (_, client, headers, _):
        session = (await client.get("/api/sessions", headers=headers)).json()["session_id"]
        response = await client.post(
            "/api/run", json={"session_id": session, "prompt": "offline fixture"}, headers=headers
        )
        records = [json.loads(line) for line in response.text.splitlines()]
        error = next(record for record in records if record["event"] == "error")
        assert error["message"] == RESPONSE_TIMEOUT and error["recoverable"] is False
        assert "extra" not in error and "transport" not in error
        assert records[-1]["status"] == "failed"
        assert not any(record.get("event") == "tool_call" for record in records)
        assert "egress" not in response.text and ORIGINAL_TIMEOUT not in response.text
    assert native_timeout.call_count == 1


@pytest.mark.asyncio
async def test_actual_harness_tui_displays_timeout_and_retains_original_event(
    tmp_path: Path, native_timeout: Mock
) -> None:
    harness = Harness(native_config(tmp_path))
    app = NagentsApp(harness)
    async with app.run_test(size=(90, 25)) as pilot:
        await idle(app, pilot)
        errors = [event async for event in harness.run("offline fixture") if isinstance(event, ErrorEvent)]
        assert len(errors) == 1
        before = cli._event_record(errors[0])
        await app._event(errors[0])
        notices = [str(widget.content) for widget in app.query(".notice.error").results(Static)]
        assert notices == [f"Error: {RESPONSE_TIMEOUT}"]
        assert cli._event_record(errors[0]) == before and errors[0].message == ORIGINAL_TIMEOUT
    assert harness._closed and native_timeout.call_count == 1
