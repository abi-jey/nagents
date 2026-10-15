"""Web Codex failures distinguish request preparation from interrupted output."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from nagents.events import ErrorEvent
from nagents.provider._diagnostics import transport_diagnostic
from nagents.web.provider_setup import provider_error
from nagents.web.provider_setup import provider_error_diagnostic
from tests.support.web import ControlledHarness
from tests.support.web import client_app
from tests.support.web import submit_and_wait

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.types import ContentPart

PRIVATE = "PRIVATE-UPSTREAM-PAYLOAD"


@pytest.mark.parametrize(
    ("code", "message", "diagnostic", "category", "visible"),
    [
        (
            "CODEX_REQUEST_INVALID",
            "A tool result is missing its call ID; repair the session history.",
            "codex_request_invalid:request_history",
            "chatgpt_request",
            "tool history",
        ),
        (
            "CODEX_REQUEST_INVALID",
            "This reasoning effort is not supported by the selected provider and model; choose provider default.",
            "codex_request_invalid:request_effort",
            "chatgpt_request",
            "Provider default",
        ),
        (
            "CODEX_STREAM_INVALID",
            "Codex returned an incomplete response; no tool calls were released. Retry the request.",
            "codex_stream_invalid:response_incomplete",
            "chatgpt_response",
            "incomplete response",
        ),
        (
            "CODEX_STREAM_INVALID",
            "Codex terminal output did not match streamed text.",
            "codex_stream_invalid:output_text",
            "chatgpt_response",
            "could not be verified",
        ),
    ],
)
def test_known_local_reason_gives_actionable_safe_message(
    code: str, message: str, diagnostic: str, category: str, visible: str
) -> None:
    event = ErrorEvent(code=code, message=message, extra={"private": PRIVATE})
    actual_category, actual_message = provider_error(event)
    assert actual_category == category and visible in actual_message
    assert provider_error_diagnostic(event) == diagnostic
    assert PRIVATE not in actual_message
    if code == "CODEX_STREAM_INVALID":
        assert "selected model" not in actual_message


@pytest.mark.parametrize("code", ["CODEX_STREAM_INVALID", "CODEX_REQUEST_INVALID", PRIVATE, "401"])
def test_unrecognized_messages_and_metadata_never_enter_ui_or_diagnostic(code: str) -> None:
    event = ErrorEvent(code=code, message=PRIVATE, extra={"transport": {"reason": PRIVATE}, "codex": PRIVATE})
    assert PRIVATE not in repr(provider_error(event))
    diagnostic = provider_error_diagnostic(event)
    assert diagnostic in {"codex_stream_invalid:unknown", "codex_request_invalid:unknown", "none"}


@pytest.mark.asyncio
async def test_web_run_logs_allowlisted_protocol_detail_without_payload(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async def failed(self: ControlledHarness, prompt: str | list[ContentPart]) -> AsyncIterator[ErrorEvent]:
        yield ErrorEvent(
            code="CODEX_STREAM_INVALID",
            message="Codex terminal output did not match streamed text.",
            extra={"private": PRIVATE},
        )

    with patch.object(ControlledHarness, "run", failed):
        async with client_app(tmp_path) as (app, client, headers, _):
            session = (await client.get("/api/sessions", headers=headers)).json()["session_id"]
            events = await submit_and_wait(app, headers, session, "test")
    assert events[-1]["status"] == "failed"
    assert "diagnostic=codex_stream_invalid:output_text" in caplog.text
    assert "reason=chatgpt_response" in caplog.text
    assert PRIVATE not in caplog.text and PRIVATE not in repr(events)


@pytest.mark.asyncio
async def test_web_run_logs_transport_cause_timing_and_correlation_without_payload(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    async def failed(self: ControlledHarness, prompt: str | list[ContentPart]) -> AsyncIterator[ErrorEvent]:
        yield ErrorEvent(
            code="CODEX_CONNECTION",
            message=PRIVATE,
            extra={
                "transport": {
                    "category": "connect",
                    "phase": "request",
                    "exception": "ClientConnectorError",
                    "errno": 111,
                    "causes": [{"exception": "ConnectionRefusedError", "errno": 111, "message": PRIVATE}],
                    "generation_elapsed_ms": 1500,
                    "http_attempt_elapsed_ms": 1400,
                    "model_call_id": "a" * 32,
                    "http_attempt_id": "b" * 32,
                    "url": PRIVATE,
                }
            },
        )

    with patch.object(ControlledHarness, "run", failed):
        async with client_app(tmp_path) as (app, client, headers, _):
            session = (await client.get("/api/sessions", headers=headers)).json()["session_id"]
            events = await submit_and_wait(app, headers, session, "test")
    assert events[-1]["status"] == "failed"
    assert "reason=chatgpt_network recoverable=False diagnostic=transport=connect phase=request" in caplog.text
    assert "exception=ClientConnectorError(errno=111) causes=ConnectionRefusedError(errno=111)" in caplog.text
    assert "generation_elapsed_ms=1500.000 http_attempt_elapsed_ms=1400.000" in caplog.text
    assert f"model_call_id={'a' * 32} http_attempt_id={'b' * 32}" in caplog.text
    assert any(record.levelname == "ERROR" and "reason=chatgpt_network" in record.message for record in caplog.records)
    assert PRIVATE not in caplog.text and PRIVATE not in repr(events)


class HostileDict(dict[str, object]):
    def get(self, *args: object, **kwargs: object) -> object:
        raise AssertionError(PRIVATE)


class HostileString(str):
    def __hash__(self) -> int:
        raise AssertionError(PRIVATE)

    def __format__(self, format_spec: str) -> str:
        raise AssertionError(PRIVATE)


def test_transport_timing_rejects_numeric_subclasses_without_running_conversion() -> None:
    class HostileFloat(float):
        def __float__(self) -> float:
            raise AssertionError(PRIVATE)

        def __format__(self, format_spec: str) -> str:
            raise AssertionError(PRIVATE)

    event = ErrorEvent(
        code="CODEX_CONNECTION",
        extra={
            "transport": {
                "category": "connect",
                "phase": "request",
                "generation_elapsed_ms": HostileFloat(1),
            }
        },
    )
    assert provider_error_diagnostic(event) == "transport=connect phase=request"


@pytest.mark.parametrize(
    "field",
    [
        "category",
        "phase",
        "exception",
        "errno",
        "causes",
        "model_call_id",
        "http_attempt_id",
        "generation_elapsed_ms",
        "http_attempt_elapsed_ms",
    ],
)
@pytest.mark.parametrize(
    "malformed",
    [PRIVATE, HostileString(PRIVATE), HostileDict(), object(), True, float("inf"), float("nan"), -1, 10**1000],
)
def test_malformed_transport_fields_cannot_escape_fixed_bounded_log_schema(field: str, malformed: object) -> None:
    details: dict[str, object] = {"category": "connect", "phase": "request", field: malformed}
    result = provider_error_diagnostic(
        ErrorEvent(code="CODEX_CONNECTION", message=PRIVATE, extra={"transport": details})
    )
    assert result in {"none", "transport=connect phase=request"}
    assert PRIVATE not in result


@pytest.mark.parametrize(
    "extra",
    [
        HostileDict(),
        {"transport": HostileDict()},
        {"transport": object()},
        {"transport": {"category": [], "phase": "request"}},
    ],
)
def test_transport_metadata_getters_are_never_called(extra: dict[str, object]) -> None:
    event = ErrorEvent(code="CODEX_CONNECTION", message=PRIVATE, extra=extra)
    assert provider_error_diagnostic(event) == "none"
    assert provider_error(event)[0] == "chatgpt_network"


def test_transport_cause_chain_is_bounded_and_protocol_reason_is_preserved() -> None:
    extra: dict[str, object] = {
        "transport": {
            "category": "protocol",
            "phase": "response",
            "exception": "ValueError",
            "causes": [{"exception": "ValueError", "errno": True}] * 100,
            "model_call_id": "a" * 32 + PRIVATE,
        }
    }
    result = provider_error_diagnostic(
        ErrorEvent(
            code="CODEX_STREAM_INVALID",
            message="Codex terminal output did not match streamed text.",
            extra=extra,
        )
    )
    assert result == (
        "codex_stream_invalid:output_text transport=protocol phase=response exception=ValueError "
        "causes=ValueError>ValueError>ValueError"
    )


def test_hostile_dict_keys_cannot_raise_while_formatting_diagnostics() -> None:
    class HostileKey:
        def __hash__(self) -> int:
            return hash("transport")

        def __eq__(self, other: object) -> bool:
            raise AssertionError(PRIVATE)

    assert transport_diagnostic({HostileKey(): PRIVATE}) == ""
