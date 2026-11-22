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
import threading
import time
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from aiohttp import web
from browser_smoke import approval_dialog, connected, new_mobile_session, ready, root_conversation, stopped_message
from playwright.async_api import Browser, Locator, Page, async_playwright, expect
from pty_smoke import COMPOSER_TITLE, WorkspaceReady
from steering_smoke import REPO, adapter_runner, require, server


class Fixture:
    def __init__(self) -> None:
        self.requests: list[list[dict[str, object]]] = []
        self.checked: set[str] = set()
        self.browser_batch = asyncio.Event()
        self.release_browser = asyncio.Event()
        self.pty_started = {prompt: threading.Event() for prompt in ("pty batch", "pty cancel")}
        self.release_pty = {prompt: asyncio.Event() for prompt in self.pty_started}

    async def handle(self, request: web.Request) -> web.StreamResponse:
        if request.method == "GET":
            return web.json_response({"data": [{"id": "client-fixture", "object": "model"}]})
        messages = (await request.json())["messages"]
        self.requests.append(messages)
        prompt = messages[-1].get("content", "")
        if prompt in {"browser batch", "browser cancel", "pty batch", "pty cancel"}:
            if prompt in self.pty_started:
                # Let the TUI steer while the composer owns input. The approval
                # panel deliberately won't accept composer commands.
                self.pty_started[prompt].set()
                await self.release_pty[prompt].wait()
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


async def receipt_status(page: Page, status: str, count: int) -> Locator:
    panel = page.locator(".native-receipts")
    summary = panel.locator("summary")
    await expect(summary).to_be_visible()
    if await panel.get_attribute("open") is None:
        await summary.click()
    await expect(
        panel.locator(".native-receipt > strong").filter(has_text=re.compile(rf"^#\d+ · {re.escape(status)} · "))
    ).to_have_count(count)
    return panel


async def defer_browser_approval(page: Page, *, touch: bool = False) -> None:
    dialog = approval_dialog(page)
    later = dialog.get_by_role("button", name="Review later", exact=True)
    if touch:
        await later.tap()
    else:
        await later.click()
    await expect(dialog).to_be_hidden()
    await expect(page.get_by_role("button", name="Review pending approval", exact=True)).to_be_visible()


async def steer_browser(page: Page, text: str, *, touch: bool = False) -> None:
    composer = page.locator("#composer")
    await composer.fill(text)
    steer = page.get_by_role("button", name="Steer active turn", exact=True)
    if touch:
        await steer.tap()
    else:
        await steer.click()
    await expect(composer).to_have_value("")
    if touch:
        await expect(composer).to_be_focused()


async def reopen_browser_approval(page: Page, path: str) -> Locator:
    await page.get_by_role("button", name="Review pending approval", exact=True).click()
    dialog = approval_dialog(page)
    await expect(dialog.get_by_role("region", name="Proposed change", exact=True)).to_contain_text(f"+++ b/{path}")
    return dialog


async def browser_checks(browser: Browser, url: str, workspace: Path, fixture: Fixture, output: Path) -> None:
    errors: list[str] = []
    context = await browser.new_context(viewport={"width": 1280, "height": 980})
    try:
        page = await context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.goto(url)
        await connected(page)
        composer = page.locator("#composer")
        steer = page.get_by_role("button", name="Steer active turn", exact=True)
        await expect(steer).to_have_count(0)
        await composer.fill("browser batch")
        await expect(page.get_by_role("button", name="Send", exact=True)).to_be_enabled()
        await composer.press("Enter")
        await defer_browser_approval(page)
        for text in ["browser first steering", "browser second steering"]:
            await steer_browser(page, text)
        await receipt_status(page, "Pending", 2)
        require(len(fixture.requests) == 1, "Steering made a model request before either tool approval")
        require(not (workspace / "browser-batch-0.txt").exists(), "First native tool ran without approval")
        require(not (workspace / "browser-batch-1.txt").exists(), "Second native tool ran without approval")
        dialog = await reopen_browser_approval(page, "browser-batch-0.txt")
        await page.screenshot(path=str(output / "browser-pending-first-approval.png"))
        allow = dialog.get_by_role("button", name="Allow once", exact=True)
        await allow.click()
        await expect(dialog.get_by_role("region", name="Proposed change", exact=True)).to_contain_text(
            "+++ b/browser-batch-1.txt"
        )
        await receipt_status(page, "Pending", 2)
        require((workspace / "browser-batch-0.txt").read_text() == "tool 0\n", "First native result missing")
        require(not (workspace / "browser-batch-1.txt").exists(), "Second tool skipped its approval")
        require(len(fixture.requests) == 1, "Steering was injected between native tools")
        await page.screenshot(path=str(output / "browser-pending-second-approval.png"))
        await allow.click()
        await asyncio.wait_for(fixture.browser_batch.wait(), 15)
        receipts = await receipt_status(page, "Applied", 2)
        require((workspace / "browser-batch-1.txt").read_text() == "tool 1\n", "Second persisted native result missing")
        await expect(receipts).to_contain_text("not proof of model consumption")
        await composer.fill("ordinary next-run FIFO")
        await page.get_by_role("button", name="Queue", exact=True).click()
        queue = page.locator(".native-queue")
        summary = queue.locator("summary")
        await expect(summary).to_contain_text("1 pending")
        await summary.click()
        await expect(queue.get_by_text("ordinary next-run FIFO", exact=True)).to_be_visible()
        await expect(receipts).not_to_contain_text("ordinary next-run FIFO")
        await queue.get_by_role("button", name="Pause queue", exact=True).click()
        await expect(summary).to_contain_text("Paused")
        await page.screenshot(path=str(output / "browser-applied-and-separate-fifo.png"))
        fixture.release_browser.set()
        await ready(page, "Both native results and both persisted steering messages were received.")
        await page.reload()
        await connected(page)
        await receipt_status(page, "Applied", 2)
        require(len(fixture.requests) == 2, "Browser reconnect replayed model/tool input")
        await expect(steer).to_have_count(0)
        require(not errors, f"Browser errors: {errors}")
    finally:
        await context.close()

    context = await browser.new_context(viewport={"width": 390, "height": 780}, is_mobile=True, has_touch=True)
    try:
        page = await context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.goto(url)
        await connected(page)
        await new_mobile_session(page)
        composer = page.locator("#composer")
        await composer.fill("local Return")
        await composer.press("End")
        await composer.press("Enter")
        await expect(composer).to_have_value("local Return\n")
        await composer.fill("browser cancel")
        await page.get_by_role("button", name="Send", exact=True).tap()
        await defer_browser_approval(page, touch=True)
        await steer_browser(page, "never offered on Stop", touch=True)
        await receipt_status(page, "Pending", 1)
        await composer.fill("newer unsent draft")
        await page.locator(".chat-composer").get_by_role("button", name="Stop run", exact=True).tap()
        await expect(stopped_message(root_conversation(page))).to_be_attached()
        await ready(page)
        await receipt_status(page, "Undelivered", 1)
        await expect(composer).to_have_value("newer unsent draft")
        await expect(composer).to_be_focused()
        require(not (workspace / "browser-cancel-0.txt").exists(), "Stop executed an unapproved tool")
        await page.screenshot(path=str(output / "touch-stop-undelivered.png"))
        await page.reload()
        await connected(page)
        await receipt_status(page, "Undelivered", 1)
        require(not errors, f"Touch browser errors: {errors}")
    finally:
        await context.close()


