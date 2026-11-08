"""POSIX Textual driver with a color-reply frame ahead of the key parser.

Textual's default parser treats OSC 10/11 replies as keystrokes. Keep its normal
event parser, UTF-8 decoder, mouse handling and lifecycle; only frame those
replies at the decoded-input boundary. Imported only for the standard POSIX
driver; headless, Windows, inline and explicit custom drivers stay unchanged.
"""

from __future__ import annotations

import os
import selectors
from codecs import getincrementaldecoder
from contextlib import suppress
from time import monotonic

from textual._parser import ParseError
from textual._xterm_parser import XTermParser
from textual.drivers.linux_driver import LinuxDriver

from .terminal_colors import QUERY_COLORS
from .terminal_colors import ColorReplies
from .terminal_colors import TerminalColorReported


class TerminalDriver(LinuxDriver):
    def run_input_thread(self) -> None:
        if self.input_tty:
            self.write(QUERY_COLORS)
            self.flush()
        parser = XTermParser(self._debug)
        decode = getincrementaldecoder("utf-8")().decode
        replies = ColorReplies()
        with selectors.SelectSelector() as selector:
            selector.register(self.fileno, selectors.EVENT_READ)
            try:
                while not self.exit_event.is_set():
                    for _key, mask in selector.select(0.025):
                        if mask & selectors.EVENT_READ:
                            data = os.read(self.fileno, 4096)
                            if not data:
                                return
                            previous = replies.colors
                            text = replies.feed(decode(data), monotonic())
                            if replies.colors != previous:
                                self.process_message(TerminalColorReported(replies.colors))
                            for event in parser.feed(text) if text else ():
                                self.process_message(event)
                    expired = replies.expire(monotonic())
                    for event in parser.feed(expired) if expired else ():
                        self.process_message(event)
                    for event in parser.tick():
                        self.process_message(event)
            finally:
                with suppress(EOFError, ParseError):
                    for _event in parser.feed(""):
                        pass
