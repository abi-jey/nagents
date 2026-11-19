"""Exercise the queue smoke driver's shutdown against real PTY backpressure."""

import os
import subprocess
import sys
import unittest

from queue_smoke import _wait_for_tui_exit


@unittest.skipUnless(os.name == "posix", "requires a PTY")
class QueueSmokeTest(unittest.TestCase):
    def test_drains_shutdown_output_and_reports_final_error(self) -> None:
        import pty

        master, slave = pty.openpty()
        try:
            # This won't fit in the PTY buffer; the child cannot exit until drained.
            with subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.stdout.write('x' * 262144); sys.stdout.flush(); "
                    "sys.stderr.write('\\x1b[2J\\x1b[Hshutdown error\\n'); sys.exit(3)",
                ],
                stdout=slave,
                stderr=slave,
            ) as process:
                try:
                    with self.assertRaisesRegex(
                        AssertionError, r"Resumed queue TUI exited with status 3.*\nFinal pane: shutdown error"
                    ) as caught:
                        _wait_for_tui_exit(process, master, b"", "Resumed", timeout=5)
                    self.assertIn(r"\x1b[2J\x1b[Hshutdown error", str(caught.exception))
                finally:
                    if process.poll() is None:
                        process.kill()
        finally:
            os.close(master)
            os.close(slave)
