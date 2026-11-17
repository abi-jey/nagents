"""Real Chromium + PTY steering acceptance, with a loopback-only two-tool model.

Run inside an explicit Playwright/aiohttp venv, using a private home-cache TMPDIR.
--runtime-python selects an existing venv; default mode pins this checkout's core
and adapter, while --installed-runtime verifies and uses isolated installed imports.
No credentials, paid requests, legacy state or stdin steering claims.
Evidence stays in --output; host/runtime state stays in the printed private root.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import pty
import re
import select
import sqlite3
import struct
import subprocess
import tempfile
import termios
import time
from pathlib import Path
from uuid import uuid4

from aiohttp import web
from playwright.async_api import Browser, Page, async_playwright
from steering_smoke import REPO, adapter_runner, require, server


class Fixture:
    def __init__(self) -> None:
        self.requests: list[list[dict[str, object]]] = []
        self.checked: set[str] = set()
        self.browser_batch = asyncio.Event()
        self.release_browser = asyncio.Event()

    async def handle(self, request: web.Request) -> web.StreamResponse:
        if request.method == "GET":
            return web.json_response({"data": [{"id": "client-fixture", "object": "model"}]})
        messages = (await request.json())["messages"]
        self.requests.append(messages)
        prompt = messages[-1].get("content", "")
        if prompt in {"browser batch", "browser cancel", "pty batch", "pty cancel"}:
            prefix = prompt.replace(" ", "-")
            delta = {
                "tool_calls": [
                    {
                        "index": index,
                        "id": f"client-{uuid4().hex}",
                        "type": "function",
                        "function": {
                            "name": "write",
                            "arguments": json.dumps({"path": f"{prefix}-{index}.txt", "content": f"tool {index}\n"}),
                        },
                    }
                    for index in range(2)
                ]
            }
            reason = "tool_calls"
        else:
            kind = "browser" if prompt == "browser second steering" else "pty"
            require(prompt == f"{kind} second steering", f"Unexpected/early model input: {prompt}")
            require(
                [message["role"] for message in messages if message["role"] != "system"]
                == ["user", "assistant", "tool", "tool", "user", "user"],
                "Client steering reached model before the complete persisted two-tool batch",
            )
            require(
                [message["content"] for message in messages[-2:]]
                == [f"{kind} first steering", f"{kind} second steering"],
                "Client steering order/text changed",
            )
            self.checked.add(kind)
            if kind == "browser":
                self.browser_batch.set()
                await self.release_browser.wait()
            delta, reason = (
                {"content": "Both native results and both persisted steering messages were received."},
                "stop",
            )
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        for part in [
            {"index": 0, "delta": delta, "finish_reason": None},
            {"index": 0, "delta": {}, "finish_reason": reason},
        ]:
            await response.write(("data: " + json.dumps({"choices": [part]}) + "\n\n").encode())
        await response.write(b"data: [DONE]\n\n")
        return response


async def receipt_status(page: Page, status: str, count: int) -> None:
    await page.wait_for_function(
        "([status, count]) => [...document.querySelectorAll('#steering-receipts article strong')].filter(e => e.textContent.includes(' · ' + status + ' · ')).length === count",
        arg=[status, count],
    )


async def finished(page: Page, status: str) -> None:
    await page.locator("#transcript").get_by_text(status, exact=True).last.wait_for()
    await page.locator("#status").filter(has_text="Ready").wait_for()


async def browser_checks(browser: Browser, url: str, workspace: Path, fixture: Fixture, output: Path) -> None:
    errors: list[str] = []
    context = await browser.new_context(viewport={"width": 1280, "height": 980})
    try:
        page = await context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.goto(url)
        await page.locator("#connection").filter(has_text="Connected").wait_for()
        require(await page.locator("#steer").is_disabled(), "Idle client enabled steering")
        await page.locator("#prompt").fill("browser batch")
        await page.locator("#prompt").press("Enter")
        await page.locator("#approval").wait_for(state="visible")
        await page.wait_for_function("() => !document.getElementById('steer').disabled")
        await page.locator("#steering-panel summary").click()
        for text in ["browser first steering", "browser second steering"]:
            await page.locator("#prompt").fill(text)
            await page.locator("#steer").click()
            await page.wait_for_function("() => document.getElementById('prompt').value === ''")
        await receipt_status(page, "pending", 2)
        require(len(fixture.requests) == 1, "Steering made a model request before either tool approval")
        require(not (workspace / "browser-batch-0.txt").exists(), "First native tool ran without approval")
        require(not (workspace / "browser-batch-1.txt").exists(), "Second native tool ran without approval")
        await page.screenshot(path=str(output / "browser-pending-first-approval.png"))
        await page.locator("#allow").click()
        await page.locator("#preview").filter(has_text="browser-batch-1.txt").wait_for()
        await receipt_status(page, "pending", 2)
        require((workspace / "browser-batch-0.txt").read_text() == "tool 0\n", "First native result missing")
        require(not (workspace / "browser-batch-1.txt").exists(), "Second tool skipped its approval")
        require(len(fixture.requests) == 1, "Steering was injected between native tools")
        await page.screenshot(path=str(output / "browser-pending-second-approval.png"))
        await page.locator("#allow").click()
        await asyncio.wait_for(fixture.browser_batch.wait(), 15)
        await receipt_status(page, "applied", 2)
        require((workspace / "browser-batch-1.txt").read_text() == "tool 1\n", "Second persisted native result missing")
        require(
            "not proof of model consumption" in await page.locator("#steering-receipts").inner_text(),
            "Receipt overclaimed delivery",
        )
        await page.locator("#prompt").fill("ordinary next-run FIFO")
        await page.locator("#send").click()
        await page.locator("#queue").filter(has_text="ordinary next-run FIFO").wait_for()
        require(
            "ordinary next-run FIFO" not in await page.locator("#steering-receipts").inner_text(),
            "Busy Send became steering",
        )
        await page.locator("#pause-queue").click()
        await page.locator("#queue-status").filter(has_text="Paused").wait_for()
        await page.screenshot(path=str(output / "browser-applied-and-separate-fifo.png"))
        fixture.release_browser.set()
        await finished(page, "completed")
        await page.reload()
        await page.locator("#connection").filter(has_text="Connected").wait_for()
        await page.locator("#steering-panel summary").click()
        await receipt_status(page, "applied", 2)
        require(len(fixture.requests) == 2, "Browser reconnect replayed model/tool input")
        require(await page.locator("#steer").is_disabled(), "Terminal root still accepts steering in UI")
        require(not errors, f"Browser errors: {errors}")
    finally:
        await context.close()

    context = await browser.new_context(viewport={"width": 390, "height": 780}, is_mobile=True, has_touch=True)
    try:
        page = await context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.goto(url)
        await page.locator("#connection").filter(has_text="Connected").wait_for()
        previous = await page.locator("#mobile-sessions").input_value()
        await page.locator("#mobile-new-session").tap()
        await page.wait_for_function("id => document.getElementById('mobile-sessions').value !== id", arg=previous)
        await page.locator("#prompt").fill("local Return")
        await page.locator("#prompt").press("End")
        await page.locator("#prompt").press("Enter")
        require(await page.locator("#prompt").input_value() == "local Return\n", "Touch Return no longer edits locally")
        await page.locator("#prompt").fill("browser cancel")
        await page.locator("#send").tap()
        await page.locator("#approval").wait_for(state="visible")
        await page.wait_for_function("() => !document.getElementById('steer').disabled")
        await page.locator("#prompt").fill("never offered on Stop")
        await page.locator("#steer").tap()
        await page.wait_for_function("() => document.getElementById('prompt').value === ''")
        require(await page.locator("#prompt").evaluate("e => e === document.activeElement"), "Steer stole touch focus")
        await page.locator("#steering-panel summary").tap()
        await receipt_status(page, "pending", 1)
        await page.locator("#prompt").fill("newer unsent draft")
        await page.locator("#cancel").tap()
        await finished(page, "cancelled")
        await receipt_status(page, "undelivered", 1)
        require(await page.locator("#prompt").input_value() == "newer unsent draft", "Stop cleared a newer touch draft")
        require(await page.locator("#prompt").evaluate("e => e === document.activeElement"), "Stop stole touch focus")
        require(not (workspace / "browser-cancel-0.txt").exists(), "Stop executed an unapproved tool")
        await page.screenshot(path=str(output / "touch-stop-undelivered.png"))
        await page.reload()
        await page.locator("#connection").filter(has_text="Connected").wait_for()
        await page.locator("#steering-panel summary").tap()
        await receipt_status(page, "undelivered", 1)
        require(not errors, f"Touch browser errors: {errors}")
    finally:
        await context.close()


async def browser_failure(browser: Browser, url: str, output: Path) -> None:
    context = await browser.new_context(viewport={"width": 1280, "height": 980})
    try:
        page = await context.new_page()
        await page.goto(url)
        await page.locator("#connection").filter(has_text="Connected").wait_for()
        await page.locator("#prompt").fill("wrong-ack trusted runtime fixture")
        await page.locator("#send").click()
        await page.locator("#approval").wait_for(state="visible")
        await page.wait_for_function("() => !document.getElementById('steer').disabled")
        for prompt in ["offered without confirmation", "pending at failure"]:
            await page.locator("#prompt").fill(prompt)
            await page.locator("#steer").click()
            await page.wait_for_function("() => document.getElementById('prompt').value === ''")
        await page.locator("#allow").click()
        await page.locator("#transcript .notice.error").filter(has_text="failed").wait_for()
        await page.locator("#steering-panel summary").click()
        await receipt_status(page, "uncertain", 1)
        await receipt_status(page, "undelivered", 1)
        await page.screenshot(path=str(output / "browser-failed-uncertain-undelivered.png"))
        posts: list[str] = []
        page.on("request", lambda request: posts.append(request.url) if request.method == "POST" else None)
        await page.reload()
        await page.locator("#connection").filter(has_text="Connected").wait_for()
        await page.locator("#steering-panel summary").click()
        await receipt_status(page, "uncertain", 1)
        await receipt_status(page, "undelivered", 1)
        require(not posts, "Reconnect automatically retried an uncertain steering input")
    finally:
        await context.close()


def pty_check(binary: Path, config: Path, workspace: Path, data: Path, output: Path) -> None:
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 42, 132, 0, 0))
    original = termios.tcgetattr(slave)
    process = subprocess.Popen(
        [str(binary), "--config", str(config), "-C", str(workspace)],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
    )
    os.close(slave)
    history = bytearray()

    def read() -> bytes:
        if select.select([master], [], [], 0.05)[0]:
            try:
                part = os.read(master, 65536)
                history.extend(part)
                return part
            except OSError:
                pass
        return b""

    def visible(raw: bytes) -> bytes:
        return b" ".join(re.sub(rb"\x1b\[[0-?]*[ -/]*[@-~]", b" ", raw).split())

    def wait(text: bytes, start: int = 0) -> None:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            read()
            if text in visible(history[start:]):
                return
            require(process.poll() is None, f"TUI exited while awaiting {text!r}")
        raise AssertionError(f"PTY did not render {text!r}: {visible(history[start:])[-2000:]!r}")

    def query(sql: str) -> list[tuple[object, ...]]:
        databases = list(data.rglob("host.sqlite3"))
        if not databases:
            return []
        with sqlite3.connect(databases[0]) as database:
            return database.execute(sql).fetchall()

    def rows(sql: str, count: int) -> list[tuple[object, ...]]:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            read()
            result = query(sql)
            if len(result) == count:
                return result
        raise AssertionError(f"PTY delivery/storage boundary not reached: {sql}")

    try:
        wait(b"Welcome to ngn")
        os.write(master, b"pty batch\r")
        wait(b"Approval:")
        root = str(rows("SELECT id FROM runs WHERE status = 'running'", 1)[0][0])
        for prompt in ["pty first steering", "pty second steering"]:
            os.write(master, f"/steer {prompt}\r".encode())
        pending = rows("SELECT run_id, prompt FROM steering_inputs WHERE status = 'pending' ORDER BY position", 2)
        require(
            pending == [(root, "pty first steering"), (root, "pty second steering")],
            "PTY steering target/order changed",
        )
        start = len(history)
        os.write(master, b"\x1b[18~")  # F7, through the terminal input driver.
        wait(b"Steering receipts", start)
        wait(root.encode(), start)
        wait(b"pending", start)
        wait(b"pty second steering", start)
        require(not (workspace / "pty-batch-0.txt").exists(), "PTY steering bypassed first approval")
        os.write(master, b"\x1b[18~\x19")  # F7 close, Ctrl+Y allow first call.
        wait(b"pty-batch-1.txt")
        rows("SELECT run_id FROM steering_inputs WHERE status = 'pending'", 2)
        require((workspace / "pty-batch-0.txt").read_text() == "tool 0\n", "PTY first native result missing")
        require(not (workspace / "pty-batch-1.txt").exists(), "PTY second tool ran without approval")
        start = len(history)
        os.write(master, b"\x1b[18~")
        wait(b"pending", start)
        os.write(master, b"\x1b[18~\x19")
        rows("SELECT id FROM runs WHERE status = 'completed'", 1)
        rows("SELECT run_id FROM steering_inputs WHERE status = 'applied'", 2)
        start = len(history)
        os.write(master, b"\x1b[18~")
        wait(b"applied", start)
        wait(root.encode(), start)
        wait(b"not proof of model consumption", start)
        os.write(master, b"\x1b[18~pty cancel\r")
        wait(b"pty-cancel-0.txt")
        os.write(master, b"/steer terminal receipt after Stop\r")
        rows("SELECT run_id FROM steering_inputs WHERE status = 'pending'", 1)
        os.write(master, b"\x1b")  # Esc: stop the exact active root.
        rows("SELECT id FROM runs WHERE status = 'cancelled'", 1)
        rows("SELECT run_id FROM steering_inputs WHERE status = 'undelivered'", 1)
        start = len(history)
        os.write(master, b"\x1b[18~")
        wait(b"undelivered", start)
        wait(b"terminal receipt after Stop", start)
        require(not (workspace / "pty-cancel-0.txt").exists(), "PTY Stop executed pending tool")
        os.write(master, b"\x11")
        deadline = time.monotonic() + 15
        while process.poll() is None and time.monotonic() < deadline:
            read()
        process.wait(timeout=2)
        require(process.returncode == 0, "PTY did not exit cleanly")
        restored = termios.tcgetattr(master)
        mask = termios.ICANON | termios.ECHO | termios.ISIG
        require(restored[3] & mask == original[3] & mask, "PTY terminal modes were not restored")
    finally:
        (output / "tui-steering.ansi").write_bytes(history)
        (output / "tui-steering.txt").write_bytes(visible(history))
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(master)


async def main(binary: Path, runtime_python: Path, executable: str, output: Path, *, installed: bool = False) -> None:
    output.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="ngn-steering-clients-"))
    print(f"Private fixture state: {root}", flush=True)
    os.environ["NGN_STEERING_CLIENT_FIXTURE_KEY"] = "loopback-only-not-a-provider-credential"
    adapter = adapter_runner(runtime_python, installed=installed)
    fixture = Fixture()
    application = web.Application()
    application.router.add_route("*", "/v1/{tail:.*}", fixture.handle)
    runner = web.AppRunner(application, access_log=None, shutdown_timeout=0.1)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()

    def settings(name: str, failure: bool = False) -> tuple[Path, Path, Path]:
        workspace, data, config = root / name, root / f"{name}-state", root / f"{name}.json"
        workspace.mkdir()
        config.write_text(
            json.dumps(
                {
                    "data_dir": str(data),
                    "runner": [
                        str(runtime_python),
                        str(REPO / "ngn/scripts/steering_smoke.py"),
                        "--runtime-fixture",
                        "wrong-ack",
                    ]
                    if failure
                    else adapter,
                    "provider": "fixture",
                    "providers": {
                        "fixture": {
                            "kind": "openai_compatible",
                            "api": "chat_completions",
                            "model": "client-fixture",
                            "base_url": f"http://127.0.0.1:{runner.addresses[0][1]}/v1",
                            "api_key_env": "NGN_STEERING_CLIENT_FIXTURE_KEY",
                        }
                    },
                }
            )
        )
        return workspace, data, config

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                executable_path=executable or None, args=["--no-sandbox", "--disable-dev-shm-usage"]
            )
            try:
                workspace, _, config = settings("browser")
                async with server(binary, config, workspace) as (url, _):
                    await browser_checks(browser, url, workspace, fixture, output)
                workspace, _, config = settings("failure", True)
                async with server(binary, config, workspace) as (url, _):
                    await browser_failure(browser, url, output)
            finally:
                await browser.close()
        workspace, data, config = settings("pty")
        await asyncio.to_thread(pty_check, binary, config, workspace, data, output)
        require(fixture.checked == {"browser", "pty"}, "Actual clients did not reach the Python model boundary")
        evidence = {
            "checks": sorted(fixture.checked),
            "model_requests": fixture.requests,
            "private_root": str(root),
            "runtime": adapter,
            "installed_runtime": installed,
        }
        (output / "model-evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
        print(
            "PASS: real Chromium desktop/touch and real PTY /steer + F7; two native approvals before persistence-ack/model continuation; separate FIFO; Stop, failure, reconnect receipts; terminal restoration"
        )
    finally:
        fixture.release_browser.set()
        await runner.cleanup()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--runtime-python", type=Path, required=True)
    parser.add_argument(
        "--installed-runtime", action="store_true", help="require isolated installed core/adapter imports"
    )
    parser.add_argument("--browser", default="")
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    asyncio.run(
        main(
            arguments.binary.resolve(),
            arguments.runtime_python.absolute(),
            arguments.browser,
            arguments.output,
            installed=arguments.installed_runtime,
        )
    )
