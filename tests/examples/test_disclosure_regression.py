"""Regression: disclosure wording is reported once, never as a delivery claim."""

from __future__ import annotations

import asyncio
import importlib
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from typing import cast

import pytest

from nagents import AudioTranscriptDeltaEvent
from nagents.live import LiveEvent

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable
    from types import ModuleType

    from nagents import Agent
    from nagents import Event

_LIVE_DIR = Path(__file__).parents[2] / "examples" / "live"


class _FakeLive:
    async def wait(self, event_id: str) -> dict[str, object]:
        return {"type": "session.instructions.appended", "client_event_id": event_id}


class _FakeAgent:
    def __init__(self) -> None:
        self.live = _FakeLive()
        self.instructions: list[str] = []

    async def add_instructions(self, content: str) -> str:
        self.instructions.append(content)
        return "cmd-1"


@pytest.fixture(scope="module")
def disclosure() -> ModuleType:
    if str(_LIVE_DIR) not in sys.path:
        sys.path.insert(0, str(_LIVE_DIR))
    return importlib.import_module("disclosure")


def test_disclosure_reports_once_without_claiming_delivery(
    disclosure: ModuleType, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    agent = _FakeAgent()
    handler: Callable[[Agent, Event], Awaitable[None]] | None = None

    async def fake_drive(
        voice_agent: Agent, callback: Callable[[Agent, Event], Awaitable[None]], *, duration: float = 0
    ) -> None:
        nonlocal handler
        handler = callback
        typed = cast("Agent", agent)
        await callback(typed, LiveEvent(event_type="session.started", payload={}))
        # The phrase arrives split across many transcript chunks, repeatedly.
        await callback(typed, AudioTranscriptDeltaEvent(delta="This call may be "))
        await callback(typed, AudioTranscriptDeltaEvent(delta="recorded for quality and training purposes."))
        for _ in range(500):
            await callback(
                typed, AudioTranscriptDeltaEvent(delta=" This call may be recorded for quality and training purposes.")
            )
        for _ in range(500):
            await callback(typed, AudioTranscriptDeltaEvent(delta="unrelated caller speech "))

    monkeypatch.setattr(disclosure, "voice", lambda: agent)
    monkeypatch.setattr(disclosure, "drive", fake_drive)
    monkeypatch.setattr(sys, "argv", ["disclosure.py"])

    with caplog.at_level(logging.INFO, logger="disclosure"):
        asyncio.run(disclosure.main())

    assert handler is not None
    assert agent.instructions and "Immediately say this disclosure" in agent.instructions[0]
    wording = [record.message for record in caplog.records if "Wording observed" in record.message]
    assert len(wording) == 1
    assert "does not prove playback" in wording[0]
    assert "delivered" not in wording[0].lower()
    assert "played" not in wording[0].lower()
