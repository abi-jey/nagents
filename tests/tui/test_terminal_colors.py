"""Terminal color replies are filtered before Textual turns them into keys."""

from __future__ import annotations

import os
import time
from threading import Event

import pytest
from textual._xterm_parser import XTermParser
from textual.events import Paste

from nagents.tui.terminal_colors import QUERY_COLORS
from nagents.tui.terminal_colors import ColorReplies
from nagents.tui.terminal_colors import TerminalColors

if os.name == "posix":
    from textual.drivers.linux_driver import LinuxDriver

    from nagents.tui.terminal_driver import TerminalDriver


@pytest.mark.parametrize(
    ("terminator", "field", "channels", "expected"),
    [
        ("\x07", "foreground", "0/1/f", "#0011ff"),
        ("\x1b\\", "background", "00/80/ff", "#0080ff"),
        ("\x07", "foreground", "000/800/fff", "#0080ff"),
        ("\x1b\\", "background", "0000/8080/ffff", "#0080ff"),
        ("\x07", "background", "1/12/1234", "#111212"),
    ],
)
def test_valid_color_replies_are_removed_at_every_fragment_boundary(
    terminator: str, field: str, channels: str, expected: str
) -> None:
    number = "10" if field == "foreground" else "11"
    reply = f"\x1b]{number};rgb:{channels}{terminator}"
    for split in range(len(reply) + 1):
        replies = ColorReplies()
        first = replies.feed(reply[:split], 0.0)
        second = replies.feed(reply[split:], 0.01)
        assert first + second == ""
        assert getattr(replies.colors, field) == expected


def test_color_reports_update_independently_and_leave_typing_and_arrows_intact() -> None:
    replies = ColorReplies()
    foreground = "\x1b]10;rgb:ffff/cccc/9999\x07"
    background = "\x1b]11;rgb:1212/2424/3636\x1b\\"
    raw = "typed\x1b[A\x1bx"
    framed = replies.feed(raw + foreground + "more" + background, 1.0)
    assert framed == raw + "more"
    assert replies.colors == TerminalColors(foreground="#ffcc99", background="#122436")

    parser = XTermParser(False)
    events = list(parser.feed(framed))
    time.sleep(0.15)
    events.extend(parser.tick())
    assert [event.key for event in events if hasattr(event, "key")] == [
        "t",
        "y",
        "p",
        "e",
        "d",
        "up",
        "alt+x",
        "m",
        "o",
        "r",
        "e",
    ]


def test_valid_looking_osc_reply_inside_bracketed_paste_stays_literal() -> None:
    reply = "\x1b]11;rgb:1212/2424/3636\x07"
    paste = f"\x1b[200~before {reply} after\x1b[201~"
    replies = ColorReplies()
    for split in range(len(paste) + 1):
        replies = ColorReplies()
        framed = replies.feed(paste[:split], 0.0) + replies.feed(paste[split:], 0.01)
        assert framed == paste
        assert replies.colors == TerminalColors()
        baseline_events = list(XTermParser(False).feed(paste))
        filtered_events = list(XTermParser(False).feed(framed))
        baseline = tuple(
            (type(event), str(getattr(event, "text", "")), str(getattr(event, "key", ""))) for event in baseline_events
        )
        filtered = tuple(
            (type(event), str(getattr(event, "text", "")), str(getattr(event, "key", ""))) for event in filtered_events
        )
        assert filtered == baseline


def test_ordinary_multiline_unicode_bracketed_paste_matches_textual_baseline() -> None:
    paste = "\x1b[200~first line\nCafé 日本語\x1b[201~"
    framed = ColorReplies().feed(paste, 0.0)
    assert framed == paste
    events = list(XTermParser(False).feed(framed))
    pasted = [event for event in events if isinstance(event, Paste)]
    assert len(pasted) == 1
    assert pasted[0].text == "first line\nCafé 日本語"


def test_malformed_and_overlong_replies_are_bounded_and_forwarded() -> None:
    replies = ColorReplies()
    malformed = "\x1b]11;rgb:12345/12/12\x07"
    assert replies.feed(malformed, 0.0) == malformed
    assert replies.colors == TerminalColors()

    incomplete = "\x1b]11;" + "x" * 10_000
    assert replies.feed(incomplete, 1.0) == incomplete
    assert replies._pending == ""


@pytest.mark.parametrize("sequence", ["\x1b]11;rgb:12/24/36", "\x1b[20", "\x1b"])
def test_incomplete_sequences_expire_without_losing_input(sequence: str) -> None:
    replies = ColorReplies()
    assert replies.feed(sequence, 4.0) == ""
    assert replies.expire(4.049) == ""
    assert replies.expire(4.051) == sequence
    assert replies.expire(4.1) == ""


@pytest.mark.requires_posix
def test_driver_only_issues_read_only_color_queries(monkeypatch: pytest.MonkeyPatch) -> None:
    writes: list[str] = []
    flushes: list[bool] = []
    reads: list[int] = []

    class FakeSelector:
        def __enter__(self) -> FakeSelector:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def register(self, _file_descriptor: int, _mask: int) -> None:
            return None

        def select(self, _timeout: float) -> list[tuple[None, int]]:
            return [(None, 1)]

    driver = TerminalDriver.__new__(TerminalDriver)
    driver.input_tty = True
    monkeypatch.setattr(LinuxDriver, "write", lambda _self, text: writes.append(text))
    monkeypatch.setattr(LinuxDriver, "flush", lambda _self: flushes.append(True))
    monkeypatch.setattr("nagents.tui.terminal_driver.selectors.SelectSelector", FakeSelector)

    def read(file_descriptor: int, _size: int) -> bytes:
        reads.append(file_descriptor)
        return b""

    monkeypatch.setattr(
        "nagents.tui.terminal_driver.os.read",
        read,
    )
    driver.fileno = 7
    driver.exit_event = Event()
    driver._debug = False

    driver.run_input_thread()

    assert reads == [7]
    assert writes == [QUERY_COLORS]
    assert flushes == [True]
    assert "10;?" in writes[0] and "11;?" in writes[0]
    assert all(marker not in writes[0] for marker in ("1049h", "?25l", "10;#", "11;#"))


@pytest.mark.requires_posix
def test_driver_skips_queries_when_input_is_not_a_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    writes: list[str] = []
    driver = TerminalDriver.__new__(TerminalDriver)
    driver.input_tty = False
    monkeypatch.setattr(LinuxDriver, "write", lambda _self, text: writes.append(text))
    monkeypatch.setattr(LinuxDriver, "flush", lambda _self: None)
    driver.fileno = 0
    driver.exit_event = Event()
    driver.exit_event.set()
    driver._debug = False

    driver.run_input_thread()

    assert writes == []
