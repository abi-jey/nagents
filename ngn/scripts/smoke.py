"""Local end-to-end checks. Never sends requests to a paid provider.

Run from the repository root after `cargo build -p ngn`, in the environment
containing nagents + ngn_nagents: python scripts/smoke.py
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import re
import signal
import sqlite3
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import aiohttp
from aiohttp import web
from protocol_fixture import write_config as write_protocol_fixture_config
from pty_smoke import COMPOSER_TITLE, WorkspaceReady

ROOT = Path(__file__).resolve().parent.parent
TOOL_REPLY = "The tool result was returned through the Rust host boundary."
TUI_WRITE_PROMPT = "write a file for mouse selection"
TUI_REPLY = (
    f"{TOOL_REPLY} Select this café sample, then keep the next few words connected in one sentence "
    "without copying margins or neighboring text."
)
OSC52_PREFIX = b"\x1b]52;"
OSC52_CLIPBOARD = re.compile(rb"\x1b\]52;c;([^\x07\x1b]*)\x1b\\")


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

    def tool_delta(calls: list[tuple[str, dict[str, object]]]) -> dict[str, object]:
        return {
            "tool_calls": [
                {
                    "index": index,
                    "id": f"call-{uuid.uuid4().hex}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
                for index, (name, arguments) in enumerate(calls)
            ]
        }

    def tool_name(message: dict[str, object]) -> str:
        if message.get("name"):
            return message["name"]
        for prior in reversed(messages):
            for call in prior.get("tool_calls", []):
                if call["id"] == message.get("tool_call_id"):
                    return call["function"]["name"]
        return ""

    if last["role"] == "user":
        prompt = last.get("content", "")
        if prompt == "delegate two background reads":
            delta = tool_delta(
                [
                    (
                        "to_agent",
                        {
                            "prompt": "read hello.txt",
                            "title": f"Background reader {index + 1}",
                            "read_only": True,
                            "background": True,
                        },
                    )
                    for index in range(2)
                ]
            )
        elif prompt.startswith("delegate "):
            holding = "hold" in prompt
            delta = tool_delta(
                [
                    (
                        "to_agent",
                        {
                            "prompt": "wait for cancellation" if holding else "write a delegated file",
                            "title": "Waiting delegate" if holding else "Delegate writer",
                            "read_only": "read-only" in prompt,
                            "background": "orphan" in prompt,
                        },
                    )
                ]
            )
        else:
            writing = "write" in prompt
            name = "write" if writing else "read_file"
            arguments = (
                {
                    "path": "delegated.txt" if "delegated" in prompt else "changed.txt",
                    "content": "written by an approved Rust host tool\n",
                }
                if writing
                else {"path": "hello.txt"}
            )
            delta = tool_delta([(name, arguments)])
        reason = "tool_calls"
    else:
        require(last["role"] == "tool", "Fixture expected an actual persisted tool result")
        user_prompt = next(message.get("content", "") for message in reversed(messages) if message["role"] == "user")
        if user_prompt == "delegate two background reads" and tool_name(last) == "to_agent":
            tool_results = []
            for message in reversed(messages):
                if message["role"] == "user":
                    break
                if message["role"] == "tool" and tool_name(message) == "to_agent":
                    tool_results.append(json.loads(message["content"]))
            require(len(tool_results) == 2, "Expected two distinct Rust-owned delegations")
            delta = tool_delta(
                [
                    (
                        "agent_wait",
                        {"run_id": result["task"]["run_id"], "timeout_seconds": 30},
                    )
                    for result in tool_results
                ]
            )
            reason = "tool_calls"
        else:
            delta = {"content": TUI_REPLY if user_prompt == TUI_WRITE_PROMPT else TOOL_REPLY}
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


async def cli(binary: Path, config: Path, workspace: Path, *arguments: str) -> list[dict[str, object]]:
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
    require(process.returncode == 0, f"CLI failed: {stderr.decode()}\n{stdout.decode()}")
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

            async def api(path: str, body: object, expected: int = 200) -> dict[str, object]:
                async with client.post(url + path, json=body, headers=headers) as response:
                    payload = await response.json()
                    require(
                        response.status == expected,
                        f"{path}: {response.status} {payload}",
                    )
                    return payload

            async with client.get(url + "/") as response:
                require(
                    response.status == 200 and bootstrap["version"] in await response.text(),
                    "Bundled client missing",
                )
            async with client.get(url + "/api/sessions", headers={"x-ngn-token": "invalid"}) as response:
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
                    (workspace / "changed.txt").read_text() == "written by an approved Rust host tool\n",
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
                    terminal["sequence"] == sequences[-1] and terminal["event"] == "run_finished",
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
    binary: Path, config: Path, workspace: Path, data: Path, *, external_shutdown: bool, quit_key: bytes = b"\x11"
) -> None:
    import fcntl
    import pty
    import select
    import struct
    import subprocess
    import termios

    from subagent_smoke import terminal_rows, terminal_text

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 36, 120, 0, 0))
    original_mode = termios.tcgetattr(slave)
    arguments = [str(binary), "--config", str(config), "-C", str(workspace), "--data-dir", str(data)]
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
        ready = WorkspaceReady(master)

        def visible() -> bytes:
            return terminal_text(output, ready.rows, ready.columns)

        def read_pty(timeout: float) -> None:
            nonlocal output
            if select.select([master], [], [], timeout)[0]:
                output += os.read(master, 65536)

        def wait_for(predicate: Callable[[], bool], message: str, *, timeout: float = 10) -> None:
            deadline = time.monotonic() + timeout
            while not predicate():
                remaining = deadline - time.monotonic()
                require(remaining > 0, message)
                read_pty(min(remaining, 0.05))

        wait_for(
            lambda: ready.observe(output, COMPOSER_TITLE in visible()),
            "Native TUI did not render and repaint at the resized PTY dimensions",
        )
        require(b"\x1b[?1003h\x1b[?1006h" in output, "Plain TUI launch did not enable mouse reporting")
        destination = workspace / "changed.txt"
        require(not destination.exists(), "Approval fixture destination already exists")
        os.write(master, TUI_WRITE_PROMPT.encode() + b"\r")
        wait_for(lambda: b"Allow write" in visible(), "TUI approval preview did not render")
        require(not destination.exists(), "TUI wrote before approval")
        os.write(master, b"\x19")  # Ctrl+Y: explicit one-time approval.
        database = next(data.rglob("host.sqlite3"))

        def completed() -> bool:
            with sqlite3.connect(database) as connection:
                return connection.execute("SELECT COUNT(*) FROM runs WHERE status = 'completed'").fetchone()[0] == 1

        def settled() -> bool:
            pane = visible().decode()
            return (
                TOOL_REPLY in pane
                and "without copying margins" in pane
                and bool(re.search(r"\bassistant(?: · [0-9:]+)? fixture · fixture-model\b", pane))
            )

        wait_for(completed, "TUI fixture run did not complete after approving")
        wait_for(settled, "TUI did not paint the final reply and settled footer")
        pane = visible().decode()
        require("✓ Done" not in pane, "TUI showed a redundant successful turn marker")
        require(
            "write · changed.txt" in pane and "changed.txt · complete" not in pane, "TUI lost the completed tool badge"
        )
        require(
            destination.read_text() == "written by an approved Rust host tool\n",
            "TUI did not execute its approved native Rust tool",
        )
        # Use painted terminal coordinates and real SGR mouse reports,
        # rather than invoking the tool-card handler directly.
        card = " write · changed.txt"

        def disclosed(marker: str) -> bool:
            rows = terminal_rows(output, ready.rows, ready.columns)
            has_details = any(line.lstrip().startswith("Arguments:") for line in rows)
            return any(line.startswith(marker + card) for line in rows) and has_details == (marker == "▾")

        def locate(text: str) -> tuple[int, int]:
            for row, line in enumerate(terminal_rows(output, ready.rows, ready.columns), 1):
                if (column := line.find(text)) >= 0:
                    return column + 1, row
            raise AssertionError(f"TUI selection fixture text was not visible: {text!r}")

        def mouse(button: int, cell: tuple[int, int], *, release: bool = False) -> bytes:
            column, row = cell
            return f"\x1b[<{button};{column};{row}{'m' if release else 'M'}".encode()

        marker = locate("▸" + card)
        os.write(master, mouse(0, marker) + mouse(0, marker, release=True))
        wait_for(lambda: disclosed("▾"), "Mouse click did not expand the tool card", timeout=5)
        column, row = locate("▾" + card)
        letter = (column + 2, row)  # The "w" after the marker.
        os.write(master, mouse(0, letter) + mouse(32, letter) + mouse(0, letter, release=True))
        wait_for(lambda: disclosed("▸"), "Same-cell motion did not preserve the collapse click", timeout=5)

        def drag_and_copy(
            start: tuple[int, int],
            via: tuple[int, int],
            end: tuple[int, int],
            expected: bytes,
            label: str,
        ) -> None:
            start_at = len(output)
            # The drag stops short of the release position. A premature copy
            # would contain different bytes. OSC 52 stays in this private PTY.
            reports = mouse(0, start) + mouse(32, via) + mouse(0, end, release=True)
            os.write(master, reports + mouse(35, (ready.columns, ready.rows)))
            wait_for(
                lambda: OSC52_CLIPBOARD.search(output, start_at) is not None,
                f"Releasing the {label} drag did not emit a complete OSC 52 clipboard request",
                timeout=5,
            )
            match = OSC52_CLIPBOARD.search(output, start_at)
            assert match is not None
            require(base64.b64decode(match[1], validate=True) == expected, f"Release copied incorrect {label} bytes")
            wait_for(lambda: b"Copy requested" in visible(), "TUI did not paint clipboard request feedback", timeout=5)

        column, row = locate("▸" + card)
        # Skip the marker ("▸ ") to start on "w".
        drag_and_copy((column + 2, row), (column + 3, row), (column + 5, row), b"writ", "tool label")
        require(disclosed("▸"), "Dragging across the tool label disclosed its card")

        # Both endpoints are inside words on adjacent rows of a single logical
        # line; copying must neither add a newline nor include pane padding.
        first, last = "afé", "witho"
        start = locate(first)
        column, row = locate(last)
        require(row == start[1] + 1, "Selection fixture did not soft-wrap between the drag endpoints")
        expected = TUI_REPLY[TUI_REPLY.index(first) : TUI_REPLY.index(last) + len(last)].encode()
        drag_and_copy(
            start,
            (start[0] + 1, start[1]),
            (column + len(last) - 1, row),
            expected,
            "soft-wrapped prose",
        )

        if external_shutdown:
            process.send_signal(signal.SIGTERM)  # External shutdown must restore terminal state too.
        else:
            os.write(master, quit_key)  # Ctrl+Q or Ctrl+C must quit even with a selection.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.05)[0]:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                output += chunk
            elif process.poll() is not None:
                break
        process.wait(timeout=2)
        require(process.returncode == 0, "TUI did not exit cleanly")
        require(output.count(OSC52_PREFIX) == 2, "TUI emitted an unexpected extra OSC 52 request")
        require(b"\x1b[?1006l\x1b[?1003l" in output, "TUI did not disable mouse reporting on exit")
        restored_mode = termios.tcgetattr(master)
        mask = termios.ICANON | termios.ECHO | termios.ISIG
        require(
            restored_mode[3] & mask == original_mode[3] & mask,
            "TUI did not restore the terminal mode",
        )
        with sqlite3.connect(database) as connection:
            payloads = [json.loads(row[0]) for row in connection.execute("SELECT payload FROM events")]
        require(
            any(event.get("event") == "approval_resolved" and event.get("allowed") for event in payloads),
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
    parser.add_argument(
        "--bundled-runtime", action="store_true", help="Omit runner so the installed harness selects its own Python"
    )
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
            settings: dict[str, object] = {
                "data_dir": str(root / "data"),
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
            if not arguments.bundled_runtime:
                settings["runner"] = [sys.executable, "-m", "ngn_nagents"]
            config.write_text(json.dumps(settings))
            records = await cli(binary, config, workspace, "run", "--json", "read hello.txt")
            require(
                any(
                    record.get("event") == "tool_completed"
                    and record["result"]["result"].get("content") == "1: hello from a Rust tool"
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
            denied = await cli(binary, config, workspace, "run", "--json", "write a file")
            require(
                any(
                    record.get("event") == "tool_completed" and "non-interactive" in record["result"]["error"]
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
            from subagent_smoke import http_checks as subagent_http_checks
            from subagent_smoke import tui_check as subagent_tui_check

            subagent_workspace = root / "subagent-workspace"
            subagent_workspace.mkdir()
            (subagent_workspace / "hello.txt").write_text("hello from a Rust tool\n")
            await subagent_http_checks(binary, config, subagent_workspace)
            print(
                "PASS: real nagents subagents, native child approvals, permission ceilings, background result delivery, WS isolation/replay and subtree cancellation"
            )
            from queue_smoke import http_checks as queue_http_checks
            from queue_smoke import tui_check as queue_tui_check

            queue_workspace = root / "queue-workspace"
            queue_workspace.mkdir()
            (queue_workspace / "hello.txt").write_text("hello from a Rust tool\n")
            crash_config = root / "crash-config.json"
            write_protocol_fixture_config(crash_config)
            await queue_http_checks(binary, config, queue_workspace, root / "queue-data", crash_config)
            print(
                "PASS: real nagents FIFO, queued idempotency/removal, per-run approvals, session-scoped WS controls, "
                "Stop/clear, protocol-runner crash recovery and explicit resume without replay"
            )
            if not arguments.skip_tui:
                for label, external_shutdown, quit_key in (
                    ("ctrl-q", False, b"\x11"),
                    ("ctrl-c", False, b"\x03"),
                    ("sigterm", True, b""),
                ):
                    tui_workspace = root / f"tui-{label}"
                    tui_workspace.mkdir()
                    await asyncio.to_thread(
                        tui_check,
                        binary,
                        config,
                        tui_workspace,
                        root / f"tui-{label}-data",
                        external_shutdown=external_shutdown,
                        quit_key=quit_key,
                    )
                print(
                    "PASS: real-PTY TUI approved native Rust write, mouse tool disclosure, selection-release OSC 52 copy "
                    "and Ctrl+Q/Ctrl+C/SIGTERM restoration"
                )
                await asyncio.to_thread(
                    subagent_tui_check,
                    binary,
                    config,
                    subagent_workspace,
                    root / "subagent-tui-data",
                )
                print(
                    "PASS: real-PTY subagent inspection, child-only cancel, parent continuity, clean exit and terminal restoration"
                )
                await asyncio.to_thread(queue_tui_check, binary, config, queue_workspace, root / "queue-tui-data")
                print(
                    "PASS: real-PTY busy submission, F6 FIFO inspection, remove/pause/resume/clear, Stop, durable restart and terminal restoration"
                )
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
