"""Exercise the installed React workspace in real Chromium with a loopback model fixture.

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
import re
import tempfile
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from aiohttp import web
from playwright.async_api import Browser, BrowserContext, Locator, Page, Request, Response, async_playwright, expect
from smoke import require

READ = "read hello.txt"
WRITE = "write long"
DELEGATE = "delegate writer"
CHILD = "write child"
STOP = "write to stop"
WRITES = {WRITE: "preview.txt", CHILD: "child.txt", STOP: "stopped.txt"}
WRITER = "Browser fixture writer"
HELLO = "Read by the native Rust host."
READ_HEADING = "Fixture read"
READ_CODE = 'print("fixture")'
WRITE_REPLY = "The local fixture received the native tool result."
ROOT_REPLY = "The fixture root received the delegation result."
CHILD_REPLY = "The fixture child received the native tool result."
LINES = [f"Fixture line {index}: review the exact proposed native write.\n" for index in range(300)]
CONTENT = "".join(LINES)
RELEASE_STREAM = web.AppKey("release_desktop_stream", asyncio.Event)


async def model(request: web.Request) -> web.StreamResponse:
    if request.method == "GET":
        return web.json_response({"data": [{"id": "fixture-model", "object": "model"}]})
    messages = (await request.json())["messages"]
    last = messages[-1]
    prompt = next(message["content"] for message in reversed(messages) if message["role"] == "user")
    response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
    await response.prepare(request)

    async def chunk(delta: dict[str, object], finish: str = "") -> None:
        value = {"choices": [{"index": 0, "delta": delta, "finish_reason": finish or None}]}
        await response.write(("data: " + json.dumps(value) + "\n\n").encode())

    if last["role"] == "user":
        arguments: dict[str, str]
        if prompt == DELEGATE:
            name, arguments = "to_agent", {"prompt": CHILD, "title": WRITER}
        elif prompt in WRITES:
            name, arguments = "write", {"path": WRITES[prompt], "content": CONTENT}
        else:
            name, arguments = "read_file", {"path": "hello.txt"}
        await chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "browser-call",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(arguments)},
                    }
                ]
            }
        )
        await chunk({}, "tool_calls")
    else:
        require(last["role"] == "tool", "Expected a real host-owned tool result")
        if prompt == READ:
            await chunk({"content": f"## {READ_HEADING}\n\n"})
            # Do not finish until the browser has observed the first delta in the transcript.
            await request.app[RELEASE_STREAM].wait()
            await chunk(
                {"content": f"The local fixture received the **native tool result**.\n\n```python\n{READ_CODE}\n```\n"}
            )
        else:
            await chunk({"content": {DELEGATE: ROOT_REPLY, CHILD: CHILD_REPLY}.get(prompt, WRITE_REPLY)})
        await chunk({}, "stop")
    await response.write(b"data: [DONE]\n\n")
    return response


def root_conversation(page: Page) -> Locator:
    return page.locator("main > .conversation")


def approval_dialog(page: Page) -> Locator:
    return page.locator("dialog.approval-dialog")


def tool_card(conversation: Locator, path: str) -> Locator:
    return conversation.locator(".tool-record").filter(has_text=path)


async def reveal_activity(record: Locator, group: Locator) -> None:
    await expect(record).to_be_attached()
    if not await record.is_visible():
        await group.locator(":scope > summary").click()
    await expect(record).to_be_visible()


async def check_tool(conversation: Locator, path: str, state: str) -> Locator:
    tool = tool_card(conversation, path)
    await reveal_activity(tool, conversation.locator(".chat-activity-group").filter(has_text=path))
    await expect(tool.locator(".execution-target")).to_have_text(path)
    await expect(tool.locator(".execution-state")).to_have_text(state)
    return tool


def stopped_message(conversation: Locator) -> Locator:
    return conversation.locator(".entry.status").get_by_text("Stopped", exact=True)


async def connected(page: Page) -> None:
    await expect(page.locator(".topbar .native-connection")).to_have_text("Connected")


async def ready(page: Page, reply: str = "") -> None:
    conversation = root_conversation(page)
    if reply:
        await expect(conversation.locator(".entry.assistant").get_by_text(reply, exact=True)).to_be_visible()
    await expect(page.locator(".run-status")).to_have_text("Ready")
    await expect(conversation.locator(".entry.error")).to_have_count(0)


async def open_workspace(context: BrowserContext, url: str, errors: list[str]) -> Page:
    page = await context.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))

    def asset_response(response: Response) -> None:
        if response.status >= 400 and urlsplit(response.url).path.startswith("/assets/"):
            errors.append(f"Asset HTTP {response.status}: {response.url}")

    page.on("response", asset_response)
    response = await page.goto(url)
    if response is None or not response.ok:
        raise AssertionError("Installed host did not serve the workspace")
    require(await response.header_value("content-security-policy"), "Workspace response is missing its CSP")
    await connected(page)
    return page


def watch_submissions(page: Page) -> list[str]:
    posts: list[str] = []

    def submitted(request: Request) -> None:
        path = urlsplit(request.url).path
        if request.method == "POST" and path.startswith("/api/sessions/") and path.endswith("/runs"):
            posts.append(path)

    page.on("request", submitted)
    return posts


async def desktop_read(page: Page, url: str, output: Path, release_stream: asyncio.Event) -> None:
    for panel in (".app-shell", ".sidebar", ".topbar", ".team-pane"):
        await expect(page.locator(panel)).to_be_visible()
    missing = await page.request.get(urljoin(url, "/assets/not-bundled-browser-fixture.js"))
    require(missing.status == 404, f"An unknown bundled asset returned HTTP {missing.status}")
    posts = watch_submissions(page)
    conversation = root_conversation(page)
    assistant = conversation.locator(".entry.assistant")
    composer = page.locator("#composer")
    await composer.fill(READ)
    await expect(page.get_by_role("button", name="Send", exact=True)).to_be_enabled()
    await composer.press("Enter")
    await expect(assistant.get_by_role("heading", name=READ_HEADING, exact=True)).to_be_visible()
    await expect(page.locator(".run-status")).to_have_text("Working…")
    await expect(assistant.locator("strong")).to_have_count(0)
    release_stream.set()
    await expect(assistant.locator("strong")).to_have_text("native tool result")
    code = assistant.get_by_role("region", name="Code (python)", exact=True)
    await expect(code.locator("code")).to_have_text(READ_CODE)
    await ready(page)
    await expect(conversation.locator(".entry.user")).to_have_count(1)
    await expect(conversation.locator(".entry.assistant")).to_have_count(1)
    await expect(conversation.locator(".tool-record")).to_have_count(1)
    require(len(posts) == 1, f"Desktop Enter made {len(posts)} native submissions")
    tool = await check_tool(conversation, "hello.txt", "Completed")
    await tool.locator(":scope > summary").click()
    await expect(tool.get_by_role("tabpanel", name="Result", exact=True)).to_contain_text(HELLO)
    await page.screenshot(path=str(output / "desktop-read.png"))


async def desktop_preferences(page: Page) -> None:
    saved_widths: list[tuple[Locator, str]] = []
    for name, selector, key in (
        ("sessions sidebar", ".sidebar", "ArrowRight"),
        ("team panel", ".team-pane", "ArrowLeft"),
    ):
        panel = page.locator(selector)
        divider = page.get_by_role("separator", name=f"Resize {name}", exact=True)
        before = await panel.bounding_box()
        require(before is not None, f"The {name} is not rendered")
        width = int(await divider.get_attribute("aria-valuenow") or "0")
        await divider.press(key)
        saved = str(width + 8)
        await expect(divider).to_have_attribute("aria-valuenow", saved)
        after = await panel.bounding_box()
        require(after is not None and after["width"] > before["width"], f"The {name} did not resize")
        saved_widths.append((divider, saved))

    settings = page.get_by_role("button", name="Settings", exact=True)
    appearance = page.get_by_role("dialog", name="Appearance", exact=True)
    await settings.click()
    await appearance.get_by_role("button", name="Detailed", exact=True).click()
    await expect(appearance.get_by_text("Saved in this browser", exact=True)).to_be_visible()
    await appearance.get_by_role("button", name="Close settings", exact=True).click()
    await expect(settings).to_be_focused()

    await page.reload()
    await connected(page)
    for divider, saved in saved_widths:
        await expect(divider).to_have_attribute("aria-valuenow", saved)
    conversation = root_conversation(page)
    await expect(conversation.get_by_role("heading", name=READ_HEADING, exact=True)).to_be_visible()
    await expect(conversation.locator(".entry.user")).to_have_count(1)
    await expect(conversation.locator(".entry.assistant")).to_have_count(1)
    await expect(conversation.locator(".tool-record")).to_have_count(1)
    await settings.click()
    await expect(appearance.get_by_role("slider", name="Conversation detail level")).to_have_attribute(
        "aria-valuetext", "Detailed"
    )
    await appearance.get_by_role("button", name="Close settings", exact=True).click()


async def new_mobile_session(page: Page) -> None:
    drawer = page.locator(".sidebar")
    selected = drawer.locator(".session-row.selected")
    previous = await selected.get_attribute("data-session-id") or ""
    require(previous, "No session was selected before creating a new one")
    await page.get_by_role("button", name="Toggle sessions", exact=True).tap()
    await expect(drawer).to_be_visible()
    async with page.expect_response(
        lambda response: response.request.method == "POST" and urlsplit(response.url).path == "/api/sessions",
        timeout=8000,
    ) as creating:
        await drawer.locator(".sidebar-create").get_by_role("button", name="New session", exact=True).tap()
    response = await creating.value
    require(response.ok, "The host refused to create a session")
    created = (await response.json())["session"]["id"]
    require(created != previous, "The host did not create a new session")
    await expect(drawer).to_be_hidden()
    await expect(selected).to_have_attribute("data-session-id", created)
    await expect(page.locator("#composer")).to_be_focused()
    await connected(page)
    await expect(root_conversation(page)).to_have_count(1)
    await expect(root_conversation(page).locator(".entry")).to_have_count(0)


async def inspect_approval(dialog: Locator, path: str) -> None:
    proposed = dialog.get_by_role("region", name="Proposed change", exact=True).locator("code")
    await expect(proposed).to_contain_text(f"+++ b/{path}")
    await expect(proposed).to_contain_text(f"+{LINES[0].strip()}")
    await expect(proposed).to_contain_text(f"+{LINES[-1].strip()}")
    require(
        await dialog.locator(".approval-body").evaluate(
            """element => {
                element.scrollTop = element.scrollHeight;
                const scrolled = element.clientHeight > 0 && element.scrollTop > 0;
                element.scrollTop = 0;
                return scrolled;
            }"""
        ),
        "Long approval preview did not scroll independently",
    )
    require(
        await dialog.evaluate("() => document.documentElement.scrollWidth <= innerWidth"),
        "Browser layout overflowed horizontally",
    )
    for action in ("Allow once", "Deny", "Stop run", "Review later"):
        await dialog.get_by_role("button", name=action, exact=True).tap(trial=True)
    await expect(dialog.get_by_role("button", name="Always allow this tool", exact=True)).to_have_count(0)


async def start_mobile_approval(page: Page, prompt: str) -> Locator:
    await page.locator("#composer").fill(prompt)
    await page.get_by_role("button", name="Send", exact=True).tap()
    dialog = approval_dialog(page)
    await expect(dialog.get_by_role("heading", name="Review write", exact=True)).to_be_focused()
    return dialog


async def review_later(page: Page, dialog: Locator) -> None:
    await dialog.get_by_role("button", name="Review later", exact=True).tap()
    await expect(dialog).to_be_hidden()
    await expect(page.get_by_role("button", name="Review pending approval", exact=True)).to_be_visible()
    await expect(page.locator(".run-status")).to_have_text("Waiting for approval")
    await expect(page.locator("#composer")).to_be_focused()


async def mobile_write(page: Page, workspace: Path, output: Path) -> None:
    posts = watch_submissions(page)
    composer = page.locator("#composer")
    conversation = root_conversation(page)
    await expect(composer).to_have_attribute("enterkeyhint", "enter")
    await composer.fill("unsent draft")
    await composer.press("End")
    await composer.press("Enter")
    require(await composer.input_value() == "unsent draft\n", "Touch Return submitted or lost draft")
    await expect(conversation.locator(".entry.user")).to_have_count(0)
    require(not posts, "Touch Return made a network submission")

    dialog = await start_mobile_approval(page, WRITE)
    path = WRITES[WRITE]
    require(not (workspace / path).exists(), "Native write occurred before browser approval")
    tool = tool_card(conversation, path)
    await expect(tool.locator(".execution-state")).to_have_text("Needs approval")
    duration = tool.locator(".execution-duration")
    await expect(duration).not_to_be_empty()
    frozen = await duration.inner_text()
    await inspect_approval(dialog, path)
    await page.screenshot(path=str(output / "touch-write-approval.png"))
    await review_later(page, dialog)
    # Give a live clock time to tick; deferring review must leave this clock paused.
    await asyncio.sleep(0.5)
    await expect(tool.locator(".execution-state")).to_have_text("Needs approval")
    await expect(duration).to_have_text(frozen)
    await page.get_by_role("button", name="Review pending approval", exact=True).tap()
    await expect(dialog).to_be_visible()
    require(not (workspace / path).exists(), "Review later granted approval or executed the pending write")
    await dialog.get_by_role("button", name="Allow once", exact=True).tap()
    await ready(page, WRITE_REPLY)
    require((workspace / path).read_bytes() == CONTENT.encode(), "Approved native write was not applied exactly")
    tool = await check_tool(conversation, path, "Completed")
    await expect(tool.locator(".execution-approval")).to_have_text("Allowed once")
    require(len(posts) == 1, f"Expected one deliberate touch submission, got {len(posts)}")


async def inspect_mobile_child(page: Page) -> Locator:
    await page.get_by_role("button", name="Team conversations", exact=True).tap()
    team = page.get_by_role("dialog", name="Team conversations", exact=True)
    await team.get_by_role("button", name=re.compile(rf"^Open {WRITER} conversation")).tap()
    await expect(team).to_be_hidden()
    inspector = page.locator(".focused-task-conversation")
    await expect(inspector.get_by_role("heading", name=WRITER, exact=True)).to_be_visible()
    await check_tool(inspector.locator(".conversation"), WRITES[CHILD], "Needs approval")
    return inspector


async def mobile_child(page: Page, workspace: Path, output: Path, *, deny: bool) -> None:
    dialog = await start_mobile_approval(page, DELEGATE)
    identity = dialog.locator(".request-identity")
    await expect(identity).to_contain_text(WRITER)
    await expect(identity).to_contain_text("depth 1")
    await review_later(page, dialog)
    inspector = await inspect_mobile_child(page)
    child = inspector.locator(".conversation")
    require(not (workspace / WRITES[CHILD]).exists(), "Child write occurred before browser approval")
    if deny:
        await page.set_viewport_size({"width": 390, "height": 560})
        await page.get_by_role("button", name="Review pending approval", exact=True).tap()
        await expect(identity).to_contain_text(WRITER)
        await expect(identity).to_contain_text("depth 1")
        await inspect_approval(dialog, WRITES[CHILD])
        await page.screenshot(path=str(output / "touch-child-approval.png"))
        await dialog.get_by_role("button", name="Deny", exact=True).tap()
        await expect(child.locator(".entry.assistant").get_by_text(CHILD_REPLY, exact=True)).to_be_visible()
        await expect(tool_card(child, WRITES[CHILD]).locator(".execution-approval")).to_have_text("Denied")
    else:
        await page.get_by_role("button", name="Stop this agent", exact=True).tap()
        await expect(stopped_message(child)).to_be_attached()
        await expect(page.get_by_role("button", name="Review pending approval", exact=True)).to_be_hidden()

    await inspector.get_by_role("button", name="Assistant", exact=True).tap()
    await expect(inspector).to_be_hidden()
    await ready(page, ROOT_REPLY)
    root = root_conversation(page)
    await expect(root.locator(".entry.assistant").filter(has_text=CHILD_REPLY)).to_have_count(0)
    await expect(stopped_message(root)).to_have_count(0)
    require(not (workspace / WRITES[CHILD]).exists(), "Deny or child-only Stop allowed the native write")
    require((workspace / WRITES[WRITE]).read_bytes() == CONTENT.encode(), "Child action changed the approved file")


async def mobile_stop(page: Page, workspace: Path) -> None:
    dialog = await start_mobile_approval(page, STOP)
    await review_later(page, dialog)
    composer = page.locator("#composer")
    form = page.locator(".chat-composer")
    queue = page.locator(".native-queue")
    queued = "queued read after current run"
    await composer.fill(queued)
    await form.get_by_role("button", name="Queue", exact=True).tap()
    await expect(queue.locator("summary")).to_contain_text("1 pending")
    await queue.locator("summary").tap()
    await expect(queue.get_by_text(queued, exact=True)).to_be_visible()
    await queue.get_by_role("button", name="Pause queue", exact=True).tap(trial=True)

    draft = "retain this draft while stopping"
    await composer.fill(draft)
    await form.get_by_role("button", name="Stop run", exact=True).tap()
    conversation = root_conversation(page)
    stopped = stopped_message(conversation)
    await expect(stopped).to_be_attached()
    await ready(page)
    await expect(composer).to_have_value(draft)
    await expect(composer).to_be_focused()
    await expect(queue.locator("summary")).to_contain_text("Paused")
    await expect(queue.get_by_role("button", name="Resume queue", exact=True)).to_be_visible()
    await expect(queue.get_by_text(queued, exact=True)).to_be_visible()
    await expect(conversation.locator(".entry.user")).to_have_count(1)
    await reveal_activity(stopped, conversation.locator('.chat-activity-group[data-category="notices"]'))
    require(not (workspace / WRITES[STOP]).exists(), "Stopping a pending approval executed a native write")


async def exercise(browser: Browser, url: str, workspace: Path, output: Path, release_stream: asyncio.Event) -> None:
    errors: list[str] = []
    expect.set_options(timeout=10_000)
    context = await browser.new_context(viewport={"width": 1280, "height": 900})
    try:
        page = await open_workspace(context, url, errors)
        await desktop_read(page, url, output, release_stream)
        await desktop_preferences(page)
    finally:
        release_stream.set()
        await context.close()

    context = await browser.new_context(
        viewport={"width": 390, "height": 780}, is_mobile=True, has_touch=True, device_scale_factor=1
    )
    try:
        page = await open_workspace(context, url, errors)
        await new_mobile_session(page)
        await mobile_write(page, workspace, output)
        await new_mobile_session(page)
        await mobile_child(page, workspace, output, deny=True)
        await new_mobile_session(page)
        await mobile_child(page, workspace, output, deny=False)
        await new_mobile_session(page)
        await mobile_stop(page, workspace)
    finally:
        await context.close()
    require(not errors, f"Browser page or asset errors: {errors}")


async def main(binary: Path, runtime_python: Path, executable: str, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ngn-browser-") as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        workspace.mkdir()
        (workspace / "hello.txt").write_text(f"{HELLO}\n", encoding="utf-8")
        release_stream = asyncio.Event()
        application = web.Application()
        application[RELEASE_STREAM] = release_stream
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
            environment = dict(
                os.environ,
                NGN_BROWSER_FIXTURE_KEY="local-fixture-not-a-provider-credential",
                NGN_WIRE_LOG="false",
                XDG_CONFIG_HOME=str(root / "xdg-config"),
                XDG_DATA_HOME=str(root / "xdg-data"),
            )
            for inherited in ("PYTHONPATH", "NGN_DATA_DIR"):
                environment.pop(inherited, None)
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
                        await exercise(browser, url, workspace, output, release_stream)
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
        "PASS: installed React workspace in Chromium; streaming Markdown, history replay, panel and appearance preferences, "
        "touch Return, native approvals, child isolation and stop, queue pause, assets"
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
