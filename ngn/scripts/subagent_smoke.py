"""Real HTTP/WebSocket and PTY subagent regressions, invoked by smoke.py."""

from __future__ import annotations

import asyncio
import os
import re
import signal
import sqlite3
import time
import unicodedata
import uuid
from collections.abc import Callable
from pathlib import Path

import aiohttp
from pty_smoke import COMPOSER_TITLE, WorkspaceReady


def require(condition: object, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def terminal_rows(output: bytes, rows: int, columns: int) -> list[str]:
    """Replay the VT subset used by the native startup prelude and Ratatui."""
    screen = [[" " for _ in range(columns)] for _ in range(rows)]
    text = output.decode("utf-8", errors="replace")
    cursor = re.compile(r"\x1b\[([0-?]*)([ -/]*)([@-~])")
    row = column = index = 0
    saved_cursor = (0, 0)
    normal_screen: tuple[list[list[str]], int, int, tuple[int, int]] | None = None
    while index < len(text):
        if text[index] == "\x1b":
            if text.startswith("\x1b]11;?\x1b\\", index):
                index += len("\x1b]11;?\x1b\\")
                continue
            if text.startswith("\x1b7", index):
                saved_cursor = (row, column)
                index += 2
                continue
            if text.startswith("\x1b8", index):
                row, column = saved_cursor
                index += 2
                continue
            match = cursor.match(text, index)
            if match is None:
                break
            raw, _, operation = match.groups()
            parameters = [int(value or "0") for value in raw.split(";")] if re.fullmatch(r"[0-9;]*", raw) else []
            if raw == "?1049" and operation == "h" and normal_screen is None:
                normal_screen = (screen, row, column, saved_cursor)
                screen = [[" " for _ in range(columns)] for _ in range(rows)]
                row = column = 0
                saved_cursor = (0, 0)
            elif raw == "?1049" and operation == "l" and normal_screen is not None:
                screen, row, column, saved_cursor = normal_screen
                normal_screen = None
            elif operation in {"H", "f"}:
                row = min(rows - 1, max(0, (parameters[0] if parameters else 1) - 1))
                column = min(
                    columns - 1,
                    max(0, (parameters[1] if len(parameters) > 1 else 1) - 1),
                )
            elif operation == "A":
                row = max(0, row - ((parameters[0] if parameters else 0) or 1))
            elif operation == "J" and parameters == [2]:
                screen = [[" " for _ in range(columns)] for _ in range(rows)]
            elif operation == "K":
                start = 0 if parameters and parameters[0] in {1, 2} else column
                end = column + 1 if parameters == [1] else columns
                screen[row][start:end] = [" "] * (end - start)
            index = match.end()
            continue
        character = text[index]
        index += 1
        if character == "\r":
            column = 0
        elif character == "\n":
            row = min(rows - 1, row + 1)
        elif not character.isprintable():
            continue
        else:
            if column >= columns:
                row = min(rows - 1, row + 1)
                column = 0
            if not unicodedata.combining(character):
                screen[row][column] = character
                column += 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1
    return ["".join(line) for line in screen]


def terminal_text(output: bytes, rows: int = 40, columns: int = 140) -> bytes:
    return " ".join("".join(terminal_rows(output, rows, columns)).split()).encode()


async def http_checks(binary: Path, config: Path, workspace: Path) -> None:
    process = await asyncio.create_subprocess_exec(
        str(binary),
        "--config",
        str(config),
        "-C",
        str(workspace),
        "serve",
        "--port",
        "0",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        require(process.stderr is not None, "Missing startup stream")
        url = ""
        for _ in range(8):
            line = (await asyncio.wait_for(process.stderr.readline(), 10)).decode()
            match = re.search(r"Open (http://\S+)", line)
            if match:
                url = match[1]
                break
        require(url, "Subagent HTTP host did not start")
        async with aiohttp.ClientSession() as client:
            async with client.get(url + "/api/bootstrap") as response:
                bootstrap = await response.json()
            headers = {"x-ngn-token": bootstrap["token"], "Origin": url}

            async def api(path: str, body: object = None, expected: int = 200) -> dict[str, object]:
                async with client.request(
                    "GET" if body is None else "POST",
                    url + path,
                    json=body,
                    headers=headers,
                ) as response:
                    payload = await response.json()
                    require(
                        response.status == expected,
                        f"{path}: {response.status} {payload}",
                    )
                    return payload

            async def conversation(
                prompt: str,
                *,
                approve: bool = False,
                cancel: str = "",
                expected: str = "completed",
            ) -> tuple[str, list[dict[str, object]], list[dict[str, object]]]:
                created = await api("/api/sessions", {})
                session = created["session"]["id"]
                records = []
                approvals = []
                children = []
                async with client.ws_connect(
                    url + f"/api/events?session_id={session}&after=0",
                    protocols=["ngn.events.v1", "ngn.token." + bootstrap["token"]],
                    headers={"Origin": url},
                ) as socket:
                    accepted = await api(
                        f"/api/sessions/{session}/runs",
                        {"prompt": prompt, "request_id": str(uuid.uuid4())},
                    )
                    root_run = accepted["run"]["id"]
                    while True:
                        record = await asyncio.wait_for(socket.receive_json(), 30)
                        records.append(record)
                        require(
                            record["session_id"] == session
                            and (
                                record["run_id"] == root_run
                                or (record["event"] == "queue_updated" and record["run_id"] == "")
                            ),
                            "Child events escaped into the parent's stream",
                        )
                        if record["event"] == "task_started":
                            children.append(record["task"])
                            require(
                                record["task"]["parent_run_id"] == root_run,
                                "Child was not bound to its owning root",
                            )
                            if cancel:
                                target = root_run if cancel == "root" else record["task"]["run_id"]
                                await api(f"/api/runs/{target}/cancel", {})
                        if record["event"] == "task_updated" and approve:
                            state = await api("/api/state")
                            for approval in state["approvals"]:
                                if approval["root_session_id"] != session:
                                    continue
                                require(
                                    approval["session_id"] != session and approval["depth"] == 1,
                                    "Child approval was misattributed to the parent",
                                )
                                require(
                                    not (workspace / "delegated.txt").exists(),
                                    "Child wrote before its own approval",
                                )
                                await api(
                                    "/api/approvals",
                                    {
                                        "run_id": root_run,
                                        "approval_id": approval["id"],
                                        "allow": True,
                                    },
                                    409,
                                )
                                await api(
                                    "/api/approvals",
                                    {
                                        "run_id": approval["run_id"],
                                        "approval_id": approval["id"],
                                        "allow": True,
                                    },
                                )
                                approvals.append(approval)
                        if record["event"] == "run_finished":
                            require(
                                record["status"] == expected,
                                f"Unexpected parent terminal state: {record}",
                            )
                            break
                catalog = (await api(f"/api/sessions/{session}/tasks"))["tasks"]
                require(
                    len(catalog) == len(children),
                    "Task catalog disagrees with accepted delegations",
                )
                require(
                    all(task["status"] != "running" for task in catalog),
                    "Parent finished with orphaned child work",
                )
                for task in catalog:
                    require(
                        "result" not in task,
                        "Task polling catalog must remain metadata-only",
                    )
                    await api(
                        f"/api/sessions/{task['session_id']}/runs",
                        {"prompt": "resume as root", "request_id": str(uuid.uuid4())},
                        409,
                    )
                    async with client.ws_connect(
                        url + f"/api/events?session_id={task['session_id']}&after=0",
                        protocols=["ngn.events.v1", "ngn.token." + bootstrap["token"]],
                        headers={"Origin": url},
                    ) as child_socket:
                        child_records = []
                        while True:
                            child_record = await asyncio.wait_for(child_socket.receive_json(), 10)
                            require(
                                child_record["session_id"] == task["session_id"],
                                "Child inspection crossed a conversation boundary",
                            )
                            child_records.append(child_record)
                            if child_record["event"] == "run_finished":
                                break
                        require(
                            child_records[-1]["status"] == task["status"],
                            "Durable child replay lost terminal state",
                        )
                return session, records, approvals

            _, background, _ = await conversation("delegate two background reads")
            require(
                sum(record["event"] == "task_started" for record in background) == 2,
                "Background delegation did not spawn two supervised agents",
            )
            delivered = [
                record
                for record in background
                if record["event"] == "tool_completed" and record["result"]["name"] == "agent_wait"
            ]
            require(
                len(delivered) == 2
                and all(record["result"]["result"]["task"]["status"] == "completed" for record in delivered),
                "Parent did not receive both child results through agent_wait",
            )
            _, denied, approvals = await conversation("delegate read-only write")
            require(
                not approvals and not (workspace / "delegated.txt").exists(),
                "A child escaped its read-only permission ceiling",
            )
            readonly = next(record["task"] for record in denied if record["event"] == "task_started")
            require(readonly["read_only"], "Host did not persist the permission ceiling")
            child_history = (await api(f"/api/sessions/{readonly['session_id']}/events?after=0"))["events"]
            require(
                any(
                    record["event"] == "tool_completed"
                    and record["result"]["name"] == "write"
                    and "disabled" in record["result"]["error"]
                    for record in child_history
                ),
                "Forged child write was not denied in Rust",
            )
            _, written, approvals = await conversation("delegate a write", approve=True)
            require(
                len(approvals) == 1
                and (workspace / "delegated.txt").read_text() == "written by an approved Rust host tool\n",
                "Child tool was not approved and executed by Rust",
            )
            returned = next(
                record["result"]["result"]
                for record in written
                if record["event"] == "tool_completed" and record["result"]["name"] == "to_agent"
            )
            require(
                returned["task"]["status"] == "completed" and returned["result"],
                "Foreground delegation did not return the child's public result",
            )
            _, cancelled, _ = await conversation("delegate hold", cancel="child")
            require(
                any(
                    record["event"] == "task_finished" and record["task"]["status"] == "cancelled"
                    for record in cancelled
                ),
                "Child-only cancellation was not observed",
            )
            _, tree_cancelled, _ = await conversation("delegate hold", cancel="root", expected="cancelled")
            child_finishes = [record["sequence"] for record in tree_cancelled if record["event"] == "task_finished"]
            require(
                child_finishes and max(child_finishes) < tree_cancelled[-1]["sequence"],
                "Parent terminated before recording child cancellation",
            )
            await conversation("delegate hold orphan", expected="failed")
            state = await api("/api/state")
            require(
                not state["runs"] and not state["approvals"],
                "Supervisor leaked an active runner or pending approval",
            )
    finally:
        if process.returncode is None:
            process.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), 15)
            except TimeoutError:
                process.kill()
                await process.wait()
        require(process.returncode == 0, "Subagent web host did not exit cleanly")


