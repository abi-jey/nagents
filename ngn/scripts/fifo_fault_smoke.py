"""Inject a post-enqueue dispatch failure in a disposable native TUI store."""

from __future__ import annotations

import argparse
import json
import os
import pty
import select
import sqlite3
import subprocess
import tempfile
import termios
import time
from collections.abc import Callable
from pathlib import Path

from pty_smoke import COMPOSER_TITLE, WorkspaceReady
from subagent_smoke import require, terminal_text


def check(binary: Path, root: Path, *, ctrl_enter: bool) -> None:
    config = root / "config.json"
    state = root / "state"
    config.write_text(
        json.dumps(
            {
                "data_dir": str(state),
                "runner": ["false"],  # The failing FIFO head must prevent any launch.
                "provider": "fixture",
                "providers": {
                    "fixture": {
                        "kind": "openai_compatible",
                        "model": "offline",
                        "api": "chat_completions",
                        "base_url": "http://127.0.0.1:1/v1",
                        "api_key_env": "NGN_FIFO_FAULT_KEY",
                    }
                },
            }
        )
    )
    master, slave = pty.openpty()
    termios.tcsetwinsize(slave, (32, 100))
    original = termios.tcgetattr(slave)
    ready = WorkspaceReady(master)
    env = dict(os.environ, NGN_FIFO_FAULT_KEY="fixture-only", TERM="xterm-256color", TEXTUAL_ANIMATIONS="none")
    for name in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR"):
        path = root / name.lower()
        path.mkdir(mode=0o700)
        env[name] = str(path)
    process = subprocess.Popen(
        [str(binary), "--config", str(config), "-C", str(root), "--no-wire-log"],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=env,
        start_new_session=True,
    )
    history = bytearray()

    def view() -> bytes:
        return terminal_text(bytes(history), ready.rows, ready.columns)

    def wait_for(predicate: Callable[[], bool], phase: str) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if predicate():
                return
            if select.select([master], [], [], 0.03)[0]:
                history.extend(os.read(master, 65536))
            elif process.poll() is not None:
                break
        if not predicate():
            raise AssertionError(f"{phase}: {view()!r}")

    try:
        wait_for(lambda: ready.observe(history, COMPOSER_TITLE in view()), "workspace input readiness")
        database = next(state.rglob("host.sqlite3"))
        with sqlite3.connect(database) as connection:
            connection.execute(
                """CREATE TRIGGER fail_dispatch BEFORE UPDATE OF status ON runs
                   WHEN OLD.prompt = 'original FIFO draft' AND NEW.status = 'running'
                   BEGIN SELECT RAISE(ABORT, 'fixture dispatch after enqueue'); END"""
            )

        def runs() -> list[tuple[str, str, str]]:
            with sqlite3.connect(database) as connection:
                return connection.execute("SELECT prompt, status, request_id FROM runs ORDER BY rowid").fetchall()

        enter = b"\x1b[13;5u" if ctrl_enter else b"\r"
        os.write(master, b"original FIFO draft" + enter)
        wait_for(
            lambda: b"Input accepted" in view() and b"fixture dispatch after enqueue" in view(),
            "durable acceptance plus dispatch warning",
        )
        first = runs()[0]
        require(first[:2] == ("original FIFO draft", "queued"), "Dispatch failure lost the accepted input")
        require(b"Not sent:" not in history, "Known admission was shown as not sent")
        os.write(master, enter + b"second FIFO draft" + enter)
        wait_for(lambda: len(runs()) == 2, "empty Enter did not retry and new draft was admitted")
        require([row[:2] for row in runs()] == [first[:2], ("second FIFO draft", "queued")], "FIFO or draft changed")

        os.write(master, b"original FIFO draft" + enter)
        wait_for(lambda: len(runs()) == 3, "deliberate identical submission admitted after confirmation")
        require(runs()[2][:2] == first[:2] and runs()[2][2] != first[2], "New intent did not get a fresh request")
        os.write(master, b"\x11")
        wait_for(lambda: process.poll() is not None, "terminal exit")
        require(process.returncode == 0 and termios.tcgetattr(slave) == original, "Failed to restore terminal")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(slave)
        os.close(master)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--ctrl-enter", action="store_true")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="ngn-fifo-fault-") as directory:
        check(args.binary.resolve(), Path(directory), ctrl_enter=args.ctrl_enter)
    print("PASS: post-enqueue dispatch warning, no retained retry, exact FIFO order and intentional identical repeat")
