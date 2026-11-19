"""Readiness must use a new workspace paint, not leftover startup bytes."""

import os
import unittest

from pty_smoke import WorkspaceReady


@unittest.skipUnless(os.name == "posix", "requires a PTY")
class WorkspaceReadyTest(unittest.TestCase):
    def test_waits_for_repaint_at_grown_pty_size(self) -> None:
        import pty
        import termios

        master, slave = pty.openpty()
        try:
            termios.tcsetwinsize(slave, (40, 140))
            ready = WorkspaceReady(master)
            # Even paint-like startup bytes cannot trigger or satisfy readiness.
            prelude = b"\x1b[2J\x1b[41;3H"
            self.assertFalse(ready.observe(prelude, False))
            self.assertEqual(termios.tcgetwinsize(slave), (40, 140))
            first = prelude + b"\x1b[?1049hWelcome to ngn"
            self.assertFalse(ready.observe(first, True))
            self.assertEqual(termios.tcgetwinsize(slave), (41, 140))
            old_tail = first + b"\x1b[40;3Hold footer"
            self.assertFalse(ready.observe(old_tail, True))
            self.assertFalse(ready.observe(old_tail + b"\x1b[41;3H", True))  # No resize clear.
            repaint = old_tail + b"\x1b[2J\x1b[41;3"
            self.assertFalse(ready.observe(repaint, True))
            self.assertTrue(ready.observe(repaint + b"H", True))
            self.assertEqual(termios.tcgetwinsize(slave), (41, 140))
        finally:
            os.close(master)
            os.close(slave)
