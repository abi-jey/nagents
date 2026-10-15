"""Fatal Live failures remain visible without exception bodies or credentials."""

from __future__ import annotations

import asyncio
import errno
import logging
from dataclasses import replace
from typing import TYPE_CHECKING

import aiohttp
import pytest

from nagents.provider._diagnostics import exception_diagnostic
from nagents.web.live_diagnostics import log_live_failure
from nagents.web.live_login import ChatGPTLiveConnection
from nagents.web.live_login import LoginVoiceError
from nagents.web.live_relay import ChatGPTMediaRelay
from nagents.web.live_runtime import _Call
from tests.support.hang_guard import HANG_GUARD
from tests.test_web_live_login import OFFER
from tests.test_web_live_login import config
from tests.test_web_live_relay import Output
from tests.test_web_live_relay import Source

if TYPE_CHECKING:
    from nagents.provider.openai import CodexCredentials

SECRET = "private-token-body-sdp-url-marker"
VOICE_ID = "f" * 32


def test_exception_formatter_has_no_generation_side_effects_or_payload(caplog: pytest.LogCaptureFixture) -> None:
    error = aiohttp.ClientConnectionError(SECRET)
    error.__cause__ = ConnectionResetError(errno.ECONNRESET, SECRET)
    result = exception_diagnostic(error)
    assert "exception=ClientConnectionError" in result
    assert f"causes=ConnectionResetError(errno={errno.ECONNRESET})" in result
    assert SECRET not in result and "generation" not in result and "model_call" not in result
    assert not caplog.records


def test_hostile_exception_inspection_cannot_replace_the_original_failure(caplog: pytest.LogCaptureFixture) -> None:
    class HostileError(RuntimeError):
        def __getattribute__(self, name: str) -> object:
            raise ValueError(SECRET)

    error = HostileError(SECRET)
    assert exception_diagnostic(error) == "transport=unknown phase=unknown"
    call = _Call()
    call.fail("safe UI failure", stage="configuration", error=error)
    assert call.failure == "safe UI failure"
    assert "diagnostic=transport=unknown phase=unknown" in caplog.text
    assert SECRET not in caplog.text


def test_terminal_lifecycle_failure_is_logged_once_without_its_message(caplog: pytest.LogCaptureFixture) -> None:
    call = _Call()
    call.fail(SECRET, 503, stage="configuration", error=ValueError(SECRET))
    call.fail("second failure", stage="cleanup", error=RuntimeError(SECRET))
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.ERROR
    assert f"voice_session_id={call.record.identifier}" in record.message
    assert "stage=configuration status=503" in record.message
    assert "exception=ValueError" in record.message
    assert SECRET not in caplog.text and "second failure" not in caplog.text
    assert call.failure == SECRET  # Diagnostic projection does not alter the UI contract.


@pytest.mark.parametrize("control", [False, True])
def test_login_logs_original_transport_cause_before_safe_wrapping(
    caplog: pytest.LogCaptureFixture, control: bool
) -> None:
    async def credentials() -> CodexCredentials:
        raise aiohttp.ClientConnectionError(SECRET) from ConnectionResetError(errno.ECONNRESET, SECRET)

    async def scenario() -> None:
        connection = ChatGPTLiveConnection(replace(config(), credentials=credentials), voice_session_id=VOICE_ID)
        try:
            with pytest.raises(LoginVoiceError) as failure:
                if control:
                    connection.identifier = "rtc_fixture"
                    await anext(connection.events())
                else:
                    await connection.provision(OFFER)
            assert SECRET not in str(failure.value)
            assert failure.value.__suppress_context__
        finally:
            # This failure happened before a provider request was submitted.
            connection.identifier = ""
            await connection.aclose()

    asyncio.run(scenario())
    assert len(caplog.records) == 1
    assert f"voice_session_id={VOICE_ID}" in caplog.text
    assert f"stage={'login_control' if control else 'login_provision'}" in caplog.text
    assert "exception=ClientConnectionError" in caplog.text
    assert f"causes=ConnectionResetError(errno={errno.ECONNRESET})" in caplog.text
    assert SECRET not in caplog.text


def test_cancelled_login_and_explicit_cancel_diagnostic_do_not_log_errors(caplog: pytest.LogCaptureFixture) -> None:
    async def scenario() -> None:
        entered = asyncio.Event()

        async def credentials() -> CodexCredentials:
            entered.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        connection = ChatGPTLiveConnection(replace(config(), credentials=credentials), voice_session_id=VOICE_ID)
        task = asyncio.create_task(connection.provision(OFFER))
        await asyncio.wait_for(entered.wait(), HANG_GUARD)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await connection.aclose()
        log_live_failure(VOICE_ID, "cleanup", asyncio.CancelledError(SECRET))

    asyncio.run(scenario())
    assert not caplog.records


def test_media_worker_failure_is_visible_and_cancellation_is_quiet(caplog: pytest.LogCaptureFixture) -> None:
    async def scenario() -> None:
        relay = ChatGPTMediaRelay(Source(), Output())

        async def fail() -> None:
            raise ConnectionResetError(errno.ECONNRESET, SECRET)

        failure = asyncio.create_task(fail())
        relay._worker(failure, "media_output")
        with pytest.raises(ConnectionResetError):
            await failure
        await asyncio.sleep(0)
        with pytest.raises(LoginVoiceError, match="audio relay was interrupted"):
            await relay.wait()
        pending = asyncio.create_task(asyncio.sleep(HANG_GUARD))
        relay._worker(pending)
        await relay.aclose()
        assert pending.cancelled()

    asyncio.run(scenario())
    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert len(errors) == 1 and "stage=media_output" in errors[0].message
    assert f"exception=ConnectionResetError(errno={errno.ECONNRESET})" in errors[0].message
    assert SECRET not in caplog.text
