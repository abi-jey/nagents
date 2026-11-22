"""Browser steering checks against the Python engine, used by python_engine_smoke.py.

A loopback-only two-tool model proves Enter steers and Ctrl+Enter queues in
real Chromium (desktop and touch), with approvals before model continuation.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from pathlib import Path
from uuid import uuid4

from aiohttp import web
from playwright.async_api import Browser, Locator, Page, expect
from smoke_support import approval_dialog, connected, new_mobile_session, ready, require, root_conversation


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
    # Enter steers only once the client knows the run accepts steering; until
    # then the button reads Queue and Enter queues, as it tells the user.
    steer = page.locator(".chat-composer").get_by_role("button", name="Steer", exact=True)
    await expect(steer).to_be_visible()
    if touch:
        await steer.tap()
    else:
        # While a turn runs, Enter steers it by default; Ctrl+Enter queues instead.
        await composer.press("Enter")
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
        steer = page.locator(".chat-composer").get_by_role("button", name="Steer", exact=True)
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
        await composer.press("Control+Enter")
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
        await expect(root_conversation(page).locator('.execution-state[data-state="Cancelled"]')).to_have_count(2)
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
