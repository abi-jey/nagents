"""Optional real-Chromium acceptance against a local model fixture, never a paid API.

Run in an explicit venv with aiohttp and Playwright installed. Pass the extracted
binary and its installed runtime, e.g. --binary CANDIDATE/app/ngn --runtime-python
CANDIDATE/runtime/bin/python. --browser can select an installed Chromium executable.
No legacy state or credentials are read; all tool effects stay in a fresh workspace.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
from pathlib import Path

from aiohttp import web
from playwright.async_api import Browser, Page, async_playwright
from smoke import require

CONTENT = "".join(f"Fixture line {index}: review the exact proposed native write.\n" for index in range(300))


async def model(request: web.Request) -> web.StreamResponse:
    if request.method == "GET":
        return web.json_response({"data": [{"id": "fixture-model", "object": "model"}]})
    messages = (await request.json())["messages"]
    last = messages[-1]
    if last["role"] == "user":
        prompt = last["content"]
        if prompt == "delegate writer":
            name, arguments = "to_agent", {"prompt": "write child", "title": "Browser fixture writer"}
        elif prompt in {"write long", "write child"}:
            name, arguments = (
                "write",
                {"path": "child.txt" if prompt == "write child" else "preview.txt", "content": CONTENT},
            )
        else:
            name, arguments = "read_file", {"path": "hello.txt"}
        delta = {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "browser-call",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ]
        }
        reason = "tool_calls"
    else:
        require(last["role"] == "tool", "Expected a real host-owned tool result")
        delta, reason = {"content": "The local fixture received the native tool result."}, "stop"
    response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
    await response.prepare(request)
    for chunk, finish in [(delta, None), ({}, reason)]:
        value = {"choices": [{"index": 0, "delta": chunk, "finish_reason": finish}]}
        await response.write(("data: " + json.dumps(value) + "\n\n").encode())
    await response.write(b"data: [DONE]\n\n")
    return response


async def ready(page: Page) -> None:
    await page.locator("#status").filter(has_text="Ready").wait_for()
    await page.locator("#transcript").get_by_text("completed", exact=True).last.wait_for()


async def new_mobile_session(page: Page) -> None:
    previous = await page.locator("#mobile-sessions").input_value()
    await page.locator("#mobile-new-session").tap()
    await page.wait_for_function(
        "previous => document.getElementById('mobile-sessions').value !== previous && document.activeElement?.id === 'prompt'",
        arg=previous,
    )
    await page.locator("#transcript .message").wait_for(state="hidden")


async def inspect_layout(page: Page) -> None:
    await page.locator("#allow").scroll_into_view_if_needed()
    await page.locator("#allow").click(trial=True)
    await page.locator("#deny").click(trial=True)
    dimensions = await page.locator("#preview").evaluate(
        "element => ({height: element.clientHeight, content: element.scrollHeight})"
    )
    require(dimensions["height"] > 0, "Approval preview collapsed")
    require(dimensions["content"] > dimensions["height"], "Long approval preview did not scroll independently")
    require(
        await page.evaluate("document.documentElement.scrollWidth <= innerWidth"),
        "Browser layout overflowed horizontally",
    )
    require(
        await page.locator("#queue-panel").evaluate(
            "element => element.clientHeight >= element.firstElementChild.offsetHeight"
        ),
        "Pending-input controls were clipped by flex shrinking",
    )


async def exercise(browser: Browser, url: str, workspace: Path, output: Path) -> None:
    errors: list[str] = []
    context = await browser.new_context(viewport={"width": 1280, "height": 900})
    try:
        page = await context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.goto(url)
        await page.locator("#connection").filter(has_text="Connected").wait_for()
        await page.locator("#prompt").fill("read hello.txt")
        await page.locator("#prompt").press("Enter")
        await ready(page)
        require(await page.locator(".tool summary").count() == 1, "Desktop Enter duplicated submission")
        require("hello.txt" in await page.locator(".tool summary").inner_text(), "Native target missing")
        await page.screenshot(path=str(output / "desktop-read.png"))
    finally:
        await context.close()

    context = await browser.new_context(
        viewport={"width": 390, "height": 780}, is_mobile=True, has_touch=True, device_scale_factor=1
    )
    try:
        page = await context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.goto(url)
        await page.locator("#connection").filter(has_text="Connected").wait_for()
        await new_mobile_session(page)
        require(await page.locator("#prompt").get_attribute("enterkeyhint") == "enter", "Touch hint is not Return")
        await page.locator("#prompt").fill("unsent draft")
        await page.locator("#prompt").press("End")
        await page.locator("#prompt").press("Enter")
        require(await page.locator("#prompt").input_value() == "unsent draft\n", "Touch Return submitted or lost draft")
        require(await page.locator("#transcript .message.user").count() == 0, "Touch Return made a network submission")
        await page.locator("#prompt").fill("write long")
        await page.locator("#send").tap()
        await page.locator("#approval").wait_for(state="visible")
        require(
            await page.locator("#prompt").evaluate("element => element === document.activeElement"),
            "Send stole touch composer focus",
        )
        require(not (workspace / "preview.txt").exists(), "Native write occurred before browser approval")
        summary = page.locator(".tool summary").last
        await summary.filter(has_text="waiting for approval").wait_for()
        frozen = await summary.inner_text()
        await inspect_layout(page)
        await page.screenshot(path=str(output / "touch-write-approval.png"))
        require(await summary.inner_text() == frozen, "Approval active clock counted browser review time")
        await page.locator("#allow").tap()
        await ready(page)
        require((workspace / "preview.txt").read_text() == CONTENT, "Approved native write was not applied exactly")
        require("preview.txt" in await summary.inner_text(), "Completed tool lost its target")

        await new_mobile_session(page)
        await page.locator("#prompt").fill("delegate writer")
        await page.locator("#send").tap()
        await page.locator("#approval").wait_for(state="visible")
        await page.locator("#tasks").get_by_role("button", name="Inspect", exact=True).first.tap()
        await page.locator("#task-transcript").filter(has_text="write").wait_for()
        await page.set_viewport_size({"width": 390, "height": 560})
        await inspect_layout(page)
        await page.screenshot(path=str(output / "touch-child-approval.png"))
        require("Depth 1" in await page.locator("#approval-label").inner_text(), "Child approval lost its owner")
        await page.locator("#deny").tap()
        await ready(page)
        require(not (workspace / "child.txt").exists(), "Denied child created a file")
        require((workspace / "preview.txt").read_text() == CONTENT, "Denied child changed the approved file")
        await new_mobile_session(page)
        await page.locator("#prompt").fill("write child")
        await page.locator("#send").tap()
        await page.locator("#approval").wait_for(state="visible")
        await page.locator("#prompt").fill("retain this draft while stopping")
        await page.locator("#cancel").tap()
        await page.locator("#transcript").get_by_text("cancelled", exact=True).last.wait_for()
        require(
            await page.locator("#prompt").input_value() == "retain this draft while stopping",
            "Stop erased the local draft",
        )
        require(
            await page.locator("#prompt").evaluate("element => element === document.activeElement"),
            "Stop stole touch composer focus",
        )
        require(not (workspace / "child.txt").exists(), "Stopping a pending approval executed a native write")
        require(not errors, f"Browser page errors: {errors}")
    finally:
        await context.close()


async def main(binary: Path, runtime_python: Path, executable: str, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ngn-browser-") as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        workspace.mkdir()
        (workspace / "hello.txt").write_text("Read by the native Rust host.\n")
        application = web.Application()
        application.router.add_route("*", "/v1/{tail:.*}", model)
        fixture = web.AppRunner(application, access_log=None, shutdown_timeout=0.1)
        await fixture.setup()
        process = None
        try:
            await web.TCPSite(fixture, "127.0.0.1", 0).start()
            config = root / "config.json"
            config.write_text(
                json.dumps(
                    {
                        "workspace": str(workspace),
                        "data_dir": str(root / "state"),
                        "runner": [str(runtime_python), "-I", "-B", "-m", "ngn_nagents"],
                        "provider": "fixture",
                        "providers": {
                            "fixture": {
                                "kind": "openai_compatible",
                                "api": "chat_completions",
                                "model": "fixture-model",
                                "base_url": f"http://127.0.0.1:{fixture.addresses[0][1]}/v1",
                                "api_key_env": "NGN_BROWSER_FIXTURE_KEY",
                            }
                        },
                    }
                )
            )
            environment = dict(os.environ, NGN_BROWSER_FIXTURE_KEY="local-fixture-not-a-provider-credential")
            environment.pop("PYTHONPATH", None)
            process = await asyncio.create_subprocess_exec(
                str(binary),
                "--config",
                str(config),
                "serve",
                "--port",
                "0",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=environment,
            )
            require(process.stdout is not None, "Server stdout unavailable")
            async with asyncio.timeout(30):
                while line := await process.stdout.readline():
                    if line.startswith(b"Open http://"):
                        url = line.decode().strip().removeprefix("Open ")
                        break
                else:
                    raise AssertionError("Host did not report a bound browser URL")
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(
                    executable_path=executable or None, args=["--no-sandbox", "--disable-dev-shm-usage"]
                )
                try:
                    async with asyncio.timeout(120):
                        await exercise(browser, url, workspace, output)
                finally:
                    await browser.close()
        finally:
            if process is not None and process.returncode is None:
                process.terminate()
                try:
                    async with asyncio.timeout(15):
                        await process.wait()
                except TimeoutError:
                    process.kill()
                    await process.wait()
            await fixture.cleanup()
    print(
        "PASS: Chromium desktop/touch, Return/focus, native targets, frozen approval timing, long preview, child approval; no page errors"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--runtime-python", required=True, type=Path)
    parser.add_argument("--browser", default="")
    parser.add_argument("--output", required=True, type=Path, help="Directory for real UI evidence screenshots")
    arguments = parser.parse_args()
    # Preserve the venv executable path: resolving its symlink would select the
    # base interpreter instead of the installed runtime being accepted.
    asyncio.run(
        main(arguments.binary.resolve(), arguments.runtime_python.absolute(), arguments.browser, arguments.output)
    )
