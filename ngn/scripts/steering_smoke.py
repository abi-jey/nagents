"""Offline real HTTP/CLI boundary acceptance; only a loopback fixture is contacted.

Use an explicit venv and private TMPDIR under /tmp (not group-writable
/tmp/opencode), then pass --binary pointing at a DEBUG build. This script uses
this checkout's Python source runtime explicitly. TUI/browser steering controls
are deliberately not implemented or claimed here.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import signal
import sys
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from uuid import uuid4

import aiohttp
from aiohttp import web

REPO = Path(__file__).resolve().parents[2]


def require(condition: object, message: str) -> None:
    if not condition:
        raise AssertionError(message)


class Fixture:
    def __init__(self) -> None:
        self.final_started = asyncio.Event()
        self.release_final = asyncio.Event()
        self.cancel_started = asyncio.Event()
        self.release_cancel = asyncio.Event()
        self.batch_checked = False
        self.final_checked = False

    async def handle(self, request: web.Request) -> web.StreamResponse:
        if request.method == "GET":
            return web.json_response({"data": [{"id": "steering-fixture", "object": "model"}]})
        body = await request.json()
        messages = body["messages"]
        last = messages[-1]
        prompt = last.get("content", "")
        if prompt == "batch start":
            calls = [
                ("read_file", {"path": "hello.txt"}),
                ("write", {"path": "second.txt", "content": "approved second tool\n"}),
            ]
            delta = {
                "tool_calls": [
                    {
                        "index": index,
                        "id": f"call-{uuid4().hex}",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(arguments)},
                    }
                    for index, (name, arguments) in enumerate(calls)
                ]
            }
            reason = "tool_calls"
        else:
            if prompt == "second steering":
                require(
                    [message["role"] for message in messages if message["role"] != "system"]
                    == ["user", "assistant", "tool", "tool", "user", "user"],
                    "Steering was inserted before the whole two-tool batch",
                )
                require(
                    [message["content"] for message in messages[-2:]] == ["first steering", "second steering"],
                    "Steering FIFO/payload changed",
                )
                self.batch_checked = True
            elif prompt == "final steering":
                require(
                    [message["role"] for message in messages if message["role"] != "system"]
                    == ["user", "assistant", "user"],
                    "Final continuation duplicated or omitted a persisted message",
                )
                self.final_checked = True
            elif prompt == "cancel hold":
                self.cancel_started.set()
                await self.release_cancel.wait()
                if request.transport is None or request.transport.is_closing():
                    raise asyncio.CancelledError
            delta, reason = {"content": "persisted local fixture final"}, "stop"
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        if prompt == "final hold":
            draft = {
                "id": "fixture",
                "model": "steering-fixture",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {"content": "draft streaming "}, "finish_reason": None}],
            }
            await response.write(("data: " + json.dumps(draft) + "\n\n").encode())
            self.final_started.set()
            await self.release_final.wait()
        for part in [
            {"index": 0, "delta": delta, "finish_reason": None},
            {"index": 0, "delta": {}, "finish_reason": reason},
        ]:
            await response.write(
                (
                    "data: "
                    + json.dumps(
                        {
                            "id": "fixture",
                            "model": "steering-fixture",
                            "object": "chat.completion.chunk",
                            "choices": [part],
                        }
                    )
                    + "\n\n"
                ).encode()
            )
        await response.write(b"data: [DONE]\n\n")
        await response.write_eof()
        return response


@asynccontextmanager
async def server(
    binary: Path, config: Path, workspace: Path, expected_exit: int = 0
) -> AsyncIterator[tuple[str, asyncio.subprocess.Process]]:
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
        require(process.stderr is not None, "No startup stream")
        assert process.stderr is not None
        for _ in range(8):
            line = (await asyncio.wait_for(process.stderr.readline(), 10)).decode()
            match = re.search(r"Open (http://\S+)", line)
            if match:
                yield match[1], process
                break
        else:
            raise AssertionError("No loopback address printed")
    finally:
        if process.returncode is None:
            process.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(process.wait(), 15)
            except TimeoutError:
                process.kill()
                await process.wait()
        require(process.returncode == expected_exit, "Unexpected host exit status")


async def http_checks(binary: Path, config: Path, workspace: Path, fixture: Fixture) -> None:
    saved: dict[str, object] = {}
    async with aiohttp.ClientSession() as client:
        async with server(binary, config, workspace) as (url, _):
            async with client.get(url + "/api/bootstrap") as response:
                bootstrap = await response.json()
            headers = {"x-ngn-token": bootstrap["token"], "Origin": url}

            async def api(path: str, body: object = None, expected: int = 200) -> dict[str, object]:
                async with client.request(
                    "GET" if body is None else "POST", url + path, json=body, headers=headers
                ) as response:
                    payload = await response.json()
                    require(response.status == expected, f"Unexpected HTTP {response.status} on {path}: {payload}")
                    return payload

            async def start(prompt: str) -> tuple[str, str]:
                session = (await api("/api/sessions", {}))["session"]["id"]
                run = (await api(f"/api/sessions/{session}/runs", {"prompt": prompt, "request_id": str(uuid4())}))[
                    "run"
                ]["id"]
                return session, run

            async def terminal(session: str, run: str, status: str = "completed") -> None:
                async with asyncio.timeout(20):
                    while True:
                        events = (await api(f"/api/sessions/{session}/events"))["events"]
                        ended = next(
                            (event for event in events if event["run_id"] == run and event["event"] == "run_finished"),
                            None,
                        )
                        if ended:
                            require(ended["status"] == status, f"Unexpected run result: {ended}")
                            return
                        await asyncio.sleep(0.01)

            session, run = await start("batch start")
            async with asyncio.timeout(15):
                while True:
                    approvals = (await api("/api/state"))["approvals"]
                    if approvals:
                        break
                    await asyncio.sleep(0.01)
            require(not (workspace / "second.txt").exists(), "Second tool ran before approval")
            path = f"/api/runs/{run}/steer"
            inputs = [
                {"prompt": prompt, "request_id": str(uuid4())} for prompt in ["first steering", "second steering"]
            ]
            receipts = [(await api(path, item))["receipt"] for item in inputs]
            require((await api(path, inputs[0]))["receipt"] == receipts[0], "Retry changed the pending receipt")
            await api(path, {**inputs[0], "prompt": "different"}, 409)
            await api("/api/runs/foreign-run/steer", inputs[0], 409)
            state = (await api(path))["steering"]
            require(
                [item["status"] for item in state["receipts"]] == ["pending", "pending"],
                "Input offered before last tool result",
            )
            require(
                (await api(f"/api/sessions/{session}/queue"))["queue"]["entries"] == [],
                "Steering silently became a next-run FIFO input",
            )
            await api("/api/approvals", {"run_id": run, "approval_id": approvals[0]["id"], "allow": True})
            await terminal(session, run)
            state = (await api(path))["steering"]
            require(
                [item["status"] for item in state["receipts"]] == ["applied", "applied"],
                "Persistence acknowledgments not durable",
            )
            require(
                [item["input_id"] for item in state["receipts"]] == [item["input_id"] for item in receipts],
                "Receipt attribution/order changed",
            )
            require(fixture.batch_checked, "Model did not see the persisted full batch followed by both user inputs")
            await api(path, {"prompt": "late", "request_id": str(uuid4())}, 409)
            saved = {"path": path, "input": inputs[0], "id": receipts[0]["input_id"]}

            session, run = await start("final hold")
            await asyncio.wait_for(fixture.final_started.wait(), 10)
            path = f"/api/runs/{run}/steer"
            incoming = {"prompt": "final steering", "request_id": str(uuid4())}
            await api(path, incoming)
            require((await api(path))["steering"]["receipts"][0]["status"] == "pending", "Injected during streaming")
            fixture.release_final.set()
            await terminal(session, run)
            require(fixture.final_checked, "No explicit Agent.run continuation after a no-tools final")
            require(
                (await api(path))["steering"]["receipts"][0]["status"] == "applied",
                "Final continuation was not acknowledged",
            )

            session, run = await start("cancel hold")
            await asyncio.wait_for(fixture.cancel_started.wait(), 10)
            path = f"/api/runs/{run}/steer"
            await api(path, {"prompt": "never delivered", "request_id": str(uuid4())})
            await api(f"/api/runs/{run}/cancel", {})
            await terminal(session, run, "cancelled")
            require(
                (await api(path))["steering"]["receipts"][0]["status"] == "undelivered",
                "Cancellation did not settle pending input",
            )
            await api(path, {"prompt": "after stop", "request_id": str(uuid4())}, 409)

        async with server(binary, config, workspace) as (url, _):
            async with client.get(url + "/api/bootstrap") as response:
                bootstrap = await response.json()
            async with client.post(
                url + str(saved["path"]),
                json=saved["input"],
                headers={"x-ngn-token": bootstrap["token"], "Origin": url},
            ) as response:
                payload = await response.json()
                require(
                    response.status == 200
                    and payload["receipt"]["input_id"] == saved["id"]
                    and payload["receipt"]["status"] == "applied",
                    "Idempotent receipt was lost/replayed across restart",
                )


def runtime_fixture(mode: str) -> None:
    """Deliberately incomplete trusted runtime, exclusively for failure acceptance."""

    def emit(run: str, message: dict[str, object]) -> None:
        print(json.dumps({"protocol_version": 1, "run_id": run, "message": message}), flush=True)

    capabilities = ["host_tools", "cancel"] + ([] if mode == "legacy" else ["safe_steering"])
    emit("", {"type": "ready", "runtime": "offline-protocol-fixture", "capabilities": capabilities})
    start = json.loads(sys.stdin.readline())
    run = start["run_id"]
    emit(
        run,
        {
            "type": "tool_request",
            "call": {
                "call_id": "once-only",
                "name": "write",
                "arguments": {"path": "once.txt", "content": "only one admitted tool execution\n"},
            },
        },
    )
    result = sys.stdin.readline()
    if not result or json.loads(result)["message"]["type"] == "cancel":
        return
    if mode == "legacy":
        emit(run, {"type": "finished", "status": "completed", "error": ""})
        return
    boundary = str(uuid4())
    emit(run, {"type": "steering_boundary", "boundary_id": boundary, "kind": "final"})
    offered = json.loads(sys.stdin.readline())["message"]
    require(offered["input"] is not None, "Fixture requires an actual offered receipt")
    if mode == "wrong-ack":
        emit(run, {"type": "steering_applied", "boundary_id": boundary, "input_id": "forged-not-the-offer"})
    # No persistence and no acknowledgment. EOF also exits the fixture after a
    # SIGKILL of its host, avoiding an intentionally orphaned waiting process.
    sys.stdin.readline()


async def protocol_api(
    client: aiohttp.ClientSession,
    url: str,
    headers: dict[str, str],
    path: str,
    body: object = None,
    expected: int = 200,
) -> dict[str, object]:
    async with client.request("GET" if body is None else "POST", url + path, json=body, headers=headers) as response:
        payload = await response.json()
        require(response.status == expected, f"Protocol fixture HTTP mismatch: {path} {response.status}")
        return payload


async def protocol_checks(binary: Path, base: Path, root: Path) -> None:
    for mode in ["legacy", "wrong-ack", "cancel", "crash"]:
        workspace = root / mode
        workspace.mkdir()
        config = root / f"{mode}.json"
        settings = json.loads(base.read_text())
        settings.update(
            {
                "data_dir": str(root / f"{mode}-state"),
                "runner": [sys.executable, str(Path(__file__).resolve()), "--runtime-fixture", mode],
            }
        )
        config.write_text(json.dumps(settings))
        async with aiohttp.ClientSession() as client:
            async with server(binary, config, workspace, -signal.SIGKILL if mode == "crash" else 0) as (url, process):
                async with client.get(url + "/api/bootstrap") as response:
                    bootstrap = await response.json()
                headers = {"x-ngn-token": bootstrap["token"], "Origin": url}

                api = partial(protocol_api, client, url, headers)

                session = (await api("/api/sessions", {}))["session"]["id"]
                run = (
                    await api(
                        f"/api/sessions/{session}/runs",
                        {"prompt": "offline protocol fixture", "request_id": str(uuid4())},
                    )
                )["run"]["id"]
                async with asyncio.timeout(10):
                    while True:
                        approvals = (await api("/api/state"))["approvals"]
                        if approvals:
                            break
                        await asyncio.sleep(0.01)
                path = f"/api/runs/{run}/steer"
                incoming = {"prompt": "never auto replay", "request_id": str(uuid4())}
                if mode == "legacy":
                    await api(path, incoming, 409)
                    require(not (await api(path))["steering"]["receipts"], "Baseline v1 runtime admitted steering")
                else:
                    first = (await api(path, incoming))["receipt"]
                    await api(path, {"prompt": "never offered", "request_id": str(uuid4())})
                await api(
                    "/api/approvals", {"run_id": run, "approval_id": approvals[0]["id"], "allow": mode != "legacy"}
                )
                async with asyncio.timeout(10):
                    while True:
                        state = (await api(path))["steering"]
                        if mode == "crash" and state["receipts"][0]["status"] == "offered":
                            process.kill()
                            await process.wait()
                            break
                        if mode == "cancel" and state["receipts"][0]["status"] == "offered":
                            await api(f"/api/runs/{run}/cancel", {})
                        if mode != "crash" and state["run_status"] != "running":
                            require(
                                state["run_status"]
                                == {"legacy": "completed", "wrong-ack": "failed", "cancel": "cancelled"}[mode],
                                "Legacy v1 compatibility or wrong-ack rejection failed",
                            )
                            break
                        await asyncio.sleep(0.01)
            if mode != "legacy":
                async with server(binary, config, workspace) as (url, _):
                    async with client.get(url + "/api/bootstrap") as response:
                        bootstrap = await response.json()
                    headers = {"x-ngn-token": bootstrap["token"], "Origin": url}
                    api = partial(protocol_api, client, url, headers)
                    state = (await api(path))["steering"]
                    require(
                        [item["status"] for item in state["receipts"]] == ["uncertain", "undelivered"],
                        "Failure/crash incorrectly claimed persistence or lost pending input",
                    )
                    repeated = (await api(path, incoming))["receipt"]
                    require(
                        repeated["input_id"] == first["input_id"] and repeated["status"] == "uncertain",
                        "Restart retry redelivered the uncertain offer",
                    )
                    require((await api("/api/state"))["runs"] == [], "Host restart replayed an activation")
                    events = (await api(f"/api/sessions/{session}/events"))["events"]
                    require(sum(event["event"] == "tool_started" for event in events) == 1, "Tool work was replayed")
                    require(
                        (workspace / "once.txt").read_text() == "only one admitted tool execution\n",
                        "Expected completed native tool result missing",
                    )


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    arguments = parser.parse_args()
    binary = arguments.binary.resolve()
    fixture = Fixture()
    app = web.Application()
    app.router.add_route("*", "/v1/{tail:.*}", fixture.handle)
    runner = web.AppRunner(app, shutdown_timeout=0.1, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    os.environ["NGN_STEERING_FIXTURE_KEY"] = "local-fixture-only"
    os.environ["PYTHONPATH"] = f"{REPO / 'src'}:{REPO / 'ngn/integrations/nagents/src'}"
    try:
        with tempfile.TemporaryDirectory(prefix="ngn-steering-smoke-") as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "hello.txt").write_text("first tool result\n")
            config = root / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "data_dir": str(root / "state"),
                        "runner": [sys.executable, "-m", "ngn_nagents"],
                        "provider": "fixture",
                        "providers": {
                            "fixture": {
                                "kind": "openai_compatible",
                                "model": "steering-fixture",
                                "api": "chat_completions",
                                "base_url": f"http://127.0.0.1:{port}/v1",
                                "api_key_env": "NGN_STEERING_FIXTURE_KEY",
                            }
                        },
                    }
                )
            )
            process = await asyncio.create_subprocess_exec(
                str(binary),
                "--config",
                str(config),
                "-C",
                str(workspace),
                "run",
                "--json",
                "cli final",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), 20)
            require(process.returncode == 0, f"CLI final boundary failed: {stderr.decode()}")
            require(
                json.loads(stdout.splitlines()[-1])["status"] == "completed", "CLI omitted final boundary completion"
            )
            await http_checks(binary, config, workspace, fixture)
            await protocol_checks(binary, config, root)
        print(
            "PASS: source-runtime CLI final boundary; real HTTP/native two-tool batch → FIFO steering → persistence acknowledgments; no-tools final continuation; cancel/late denial; durable retry across restart"
        )
        print(
            "PASS: baseline v1 runtime without safe_steering; wrong-ack rejection; cancel and real host SIGKILL in offer/ack window → uncertain/undelivered, no runtime/tool replay"
        )
    finally:
        fixture.release_cancel.set()
        await runner.cleanup()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--runtime-fixture":
        runtime_fixture(sys.argv[2])
    else:
        asyncio.run(main())
