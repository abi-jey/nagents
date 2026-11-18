"""Durable FIFO acceptance through actual HTTP/WebSockets and a real terminal."""

from __future__ import annotations

import asyncio
import os
import re
import signal
import sqlite3
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import aiohttp
from protocol_fixture import CRASH_NOTICE, CRASH_PROMPT
from subagent_smoke import require, terminal_text


async def http_checks(binary: Path, config: Path, workspace: Path, data: Path, crash_config: Path) -> None:
    process = None
    url = ""
    headers = {}
    token = ""

    async def launch(phase: str, settings: Path = config) -> None:
        nonlocal process, url, headers, token
        process = None
        stage = "spawning"
        stderr = b""
        try:
            async with asyncio.timeout(20):
                process = await asyncio.create_subprocess_exec(
                    str(binary),
                    "--config",
                    str(settings),
                    "-C",
                    str(workspace),
                    "--data-dir",
                    str(data),
                    "serve",
                    "--port",
                    "0",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                assert process.stderr is not None
                stage = "waiting for listen address"
                while True:
                    chunk = await process.stderr.read(4096)
                    if not chunk:
                        await process.wait()
                        raise EOFError("stderr closed before announcing a listen address")
                    stderr = (stderr + chunk)[-8192:]
                    match = re.search(rb"(?m)^Open (http://\S+)\r?\n", stderr)
                    if match:
                        url = match[1].decode()
                        break
                stage = "fetching bootstrap token"
                async with client.get(url + "/api/bootstrap") as response:
                    response.raise_for_status()
                    payload = await response.json()
                bootstrap_token = payload.get("token") if isinstance(payload, dict) else None
                if not isinstance(bootstrap_token, str) or not bootstrap_token:
                    raise ValueError("bootstrap response did not contain a token")
                token = bootstrap_token
                headers = {"x-ngn-token": token, "Origin": url}
        except (OSError, EOFError, ValueError, aiohttp.ClientError) as error:
            exit_code = process.returncode if process is not None else None
            detail = stderr.decode(errors="replace").strip() or "<empty>"
            raise AssertionError(
                f"Queue HTTP host did not start ({phase}, {stage}, exit={exit_code}): "
                f"{type(error).__name__}: {error}\nHost stderr (tail):\n{detail}"
            ) from error

    async def stop(*, crash: bool = False) -> None:
        require(process is not None, "Queue host was not started")
        if process.returncode is None:
            process.send_signal(signal.SIGKILL if crash else signal.SIGTERM)
            await asyncio.wait_for(process.wait(), 15)
        if not crash:
            require(process.returncode == 0, "Queue HTTP host did not stop cleanly")

    async with aiohttp.ClientSession() as client:

        async def api(path: str, body: object = None, expected: int = 200) -> dict:
            async with client.request(
                "GET" if body is None else "POST",
                url + path,
                json=body,
                headers=headers,
            ) as response:
                payload = await response.json()
                require(response.status == expected, f"{path}: {response.status}: {payload}")
                return payload

        async def submit(session: str, prompt: str, request: str | None = None) -> dict:
            return (
                await api(
                    f"/api/sessions/{session}/runs",
                    {
                        "prompt": prompt,
                        "request_id": request or str(uuid.uuid4()),
                    },
                )
            )["run"]

        async def history(session: str) -> list[dict]:
            return (await api(f"/api/sessions/{session}/events"))["events"]

        async def terminal(session: str, run: str, expected: str) -> None:
            async with asyncio.timeout(20):
                while True:
                    records = await history(session)
                    finished = next(
                        (record for record in records if record["event"] == "run_finished" and record["run_id"] == run),
                        None,
                    )
                    if finished:
                        require(
                            finished["status"] == expected,
                            f"Unexpected queue outcome: {finished}",
                        )
                        return
                    await asyncio.sleep(0.03)

        try:
            await launch("initial launch")
            session = (await api("/api/sessions", {}))["session"]["id"]
            queue_path = f"/api/sessions/{session}/queue"
            async with client.ws_connect(
                url + f"/api/events?session_id={session}&after=0",
                protocols=["ngn.events.v1", "ngn.token." + token],
                headers={"Origin": url},
            ) as socket:
                await api(queue_path, {"action": "pause"})
                control = await asyncio.wait_for(socket.receive_json(), 5)
                require(
                    control["event"] == "queue_updated" and control["paused"] and control["run_id"] == "",
                    "Empty-session queue controls were not journaled/streamed",
                )
                request = str(uuid.uuid4())
                first = await submit(session, "read hello.txt first", request)
                duplicate = await submit(session, "read hello.txt first", request)
                require(
                    first["id"] == duplicate["id"] and first["status"] == "queued",
                    "Queued retries were not idempotent",
                )
                await api(
                    f"/api/sessions/{session}/runs",
                    {"prompt": "changed", "request_id": request},
                    409,
                )
                removed = await submit(session, "write a cancelled file")
                second = await submit(session, "read hello.txt second")
                third = await submit(session, "write a queued file")
                await api(f"/api/runs/{removed['id']}/cancel", {})
                queue = (await api(queue_path))["queue"]
                require(
                    [entry["run"]["id"] for entry in queue["entries"]] == [first["id"], second["id"], third["id"]],
                    "Queue removal changed FIFO order",
                )
                require(
                    not (workspace / "changed.txt").exists(),
                    "A queued write ran before dispatch",
                )
                await api(queue_path, {"action": "resume"})
                starts = []
                finishes = []
                approved = False
                while len(finishes) < 3:
                    record = await asyncio.wait_for(socket.receive_json(), 30)
                    require(
                        record["session_id"] == session,
                        "Queue stream crossed session boundaries",
                    )
                    if record["event"] == "run_started":
                        starts.append(record["run_id"])
                    elif record["event"] == "approval_requested":
                        require(
                            record["run_id"] == third["id"] and not (workspace / "changed.txt").exists(),
                            "Queued write reused an approval or ran before its own decision",
                        )
                        await api(
                            "/api/approvals",
                            {
                                "run_id": first["id"],
                                "approval_id": record["approval_id"],
                                "allow": True,
                            },
                            409,
                        )
                        await api(
                            "/api/approvals",
                            {
                                "run_id": third["id"],
                                "approval_id": record["approval_id"],
                                "allow": True,
                            },
                        )
                        approved = True
                    elif record["event"] == "run_finished" and record["run_id"] != removed["id"]:
                        require(
                            record["status"] == "completed",
                            f"Queued nagents run failed: {record}",
                        )
                        finishes.append(record["run_id"])
                require(
                    starts == finishes == [first["id"], second["id"], third["id"]],
                    "Live nagents did not execute exactly once in FIFO order",
                )
                require(
                    approved and (workspace / "changed.txt").exists(),
                    "Queued native write did not cross its own approval boundary",
                )

            root = await submit(session, "wait for cancellation")
            pending = await submit(session, "read hello.txt after stop")
            await api(f"/api/runs/{root['id']}/cancel", {})
            await terminal(session, root["id"], "cancelled")
            queue = (await api(queue_path))["queue"]
            require(
                queue["paused"] and queue["entries"][0]["run"]["id"] == pending["id"],
                "Stop started the next input instead of pausing",
            )
            await api(queue_path, {"action": "clear"})
            await terminal(session, pending["id"], "cancelled")
            require(
                (await submit(session, "read hello.txt after stop", pending["request_id"]))["status"] == "cancelled",
                "Cleared request was replayed",
            )
            other = (await api("/api/sessions", {}))["session"]["id"]
            async with client.ws_connect(
                url + f"/api/events?session_id={other}&after=0",
                protocols=["ngn.events.v1", "ngn.token." + token],
                headers={"Origin": url},
            ) as socket:
                await api(queue_path, {"action": "resume"})
                await api(f"/api/sessions/{other}/queue", {"action": "pause"})
                record = await asyncio.wait_for(socket.receive_json(), 5)
                require(
                    record["session_id"] == other,
                    "Session-scoped queue controls leaked to another subscription",
                )
            await stop()

            # The test runner exits on EOF, including when its host is SIGKILLed.
            await launch("crash fixture launch", crash_config)
            interrupted = await submit(session, CRASH_PROMPT)
            async with asyncio.timeout(10):
                while not any(
                    record["run_id"] == interrupted["id"]
                    and record["event"] == "notice"
                    and record["text"] == CRASH_NOTICE
                    for record in await history(session)
                ):
                    await asyncio.sleep(0.03)
            saved = await submit(session, "read hello.txt persisted")
            require(
                saved["status"] == "queued",
                "Crash fixture did not leave a pending input",
            )
            await stop(crash=True)
            await launch("restart after SIGKILL")
            queue = (await api(queue_path))["queue"]
            require(
                queue["paused"] and [entry["run"]["id"] for entry in queue["entries"]] == [saved["id"]],
                "Restart did not preserve unstarted inputs in a paused queue",
            )
            require(
                (await submit(session, CRASH_PROMPT, interrupted["request_id"]))["status"] == "interrupted",
                "Restart retried a claimed/interrupted run",
            )
            require(
                not any(
                    record["event"] == "run_started" and record["run_id"] == saved["id"]
                    for record in await history(session)
                ),
                "Restart automatically ran a pending input",
            )
            await api(queue_path, {"action": "resume"})
            await terminal(session, saved["id"], "completed")
            require(
                sum(
                    record["event"] == "run_started" and record["run_id"] == interrupted["id"]
                    for record in await history(session)
                )
                == 1,
                "Interrupted work was replayed",
            )
            require(
                sum(
                    record["event"] == "run_started" and record["run_id"] == saved["id"]
                    for record in await history(session)
                )
                == 1,
                "Recovered input did not run exactly once on explicit resume",
            )
            await stop()
        finally:
            if process is not None and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 15)
                except TimeoutError:
                    process.kill()
                    await process.wait()


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
    arguments = [str(binary), "--config", str(config), "-C", str(workspace), "--data-dir", str(data)]
    process = subprocess.Popen(arguments, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
    output = b""
    try:

        def visible() -> bytes:
            return terminal_text(output)

        def wait_for(predicate: Callable[[], bool], explanation: str) -> None:
            nonlocal output
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                if predicate():
                    return
                require(
                    process.poll() is None,
                    f"Queue TUI exited early: {output[-3000:]!r}",
                )
                if select.select([master], [], [], 0.03)[0]:
                    output += os.read(master, 65536)
            raise AssertionError(explanation + f": {visible()[-2500:]!r}")

        wait_for(lambda: b"Welcome to ngn" in visible(), "Queue TUI did not render")
        database = next(data.rglob("host.sqlite3"))

        def statuses() -> dict[str, str]:
            with sqlite3.connect(database) as connection:
                return dict(connection.execute("SELECT prompt, status FROM runs"))

        def paused() -> bool:
            with sqlite3.connect(database) as connection:
                return connection.execute("SELECT COALESCE(MAX(paused), 0) FROM session_queues").fetchone()[0] == 1

        os.write(master, b"/queue pause\r")
        wait_for(paused, "TUI pause did not persist")
        os.write(master, b"wait for cancellation\rremove this input\rqueued before resume\r")
        wait_for(lambda: len(statuses()) == 3, "TUI did not queue all three inputs")
        require(set(statuses().values()) == {"queued"}, "Paused TUI executed an input")
        os.write(master, b"\x1b[17~")  # F6 opens the input queue.
        wait_for(
            lambda: b"Input queue: PAUSED" in visible() and b"queued before resume" in visible(),
            "F6 did not inspect the paused FIFO",
        )
        os.write(master, b"\x1b[B\x18")  # Down, Ctrl+X: remove only the second input.
        wait_for(
            lambda: statuses().get("remove this input") == "cancelled",
            "TUI removed the wrong queued input",
        )
        os.write(master, b"\x10")  # Ctrl+P resumes.
        wait_for(
            lambda: statuses().get("wait for cancellation") == "running",
            "TUI resume did not start the FIFO head",
        )
        os.write(master, b"\x1b")
        wait_for(lambda: b"Input queue:" not in visible(), "Queue inspector did not close")
        os.write(master, b"queued while busy\r")
        wait_for(
            lambda: statuses().get("queued while busy") == "queued",
            "Busy TUI did not accept a pending input",
        )
        os.write(master, b"\x1b[17~")
        wait_for(
            lambda: b"queued while busy" in visible() and b"Input queue:" in visible(),
            "TUI did not refresh the pending FIFO",
        )
        os.write(master, b"\x0b")  # Ctrl+K clears pending, never the active run.
        wait_for(
            lambda: statuses().get("queued while busy") == "cancelled",
            "TUI clear did not cancel pending inputs",
        )
        require(statuses()["wait for cancellation"] == "running", "TUI clear cancelled active work")
        os.write(master, b"\x1b")
        wait_for(
            lambda: b"Input queue:" not in visible(),
            "Queue inspector did not close after clear",
        )
        os.write(master, b"\x1b")  # Esc stops the root and leaves the queue paused.
        wait_for(
            lambda: statuses().get("wait for cancellation") == "cancelled",
            "TUI Stop did not cancel the root",
        )
        os.write(master, b"read persisted input\r")
        wait_for(
            lambda: statuses().get("read persisted input") == "queued",
            "TUI did not preserve input after Stop",
        )
        with sqlite3.connect(database) as connection:
            session = connection.execute("SELECT id FROM sessions").fetchone()[0]
        os.write(master, b"\x11")
        process.wait(timeout=15)
        require(process.returncode == 0, "First queue TUI exit failed")
        mask = termios.ECHO | termios.ICANON | termios.ISIG
        require(
            termios.tcgetattr(slave)[3] & mask == before[3] & mask,
            "First queue TUI did not restore terminal mode",
        )
        output = b""
        process = subprocess.Popen(
            arguments + ["--resume", session],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
        )
        wait_for(lambda: b"F6 queue" in visible(), "Resumed queue TUI did not render")
        os.write(master, b"\x1b[17~")
        wait_for(
            lambda: b"Input queue: PAUSED" in visible() and b"read persisted input" in visible(),
            "TUI restart did not expose paused durable inputs",
        )
        require(
            statuses()["read persisted input"] == "queued",
            "TUI restart automatically replayed pending work",
        )
        os.write(master, b"\x10")
        wait_for(
            lambda: statuses().get("read persisted input") == "completed",
            "Recovered TUI input did not complete on explicit resume",
        )
        os.write(master, b"\x11")
        process.wait(timeout=15)
        require(process.returncode == 0, "Resumed queue TUI exit failed")
        require(
            termios.tcgetattr(slave)[3] & mask == before[3] & mask,
            "Resumed queue TUI did not restore terminal mode",
        )
        with sqlite3.connect(database) as connection:
            starts = [
                row[0]
                for row in connection.execute(
                    "SELECT r.prompt FROM events e JOIN runs r ON r.id = e.run_id WHERE json_extract(e.payload, '$.event') = 'run_started' ORDER BY e.sequence"
                )
            ]
        require(
            starts == ["wait for cancellation", "read persisted input"],
            f"TUI replayed or executed cancelled inputs: {starts}",
        )
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)
        os.close(slave)
