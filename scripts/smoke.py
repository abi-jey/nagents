"""Local end-to-end checks. Never sends requests to a paid provider.

Run from the repository root after `cargo build -p ngn`, in the environment
containing nagents + ngn_nagents: python scripts/smoke.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import signal
import sqlite3
import sys
import tempfile
import time
import uuid
from pathlib import Path

import aiohttp
from aiohttp import web

ROOT = Path(__file__).resolve().parent.parent


def require(condition: object, message: str) -> None:
    if not condition:
        raise AssertionError(message)


async def fixture(request: web.Request) -> web.StreamResponse:
    if request.method == "GET":
        return web.json_response({"data": [{"id": "fixture-model", "object": "model"}]})
    body = await request.json()
    messages = body["messages"]
    last = messages[-1]
    if last["role"] == "user" and "wait" in last.get("content", ""):
        await asyncio.sleep(30)
    response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
    await response.prepare(request)
    if last["role"] == "user":
        writing = "write" in last.get("content", "")
        name = "write" if writing else "read_file"
        arguments = (
            {
                "path": "changed.txt",
                "content": "written by an approved Rust host tool\n",
            }
            if writing
            else {"path": "hello.txt"}
        )
        delta = {
            "tool_calls": [
                {
                    "index": 0,
                    "id": f"call-{uuid.uuid4().hex}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ]
        }
        reason = "tool_calls"
    else:
        require(
            last["role"] == "tool", "Fixture expected an actual persisted tool result"
        )
        delta = {
            "content": "The tool result was returned through the Rust host boundary."
        }
        reason = "stop"
    for choice in [
        {"index": 0, "delta": delta, "finish_reason": None},
        {"index": 0, "delta": {}, "finish_reason": reason},
    ]:
        await response.write(
            (
                "data: "
                + json.dumps(
                    {
                        "id": "fixture-response",
                        "object": "chat.completion.chunk",
                        "model": "fixture-model",
                        "choices": [choice],
                    }
                )
                + "\n\n"
            ).encode()
        )
    await response.write(b"data: [DONE]\n\n")
    await response.write_eof()
    return response


async def cli(
    binary: Path, config: Path, workspace: Path, *arguments: str
) -> list[dict[str, object]]:
    process = await asyncio.create_subprocess_exec(
        str(binary),
        "--config",
        str(config),
        "-C",
        str(workspace),
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
    require(
        process.returncode == 0, f"CLI failed: {stderr.decode()}\n{stdout.decode()}"
    )
    records = [json.loads(line) for line in stdout.splitlines()]
    require(
        records[-1]["status"] == "completed",
        f"No successful terminal event: {records[-1]}",
    )
    return records


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
        require(process.stderr is not None, "No server startup stream")
        url = ""
        for _ in range(8):
            line = (await asyncio.wait_for(process.stderr.readline(), 10)).decode()
            match = re.search(r"Open (http://\S+)", line)
            if match:
                url = match[1]
                break
        require(url, "Server did not print its actual bound address")
        async with aiohttp.ClientSession() as client:
            async with client.get(url + "/api/bootstrap") as response:
                bootstrap = await response.json()
                require(response.status == 200, "Bootstrap failed")
            headers = {"x-ngn-token": bootstrap["token"], "Origin": url}

            async def api(
                path: str, body: object, expected: int = 200
            ) -> dict[str, object]:
                async with client.post(
                    url + path, json=body, headers=headers
                ) as response:
                    payload = await response.json()
                    require(
                        response.status == expected,
                        f"{path}: {response.status} {payload}",
                    )
                    return payload

            async with client.get(url + "/") as response:
                require(
                    response.status == 200 and "0.100.0" in await response.text(),
                    "Bundled client missing",
                )
            async with client.get(
                url + "/api/sessions", headers={"x-ngn-token": "invalid"}
            ) as response:
                require(response.status == 403, "Invalid token accepted")
            async with client.post(
                url + "/api/sessions",
                json={},
                headers={
                    "x-ngn-token": bootstrap["token"],
                    "Origin": "http://evil.example",
                },
            ) as response:
                require(response.status == 403, "Foreign Origin accepted")
            created = await api("/api/sessions", {})
            session = created["session"]["id"]
            request_id = str(uuid.uuid4())
            ws_headers = {"Origin": url}
            async with client.ws_connect(
                url + f"/api/events?session_id={session}&after=0",
                protocols=["ngn.events.v1", "ngn.token." + bootstrap["token"]],
                headers=ws_headers,
            ) as socket:
                accepted = await api(
                    f"/api/sessions/{session}/runs",
                    {"prompt": "write a file", "request_id": request_id},
                )
                run = accepted["run"]["id"]
                sequences = []
                approved = False
                while True:
                    message = await asyncio.wait_for(socket.receive_json(), 20)
                    sequences.append(message["sequence"])
                    if message["event"] == "approval_requested":
                        require(
                            not (workspace / "changed.txt").exists(),
                            "Write happened before approval",
                        )
                        await api(
                            "/api/approvals",
                            {
                                "run_id": "wrong-run",
                                "approval_id": message["approval_id"],
                                "allow": True,
                            },
                            409,
                        )
                        await api(
                            "/api/approvals",
                            {
                                "run_id": run,
                                "approval_id": message["approval_id"],
                                "allow": True,
                            },
                        )
                        await api(
                            "/api/approvals",
                            {
                                "run_id": run,
                                "approval_id": message["approval_id"],
                                "allow": True,
                            },
                            409,
                        )
                        approved = True
                    if message["event"] == "run_finished":
                        require(
                            message["status"] == "completed",
                            f"Web run failed: {message}",
                        )
                        break
                require(approved, "No native approval occurred")
                require(
                    sequences == sorted(set(sequences)),
                    "Events duplicated or reordered",
                )
                require(
                    (workspace / "changed.txt").read_text()
                    == "written by an approved Rust host tool\n",
                    "Approved native write missing",
                )
                repeated = await api(
                    f"/api/sessions/{session}/runs",
                    {"prompt": "write a file", "request_id": request_id},
                )
                require(
                    repeated["run"]["id"] == run,
                    "Submission retry spawned duplicate work",
                )
                await api(
                    f"/api/sessions/{session}/runs",
                    {"prompt": "different prompt", "request_id": request_id},
                    409,
                )
            # Reconnect from a durable cursor without losing terminal state.
            async with client.ws_connect(
                url + f"/api/events?session_id={session}&after={sequences[-2]}",
                protocols=["ngn.events.v1", "ngn.token." + bootstrap["token"]],
                headers=ws_headers,
            ) as socket:
                terminal = await asyncio.wait_for(socket.receive_json(), 5)
                require(
                    terminal["sequence"] == sequences[-1]
                    and terminal["event"] == "run_finished",
                    "Cursor reconnect lost the terminal event",
                )
            waiting = await api(
                f"/api/sessions/{session}/runs",
                {"prompt": "wait for cancellation", "request_id": str(uuid.uuid4())},
            )
            await api(f"/api/runs/{waiting['run']['id']}/cancel", {})
            async with client.ws_connect(
                url + f"/api/events?session_id={session}&after={sequences[-1]}",
                protocols=["ngn.events.v1", "ngn.token." + bootstrap["token"]],
                headers=ws_headers,
            ) as socket:
                while True:
                    message = await asyncio.wait_for(socket.receive_json(), 10)
                    if message["event"] == "run_finished":
                        require(
                            message["status"] == "cancelled",
                            "Cancellation did not stop the agent",
                        )
                        break
    finally:
        if process.returncode is None:
            process.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), 15)
            except TimeoutError:
                process.kill()
                await process.wait()
        require(process.returncode == 0, "HTTP host did not shut down cleanly")


def tui_check(
    binary: Path, workspace: Path, data: Path, config: Path | None = None
) -> None:
    import fcntl
    import pty
    import select
    import struct
    import subprocess
    import termios

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 36, 120, 0, 0))
    original_mode = termios.tcgetattr(slave)
    arguments = [str(binary), "-C", str(workspace), "--data-dir", str(data)]
    arguments += ["--config", str(config)] if config is not None else ["--demo"]
    process = subprocess.Popen(
        arguments,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
    )
    os.close(slave)
    try:
        output = b""

        def visible() -> bytes:
            # Ratatui optimizes spaces into cursor-position commands. This is a
            # real PTY, not a plain text pipe; normalize CSI boundaries for assertions.
            return b" ".join(re.sub(rb"\x1b\[[0-?]*[ -/]*[@-~]", b" ", output).split())

        deadline = time.monotonic() + 10
        while b"Welcome to ngn" not in visible() and time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                output += os.read(master, 65536)
        require(
            b"Welcome to ngn" in visible(), "Native TUI did not render on a real PTY"
        )
        os.write(
            master, b"write a file\r" if config is not None else b"demo approval\r"
        )
        deadline = time.monotonic() + 10
        while b"Approval:" not in visible() and time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                output += os.read(master, 65536)
        require(b"Approval:" in visible(), "TUI approval preview did not render")
        os.write(master, b"\x19")  # Ctrl+Y: explicit one-time approval.
        database = next(data.rglob("host.sqlite3"))
        completed = False
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.05)[0]:
                os.read(master, 65536)
            with sqlite3.connect(database) as connection:
                completed = (
                    connection.execute(
                        "SELECT COUNT(*) FROM runs WHERE status = 'completed'"
                    ).fetchone()[0]
                    == 1
                )
            if completed:
                break
        require(completed, "TUI demo did not complete after approving")
        if config is not None:
            require(
                (workspace / "changed.txt").read_text()
                == "written by an approved Rust host tool\n",
                "Live TUI did not execute its approved native Rust tool",
            )
        if config is None:
            os.write(master, b"\x11")  # Ctrl+Q: cleanup and terminal restoration.
        else:
            process.send_signal(
                signal.SIGTERM
            )  # External shutdown must restore terminal state too.
        deadline = time.monotonic() + 10
        while process.poll() is None and time.monotonic() < deadline:
            if select.select([master], [], [], 0.05)[0]:
                try:
                    os.read(master, 65536)
                except OSError:
                    break
        process.wait(timeout=2)
        require(process.returncode == 0, "TUI did not exit cleanly")
        restored_mode = termios.tcgetattr(master)
        mask = termios.ICANON | termios.ECHO | termios.ISIG
        require(
            restored_mode[3] & mask == original_mode[3] & mask,
            "TUI did not restore the terminal mode",
        )
        with sqlite3.connect(database) as connection:
            payloads = [
                json.loads(row[0])
                for row in connection.execute("SELECT payload FROM events")
            ]
        require(
            any(
                event.get("event") == "approval_resolved" and event.get("allowed")
                for event in payloads
            ),
            "TUI did not record its bound approval",
        )
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=ROOT / "target/debug/ngn")
    parser.add_argument("--skip-tui", action="store_true")
    arguments = parser.parse_args()
    binary = arguments.binary.resolve()
    require(binary.exists(), "Build the binary first: cargo build -p ngn")
    app = web.Application()
    app.router.add_route("*", "/v1/{tail:.*}", fixture)
    runner = web.AppRunner(app, shutdown_timeout=0.1, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    os.environ["NGN_FIXTURE_KEY"] = "fixture-only-not-a-real-credential"
    try:
        with tempfile.TemporaryDirectory(prefix="ngn-smoke-") as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "hello.txt").write_text("hello from a Rust tool\n")
            config = root / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "data_dir": str(root / "data"),
                        "runner": [sys.executable, "-m", "ngn_nagents"],
                        "provider": "fixture",
                        "providers": {
                            "fixture": {
                                "kind": "openai_compatible",
                                "model": "fixture-model",
                                "api": "chat_completions",
                                "base_url": f"http://127.0.0.1:{port}/v1",
                                "api_key_env": "NGN_FIXTURE_KEY",
                            }
                        },
                    }
                )
            )
            records = await cli(
                binary, config, workspace, "run", "--json", "read hello.txt"
            )
            require(
                any(
                    record.get("event") == "tool_completed"
                    and record["result"]["result"].get("content")
                    == "1: hello from a Rust tool"
                    for record in records
                ),
                "nagents did not receive a native Rust file result",
            )
            session = records[0]["session_id"]
            await cli(
                binary,
                config,
                workspace,
                "--resume",
                session,
                "run",
                "--json",
                "read hello.txt again",
            )
            denied = await cli(
                binary, config, workspace, "run", "--json", "write a file"
            )
            require(
                any(
                    record.get("event") == "tool_completed"
                    and "non-interactive" in record["result"]["error"]
                    for record in denied
                ),
                "Headless write was not denied",
            )
            require(
                not (workspace / "changed.txt").exists(),
                "Headless mode wrote without approval",
            )
            print(
                "PASS: real nagents Agent.run → protocol → native Rust tools → resume; headless approvals fail closed"
            )
            await http_checks(binary, config, workspace)
            print(
                "PASS: HTTP security, authenticated WebSocket events/replay, approval binding, native writes, idempotency and cancellation"
            )
            if not arguments.skip_tui:
                await asyncio.to_thread(tui_check, binary, workspace, root / "tui-data")
                print("PASS: real-PTY Rust TUI, native approval and clean exit")
                live_workspace = root / "tui-live-workspace"
                live_workspace.mkdir()
                (live_workspace / "hello.txt").write_text("hello from a Rust tool\n")
                await asyncio.to_thread(
                    tui_check, binary, live_workspace, root / "tui-live-data", config
                )
                print(
                    "PASS: real-PTY TUI → nagents Agent.run → approved native Rust write; terminal mode restored"
                )
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
