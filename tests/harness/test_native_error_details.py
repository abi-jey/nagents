"""Native provider failures keep useful fixed explanations across task boundaries."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest

from nagents.events import ErrorEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.harness.task_activity import public_payload
from nagents.harness.types import TaskCompleted
from tests.support.hang_guard import HANG_GUARD
from tests.support.providers import collect
from tests.support.providers import notifications
from tests.support.providers import setup_harness

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.types import Message
    from tests.support.providers import FakeProvider

pytestmark = pytest.mark.requires_posix

PRIVATE = "private-provider-body-and-api-key-DO-NOT-DISPLAY"


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("CODEX_HTTP_400", "Codex rejected the request. Check the exact model ID"),
        ("CODEX_HTTP_401", "ChatGPT sign-in expired or was rejected; sign in again with /login."),
    ],
)
def test_native_provider_error_keeps_actionable_safe_task_and_parent_detail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    code: str,
    expected: str,
) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index == 0:
                if len(provider.requests) == 1:
                    yield ToolCallEvent(
                        id="spawn-native-error", name="delegate", arguments={"prompt": "Check model access"}
                    )
                else:
                    yield TextDoneEvent(text="I received the child outcome")
            else:
                yield ErrorEvent(
                    message=PRIVATE,
                    code=code,
                    extra={"body": PRIVATE, "authorization": PRIVATE},
                )

        harness, _ = setup_harness(tmp_path, monkeypatch, script)
        try:
            events = await asyncio.wait_for(collect(harness), HANG_GUARD)
            completed = [event for event in events if isinstance(event, TaskCompleted)]
            parent_notes = notifications(await harness.history())

            assert len(completed) == 1
            assert completed[0].status == "failed"
            assert code in completed[0].error
            assert expected in completed[0].error
            assert len(parent_notes) == 1
            serialized = str(parent_notes[0].content)
            payload = json.loads(serialized.split("\n", 1)[1])
            task = payload["tasks"][0]
            assert task["status"] == "failed"
            assert code in task["error"] and expected in task["error"]
            assert PRIVATE not in repr(events) + serialized
            assert code in caplog.text
            assert PRIVATE not in caplog.text
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_untrusted_native_error_code_and_details_are_not_echoed_in_public_activity() -> None:
    hostile = "<img src=x onerror=alert(1)>-SECRET"
    payload = public_payload(
        ErrorEvent(
            message=PRIVATE,
            code=hostile,
            recoverable=False,
            extra={"headers": PRIVATE, "response_body": PRIVATE},
        )
    )

    assert payload == {
        "type": "status",
        "phase": "failed",
        "detail": "Provider or tool processing encountered an error.",
    }
    assert hostile not in repr(payload)
    assert PRIVATE not in repr(payload)


def test_known_error_activity_exposes_only_safe_code_and_fixed_detail_and_recoverable_stays_running() -> None:
    failed = public_payload(ErrorEvent(message=PRIVATE, code="CODEX_HTTP_400", extra={"body": PRIVATE}))
    recovering = public_payload(ErrorEvent(message=PRIVATE, code="CODEX_HTTP_401", recoverable=True))

    assert failed["type"] == "status" and failed["phase"] == "failed"
    assert failed["code"] == "CODEX_HTTP_400"
    assert "exact model ID" in str(failed["detail"])
    assert recovering["type"] == "status" and recovering["phase"] == "running"
    assert recovering["code"] == "CODEX_HTTP_401"
    assert "sign in again" in str(recovering["detail"])
    assert PRIVATE not in repr(failed) + repr(recovering)
