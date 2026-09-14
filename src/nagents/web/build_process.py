"""Own and cancel the complete npm build process tree during development."""

import os
import signal
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextlib import suppress
from pathlib import Path


@contextmanager
def _interruptible() -> Iterator[None]:
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def interrupted(signum: int, frame: object) -> None:
        raise KeyboardInterrupt

    previous = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _stop_tree(process: subprocess.Popen[bytes]) -> None:
    if os.name == "nt":
        # npm.cmd and its node/tsc/Vite children must all stop before another build.
        with suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        with suppress(OSError):
            process.kill()
    else:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=5)


def run_build_command(command: list[str], *, cwd: Path) -> None:
    """Bound build duration and clean up descendants on signals, errors, or timeout."""
    creationflags = 0
    if sys.platform == "win32":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    with _interruptible():
        process = subprocess.Popen(
            command,
            cwd=cwd,
            start_new_session=os.name != "nt",
            creationflags=creationflags,
        )
        try:
            result = process.wait(timeout=300)
        except BaseException:
            _stop_tree(process)
            raise
        if result:
            # A failed npm parent can leave helpers running as well.
            _stop_tree(process)
            raise subprocess.CalledProcessError(result, command)
