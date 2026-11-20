"""Exercise the real PTY smoke driver's terminal replay."""

import unittest

from pty_smoke import COMPOSER_TITLE
from subagent_smoke import terminal_text


class TerminalReplayTest(unittest.TestCase):
    def test_prelude_cursor_and_alternate_screen(self) -> None:
        prelude = b"\x1b[?25l\r\n|old|\r\n\x1b7\x1b8\x1b[1A\r|new|\r\n\x1b8\x1b[?25h"
        self.assertEqual(terminal_text(prelude), b"|new|")

        # The driver replays accumulated bytes, so an incomplete sequence can
        # only hide text until the next read, not hide every subsequent frame.
        self.assertEqual(terminal_text(prelude + b"\x1b[?1049"), b"|new|")
        workspace = prelude + b"\x1b[?1049h\x1b[2;1H" + COMPOSER_TITLE
        self.assertEqual(terminal_text(workspace), COMPOSER_TITLE)
        self.assertEqual(terminal_text(workspace + b"\x1b[?1049l"), b"|new|")

    def test_partial_redraw_uses_visible_cells_not_a_raw_output_match(self) -> None:
        output = b"\x1b[?1049h\x1b[2;1H" + COMPOSER_TITLE + b"\x1b[2;9H\x1b[K"
        self.assertEqual(terminal_text(output), b"Message")
