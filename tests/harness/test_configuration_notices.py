"""Configuration provenance remains inspectable without replaying it in chat."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

from nagents.events import TextDoneEvent
from nagents.harness.types import Notice
from tests.support.providers import collect
from tests.support.providers import setup_harness

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.types import Message
    from tests.support.providers import FakeProvider


@pytest.mark.requires_posix
@pytest.mark.asyncio
async def test_configuration_is_logged_once_not_replayed_and_runtime_notices_still_arrive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    startup = (
        "Applied provider connection: openai",
        f"Ignoring untrusted project config {tmp_path / '.ngn/config.json'}; "
        "use trust_project=True to allow endpoints and Python code.",
        f"Loaded trusted configuration: {tmp_path / 'trusted.json'}",
    )
    runtime_warning = "A requested tool is temporarily unavailable; retry the operation."

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        await harness.emit(Notice(runtime_warning, "warning"))
        yield TextDoneEvent(text="The assistant answer")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    harness.diagnostics.extend(startup)
    try:
        with caplog.at_level(logging.INFO, logger="nagents.harness.tools.resources"):
            await harness.initialize(create_session=False)
            first = await collect(harness, "First request")
            second = await collect(harness, "Follow-up request")
            await harness.initialize(create_session=False)
        for events in (first, second):
            assert [event.text for event in events if isinstance(event, Notice)] == [runtime_warning]
            assert any(isinstance(event, TextDoneEvent) and event.text == "The assistant answer" for event in events)
        logged = [record.getMessage() for record in caplog.records]
        assert all(logged.count(text) == 1 for text in startup)
        assert all(text in harness.describe() for text in startup)
        assert not harness.config.trust_project
    finally:
        await harness.close()


@pytest.mark.requires_posix
@pytest.mark.asyncio
async def test_new_foreign_configuration_is_diagnostic_not_a_chat_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    foreign = tmp_path / "opencode.json"

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        foreign.write_text('{"secret": "MUST_NOT_READ", "command": "MUST_NOT_EXECUTE"}')
        yield TextDoneEvent(text="Done")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    try:
        with caplog.at_level(logging.INFO, logger="nagents.harness.tools.resources"):
            first = await collect(harness)
            second = await collect(harness)
        assert not any(isinstance(event, Notice) for event in (*first, *second))
        ignored = [
            record.getMessage()
            for record in caplog.records
            if "Ignoring foreign MCP configuration" in record.getMessage()
        ]
        assert len(ignored) == 1 and str(foreign) in ignored[0]
        assert harness.resources.mcp_diagnostics().ignored_configs == (str(foreign),)
        assert str(foreign) in harness.describe()
        assert "MUST_NOT_READ" not in caplog.text and "MUST_NOT_EXECUTE" not in caplog.text
    finally:
        await harness.close()
