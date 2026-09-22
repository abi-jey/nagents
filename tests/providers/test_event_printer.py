"""Offline regressions for the streamed example event printer (``event_printer.py``)."""

from __future__ import annotations

import base64
import importlib.util
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from nagents import AudioChunkEvent
from nagents import AudioTranscriptDeltaEvent
from nagents import DoneEvent
from nagents import ErrorEvent
from nagents import InputTranscriptCompletedEvent
from nagents import InputTranscriptDeltaEvent
from nagents import RealtimeRateLimitsEvent
from nagents import RealtimeRawEvent
from nagents import RealtimeSessionCreatedEvent
from nagents import RealtimeSessionUpdatedEvent
from nagents import ReasoningChunkEvent
from nagents import ResponseCancelledEvent
from nagents import ResponseCreatedEvent
from nagents import SpeechStartedEvent
from nagents import SpeechStoppedEvent
from nagents import TextChunkEvent
from nagents import ToolCallEvent
from nagents import ToolResultEvent

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

    from pytest import CaptureFixture

    from nagents.events import Event

    MakeEvent = Callable[[str], Event]


@pytest.fixture(scope="module")
def printer() -> ModuleType:
    path = Path(__file__).parents[2] / "examples" / "_voice" / "event_printer.py"
    spec = importlib.util.spec_from_file_location("voice_event_printer_stream", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def begin(printer: ModuleType, capsys: CaptureFixture[str]) -> float:
    """Clear any previous partial stream so each test starts on a clean line."""
    printer._finish_stream()
    capsys.readouterr()
    printer.logger.setLevel(logging.INFO)
    return float(printer.start_timer())


@pytest.mark.parametrize(
    ("kind", "make"),
    [
        ("assistant", lambda text: AudioTranscriptDeltaEvent(delta=text)),
        ("text", lambda text: TextChunkEvent(chunk=text)),
        ("input", lambda text: InputTranscriptDeltaEvent(delta=text)),
        ("reasoning", lambda text: ReasoningChunkEvent(chunk=text)),
    ],
)
def test_streaming_parts_share_one_prefix(
    printer: ModuleType, capsys: CaptureFixture[str], kind: str, make: MakeEvent
) -> None:
    start = begin(printer, capsys)
    for part in ("Hello", " ", "world"):
        printer.print_event(make(part), start)

    out = capsys.readouterr().out
    assert out.count(kind) == 1  # One prefix for the whole streamed part.
    assert out.endswith("Hello world")
    assert "\n" not in out
    assert "\x1b[" not in out  # No ANSI cursor escapes: pipes stay clean.


def test_empty_chunks_are_ignored(printer: ModuleType, capsys: CaptureFixture[str]) -> None:
    start = begin(printer, capsys)
    printer.print_event(AudioTranscriptDeltaEvent(delta=""), start)
    printer.print_event(TextChunkEvent(chunk=""), start)
    printer.print_event(InputTranscriptDeltaEvent(delta=""), start)

    assert capsys.readouterr().out == ""
    assert printer._stream_kind == ""


def test_different_stream_kind_starts_a_new_line(printer: ModuleType, capsys: CaptureFixture[str]) -> None:
    start = begin(printer, capsys)
    printer.print_event(TextChunkEvent(chunk="alpha"), start)
    printer.print_event(AudioTranscriptDeltaEvent(delta="spoken"), start)

    lines = capsys.readouterr().out.split("\n")
    assert len(lines) == 2
    assert "text" in lines[0] and lines[0].endswith("alpha")
    assert "assistant" in lines[1] and lines[1].endswith("spoken")


def test_suppressed_debug_audio_does_not_split_info_stream(printer: ModuleType, capsys: CaptureFixture[str]) -> None:
    start = begin(printer, capsys)
    printer.print_event(TextChunkEvent(chunk="alpha"), start)
    audio = base64.b64encode(b"\x00\x01").decode()
    printer.print_event(AudioChunkEvent(chunk=audio), start)  # DEBUG only: suppressed at INFO.
    printer.print_event(TextChunkEvent(chunk="beta"), start)

    out = capsys.readouterr().out
    assert "\n" not in out
    assert out.count("text") == 1
    assert out.endswith("alphabeta")


def test_ordinary_info_log_terminates_partial_stream(printer: ModuleType, capsys: CaptureFixture[str]) -> None:
    start = begin(printer, capsys)
    printer.print_event(TextChunkEvent(chunk="partial"), start)

    handler = logging.StreamHandler()
    handler.addFilter(printer._FinishStreamBeforeLog())
    printer.logger.addHandler(handler)
    printer.logger.propagate = False
    try:
        printer.logger.info("ordinary record")
    finally:
        printer.logger.removeHandler(handler)
        printer.logger.propagate = True

    captured = capsys.readouterr()
    assert captured.out.endswith("partial\n")
    assert "ordinary record" in captured.err


def test_finish_stream_is_idempotent_and_safe(printer: ModuleType, capsys: CaptureFixture[str]) -> None:
    start = begin(printer, capsys)
    assert printer._finish_stream() is None  # No active stream: nothing to close.
    assert capsys.readouterr().out == ""

    printer.print_event(TextChunkEvent(chunk="value"), start)
    printer._finish_stream()
    printer._finish_stream()  # Repeated end/flush must not raise or double-newline.

    out = capsys.readouterr().out
    assert out.endswith("value\n")
    assert out.count("\n") == 1
    assert printer._stream_kind == ""


def test_all_event_types_do_not_crash(printer: ModuleType, capsys: CaptureFixture[str]) -> None:
    """The previous logger end/flush path must not raise for any printed event."""
    start = begin(printer, capsys)
    events: list[object] = [
        AudioChunkEvent(chunk=base64.b64encode(b"\x00\x01").decode()),
        AudioTranscriptDeltaEvent(delta="spoken"),
        TextChunkEvent(chunk="text"),
        ReasoningChunkEvent(chunk="reasoning"),
        InputTranscriptDeltaEvent(delta="partial"),
        InputTranscriptCompletedEvent(transcript="final"),
        SpeechStartedEvent(),
        SpeechStoppedEvent(),
        ResponseCreatedEvent(),
        ResponseCancelledEvent(),
        RealtimeSessionCreatedEvent(session_id="session"),
        RealtimeSessionUpdatedEvent(),
        RealtimeRateLimitsEvent(rate_limits=[{"name": "requests", "remaining": 1, "limit": 2, "reset_seconds": 3}]),
        ToolCallEvent(id="call-1", name="work", arguments={"value": 1}),
        ToolResultEvent(id="call-1", name="work", result="ok"),
        DoneEvent(final_text="done"),
        ErrorEvent(message="boom"),
        RealtimeRawEvent(event_type="raw", payload={}),
        object(),
    ]
    for event in events:
        printer.print_event(event, start)
    printer._finish_stream()
    assert printer._stream_kind == ""