def tui_check(binary: Path, config: Path, workspace: Path, data: Path) -> None:
    import fcntl
    import pty
    import select
    import struct
    import subprocess
    import termios

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 140, 0, 0))
    before = termios.tcgetattr(slave)
    process = subprocess.Popen(
        [str(binary), "--config", str(config), "-C", str(workspace), "--data-dir", str(data)],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
    )
    os.close(slave)
    output = b""
    try:
        ready = WorkspaceReady(master)

        def visible() -> bytes:
            return terminal_text(output, ready.rows, ready.columns)

        def wait_for(predicate: Callable[[], bool], explanation: str) -> None:
            nonlocal output
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                if predicate():
                    return
                if select.select([master], [], [], 0.05)[0]:
                    try:
                        output += os.read(master, 65536)
                    except OSError:
                        process.wait(timeout=2)
                        if predicate():
                            return
                        raise
            raise AssertionError(explanation)

        wait_for(
            lambda: ready.observe(output, COMPOSER_TITLE in visible()),
            "Subagent TUI did not render and repaint at the resized PTY dimensions",
        )
        os.write(master, b"delegate hold\r")
        database = next(data.rglob("host.sqlite3"))

        def started() -> bool:
            with sqlite3.connect(database) as connection:
                return connection.execute("SELECT COUNT(*) FROM subagents").fetchone()[0] == 1

        wait_for(started, "TUI did not start a Rust-supervised child")
        os.write(master, b"\x1b[15~")  # F5 opens the shared task catalog.
        wait_for(
            lambda: b"Waiting delegate" in visible() and b"Ctrl+X" in visible(),
            "TUI task pane did not show supervised child controls",
        )
        os.write(master, b"\r")
        wait_for(lambda: b"Agent:" in visible(), "TUI did not inspect the child conversation")
        os.write(master, b"\x18")  # Ctrl+X cancels this child only.

        def completed() -> bool:
            with sqlite3.connect(database) as connection:
                root = connection.execute(
                    "SELECT status FROM runs WHERE id NOT IN (SELECT child_run_id FROM subagents)"
                ).fetchone()[0]
                child = connection.execute(
                    "SELECT r.status FROM runs r JOIN subagents t ON t.child_run_id = r.id"
                ).fetchone()[0]
                return root == "completed" and child == "cancelled"

        wait_for(completed, "TUI child cancellation did not leave its parent able to finish")
        os.write(master, b"\x1b\x1b")  # Return to the root conversation.
        os.write(master, b"\x11")
        wait_for(
            lambda: process.poll() is not None,
            "TUI did not cleanly close its task supervisor",
        )
        require(process.returncode == 0, "Subagent TUI exit was unsuccessful")
        restored = termios.tcgetattr(master)
        mask = termios.ECHO | termios.ICANON | termios.ISIG
        require(
            restored[3] & mask == before[3] & mask,
            "Subagent TUI did not restore terminal mode",
        )
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)
