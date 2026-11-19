"""Positive stdin readiness for native interactive smoke fixtures."""

import re


class WorkspaceReady:
    def __init__(self, master: int) -> None:
        import termios

        self.master = master
        self.rows, self.columns = termios.tcgetwinsize(master)
        self.resize_at: int | None = None

    def observe(self, output: bytes, first_frame_visible: bool) -> bool:
        import termios

        if self.resize_at is None:
            if first_frame_visible:
                self.resize_at = len(output)
                self.rows += 1
                termios.tcsetwinsize(self.master, (self.rows, self.columns))
            return False

        # Startup owns stdin through the first draw and its final draft-only
        # drain. Grow by one row so no pending bytes from the first frame can
        # satisfy the readiness check. Ratatui clears on resize, then paints
        # the workspace footer on the new bottom row.
        _, cleared, repaint = output[self.resize_at :].partition(b"\x1b[2J")
        return bool(cleared and re.search(rb"\x1b\[%d;[0-9]+H" % self.rows, repaint))