async def browser_failure(browser: Browser, url: str, output: Path) -> None:
    context = await browser.new_context(viewport={"width": 1280, "height": 980})
    try:
        page = await context.new_page()
        await page.goto(url)
        await connected(page)
        await page.locator("#composer").fill("wrong-ack trusted runtime fixture")
        await page.get_by_role("button", name="Send", exact=True).click()
        await defer_browser_approval(page)
        for prompt in ["offered without confirmation", "pending at failure"]:
            await steer_browser(page, prompt)
        await receipt_status(page, "Pending", 2)
        await page.get_by_role("button", name="Review pending approval", exact=True).click()
        await approval_dialog(page).get_by_role("button", name="Allow once", exact=True).click()
        await expect(root_conversation(page).locator(".entry.error")).to_contain_text("Run failed")
        await receipt_status(page, "Uncertain", 1)
        await receipt_status(page, "Undelivered", 1)
        await page.screenshot(path=str(output / "browser-failed-uncertain-undelivered.png"))
        posts: list[str] = []
        page.on("request", lambda request: posts.append(request.url) if request.method == "POST" else None)
        await page.reload()
        await connected(page)
        await receipt_status(page, "Uncertain", 1)
        await receipt_status(page, "Undelivered", 1)
        require(not posts, "Reconnect automatically retried an uncertain steering input")
    finally:
        await context.close()


def pty_check(
    binary: Path,
    config: Path,
    workspace: Path,
    data: Path,
    output: Path,
    started: dict[str, threading.Event],
    release: Callable[[str], None],
) -> None:
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

    def wait_for_model(prompt: str) -> None:
        deadline = time.monotonic() + 20
        while not started[prompt].is_set() and time.monotonic() < deadline:
            read()
            require(process.poll() is None, "TUI exited before the model fixture was called")
        require(started[prompt].is_set(), f"TUI did not reach model for {prompt}")

    try:
        ready = WorkspaceReady(master)
        deadline = time.monotonic() + 20
        while not ready.observe(history, COMPOSER_TITLE in visible(history)) and time.monotonic() < deadline:
            read()
            require(process.poll() is None, "TUI exited before repainting the workspace")
        require(
            ready.observe(history, COMPOSER_TITLE in visible(history)),
            "Steering TUI did not render and repaint at the resized PTY dimensions",
        )
        os.write(master, b"pty batch\r")
        wait_for_model("pty batch")
        root = str(rows("SELECT id FROM runs WHERE status = 'running'", 1)[0][0])
        for prompt in ["pty first steering", "pty second steering"]:
            os.write(master, f"/steer {prompt}\r".encode())
        pending = rows("SELECT run_id, prompt FROM steering_inputs WHERE status = 'pending' ORDER BY position", 2)
        require(
            pending == [(root, "pty first steering"), (root, "pty second steering")],
            "PTY steering target/order changed",
        )
        release("pty batch")
        wait(b"y allow")
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
        wait_for_model("pty cancel")
        os.write(master, b"/steer terminal receipt after Stop\r")
        rows("SELECT run_id FROM steering_inputs WHERE status = 'pending'", 1)
        release("pty cancel")
        wait(b"pty-cancel-0.txt")
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
    os.environ["NGN_WIRE_LOG"] = "false"
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
        loop = asyncio.get_running_loop()

        def release_pty(prompt: str) -> None:
            loop.call_soon_threadsafe(fixture.release_pty[prompt].set)

        await asyncio.to_thread(pty_check, binary, config, workspace, data, output, fixture.pty_started, release_pty)
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
        for release in fixture.release_pty.values():
            release.set()
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
