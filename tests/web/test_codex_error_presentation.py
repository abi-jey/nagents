"""Web Codex failures distinguish request preparation from interrupted output."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from nagents.events import ErrorEvent
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
