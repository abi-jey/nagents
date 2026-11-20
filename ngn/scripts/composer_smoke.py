"""Exercise native Enter routing through a POSIX PTY with offline protocol fixtures."""

from __future__ import annotations

import argparse
import json
import os
import pty
import select
import signal
import sqlite3
import subprocess
import sys
import tempfile
import termios
import time
from collections.abc import Callable
from pathlib import Path

from steering_smoke import require
from subagent_smoke import terminal_text

ENTER = b"\r"
QUEUE = b"\x1b[13;5u"
KEYS_ON = b"\x1b[>1u"
KEYS_OFF = b"\x1b[<1u"


def check(binary: Path, root: Path, *, reduced_motion: bool) -> None:
    config = root / "config.json"
    state = root / "state"
    config.write_text(
        json.dumps(
            {
                "data_dir": str(state),
                "runner": [
                    sys.executable,
                    str(Path(__file__).with_name("steering_smoke.py")),
                    "--runtime-fixture",
                    "composer" if reduced_motion else "legacy-composer",
                ],
                "provider": "fixture",
                "providers": {
                    "fixture": {
                        "kind": "openai_compatible",
                        "model": "offline",
                        "api": "chat_completions",
                        "base_url": "http://127.0.0.1:1/v1",
                        "api_key_env": "NGN_COMPOSER_FIXTURE_KEY",
                    }
                },
            }
        )
    )
    master, slave = pty.openpty()
    rows, columns = 36, 130
    termios.tcsetwinsize(slave, (rows, columns))
    original = termios.tcgetattr(slave)
    env = dict(os.environ, NGN_COMPOSER_FIXTURE_KEY="fixture-only", TERM="xterm-256color")
    env["TEXTUAL_ANIMATIONS"] = "none" if reduced_motion else "full"
    history = bytearray()
    process = subprocess.Popen(
        [str(binary), "--config", str(config), "--no-wire-log", "-C", str(root)],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=env,
        start_new_session=True,
    )

    def visible() -> bytes:
        return terminal_text(bytes(history), rows, columns)

    def wait_for(predicate: Callable[[], bool], phase: str) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if predicate():
                return
            if select.select([master], [], [], 0.03)[0]:
                history.extend(os.read(master, 65536))
            elif process.poll() is not None:
                break
        if predicate():
            return
        raise AssertionError(f"{phase}: process={process.poll()}, screen={visible()!r}")

    try:
        if not reduced_motion:
            wait_for(lambda: b"Opening workspace..." in history, "startup prelude")
            os.write(master, b"startup draft\r\r")
        wait_for(lambda: KEYS_ON in history, "keyboard enhancement after input handoff")
        database = next(state.rglob("host.sqlite3"))

        def runs() -> list[tuple[str, str, str]]:
            with sqlite3.connect(database) as connection:
                return connection.execute("SELECT id, prompt, status FROM runs ORDER BY rowid").fetchall()

        def receipts() -> list[tuple[str, str, str]]:
            with sqlite3.connect(database) as connection:
                return connection.execute(
                    "SELECT run_id, prompt, status FROM steering_inputs ORDER BY position"
                ).fetchall()

        if reduced_motion:
            require(b"Opening workspace..." not in history, "Reduced motion animated the startup")
            os.write(master, b"idle Enter" + ENTER)
            prompt = "idle Enter"
        else:
            require(b"startup draft" in visible() and not runs(), "Startup Enter submitted instead of buffering")
            os.write(master, b"\x7f" * len("startup draft") + b"idle Ctrl+Enter" + QUEUE)
            prompt = "idle Ctrl+Enter"
        wait_for(lambda: b"Ctrl+Y" in visible() and bool(runs()), "root reached native approval")
        run = runs()[0][0]
        require(runs() == [(run, prompt, "running")], "Idle submission did not start exactly one root")
        os.write(master, b"approval focus" + ENTER + QUEUE + b"\x04")  # Ctrl+D denies the tool.
        wait_for(
            lambda: b"fixture typing window" in visible() and b"Ctrl+Y" not in visible(),
            "approval held focus and Ctrl+D left the root active",
        )
        require(len(runs()) == 1 and not receipts() and b"approval focus" not in visible(), "Enter bypassed approval")

        os.write(master, b"active Enter" + ENTER)
        if reduced_motion:
            wait_for(lambda: receipts() == [(run, "active Enter", "pending")], "active Enter steered the exact root")
            os.write(master, b"/steer explicit steering" + ENTER)
            wait_for(
                lambda: receipts() == [(run, "active Enter", "pending"), (run, "explicit steering", "pending")],
                "explicit /steer used the same exact root",
            )
            os.write(master, b"active Ctrl+Enter" + QUEUE)
            wait_for(lambda: len(runs()) == 2, "active Ctrl+Enter admitted to FIFO")
            require(runs()[1][1:] == ("active Ctrl+Enter", "queued"), "Explicit FIFO took the steering path")
        else:
            wait_for(lambda: b"not negotiated safe_steering" in visible(), "unsupported root rejection")
            require(
                b"active Enter" in visible() and len(runs()) == 1 and not receipts(), "Rejection lost or queued draft"
            )
        require(not (root / "once.txt").exists(), "An Enter bypassed approval")

        os.write(master, b"\x03")  # Ctrl+C stops and pauses the FIFO.
        wait_for(lambda: runs()[0][2] == "cancelled" and b"Ctrl+Y" not in visible(), "cancelled root")
        if reduced_motion:
            require(
                receipts() == [(run, "active Enter", "undelivered"), (run, "explicit steering", "undelivered")],
                "Stopping did not settle the receipts",
            )
        else:
            os.write(master, ENTER)
            wait_for(lambda: b"exact running root" in visible(), "stale rejected draft retried its original root")
            require(b"active Enter" in visible() and len(runs()) == 1, "Retry queued or lost the stale draft")
            os.write(master, b"\x7f" * len("active Enter"))

        os.write(master, b"paused Ctrl+Enter" + QUEUE + b"paused Enter" + ENTER)
        wait_for(lambda: runs()[-1][1] == "paused Enter", "paused FIFO admission")
        require(
            [row[1:] for row in runs()[-2:]] == [("paused Ctrl+Enter", "queued"), ("paused Enter", "queued")],
            "Paused inputs ran or changed FIFO order",
        )
        if reduced_motion:
            process.send_signal(signal.SIGTERM)
        else:
            os.write(master, b"\x11")  # Ctrl+Q
        wait_for(lambda: process.poll() is not None and KEYS_OFF in history, "shutdown with keyboard restore")
        require(
            process.returncode == 0
            and history.index(b"\x1b[?1049h") < history.index(KEYS_ON) < history.index(KEYS_OFF)
            and history.index(KEYS_OFF) < history.index(b"\x1b[?1049l")
            and termios.tcgetattr(slave) == original,
            "Exit failed to restore the alternate-screen keyboard stack and termios",
        )
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(slave)
        os.close(master)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    args = parser.parse_args()
    for reduced in (False, True):
        with tempfile.TemporaryDirectory(prefix="ngn-composer-") as directory:
            check(args.binary.resolve(), Path(directory), reduced_motion=reduced)
    print("PASS: startup handoff, idle/busy Enter and Ctrl+Enter, approval, stale retry, paused FIFO, and tty restore")
