"""Real ngn clients against the same long-lived Python engine and a loopback-only model."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import pty
import re
import select
import sqlite3
import subprocess
import sys
import tempfile
import termios
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager, closing, suppress
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from aiohttp import ClientSession, web
from playwright.async_api import Browser, async_playwright, expect
from pty_smoke import COMPOSER_TITLE, WorkspaceReady
from smoke_support import approval_dialog, connected, root_conversation
from steering_clients_smoke import Fixture, browser_checks

TIMEOUT = 20
CRASH_PROMPT = "hold for engine crash"
CRASH_FILE = "must-not-replay.txt"
DELEGATE = "delegate for scoped inspection"
CHILD_PROMPT = "child-only input for inspection"
CHILD_TEXT = "The child response stayed in its own conversation."
ROOT_TEXT = "The root completed the delegation."
PTY_DELEGATE = "delegate for terminal stop"
PTY_CHILD_PROMPT = "terminal child inspection input"
PTY_ROOT_TEXT = "The root survived stopping its child."
IMPORT_PROMPT = "Prompt saved by ngn 0.100.46"
IMPORT_REPLY = "This response was saved by the retired Rust host."


class EngineFixture(Fixture):
    crash_requests = 0

    def __init__(self) -> None:
        super().__init__()
        self.release_child = asyncio.Event()
        self.release_pty_child = asyncio.Event()

    async def reply(
        self, request: web.Request, delta: dict[str, object], finish: str, *, hold: asyncio.Event | None = None
    ) -> web.StreamResponse:
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        with suppress(ConnectionResetError):
            if hold is not None:
                await hold.wait()
            for content, completion in [(delta, None), ({}, finish)]:
                frame = {"choices": [{"index": 0, "delta": content, "finish_reason": completion}]}
                await response.write(("data: " + json.dumps(frame) + "\n\n").encode())
            await response.write(b"data: [DONE]\n\n")
        return response

    async def handle(self, request: web.Request) -> web.StreamResponse:
        if request.method == "POST":
            messages = (await request.json())["messages"]
            latest = messages[-1].get("content")
            if latest in (CRASH_PROMPT, DELEGATE, PTY_DELEGATE):
                crash = latest == CRASH_PROMPT
                terminal = latest == PTY_DELEGATE
                self.crash_requests += int(crash)
                return await self.reply(
                    request,
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "crash-write" if crash else "inspect-delegation",
                                "type": "function",
                                "function": {
                                    "name": "write" if crash else "to_agent",
                                    "arguments": json.dumps(
                                        {"path": CRASH_FILE, "content": "must not be written\n"}
                                        if crash
                                        else {
                                            "prompt": PTY_CHILD_PROMPT if terminal else CHILD_PROMPT,
                                            "title": "Terminal fixture" if terminal else "Inspection fixture",
                                        }
                                    ),
                                },
                            }
                        ]
                    },
                    "tool_calls",
                )
            if latest == CHILD_PROMPT:
                return await self.reply(request, {"content": CHILD_TEXT}, "stop", hold=self.release_child)
            if latest == PTY_CHILD_PROMPT:
                return await self.reply(
                    request, {"content": "Should not complete"}, "stop", hold=self.release_pty_child
                )
            if any(message.get("content") == DELEGATE for message in messages):
                return await self.reply(request, {"content": ROOT_TEXT}, "stop")
            if any(message.get("content") == PTY_DELEGATE for message in messages):
                return await self.reply(request, {"content": PTY_ROOT_TEXT}, "stop")
        return await super().handle(request)


def write_object(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def environment(root: Path, port: int) -> dict[str, str]:
    config, home, data = (root / label for label in ("config", "home", "data"))
    (config / "ngn").mkdir(parents=True)
    home.mkdir()
    # Keep personal providers, plugins, authentication and sessions out of the
    # fixture even on a machine with existing nagents state.
    env = {key: value for key, value in os.environ.items() if not key.startswith("NGN_")}
    env.update(
        HOME=str(home),
        XDG_CONFIG_HOME=str(config),
        XDG_DATA_HOME=str(data),
        NGN_ENGINE_PYTHON=sys.executable,
        NGN_ENGINE_SMOKE_KEY="fixture-not-a-provider-credential",
    )
    write_object(
        config / "ngn/providers.json",
        {
            "version": 2,
            "revision": "e" * 64,
            "active": "fixture",
            "providers": {
                "fixture": {
                    "kind": "openai_compatible",
                    "base_url": f"http://127.0.0.1:{port}/v1",
                    "api": "chat_completions",
                    "api_key_env": "NGN_ENGINE_SMOKE_KEY",
                }
            },
        },
    )
    write_object(config / "ngn/config.json", {"provider_id": "fixture", "model": "client-fixture"})
    return env


def state_path(env: dict[str, str], workspace: Path) -> Path:
    key = hashlib.sha256(os.fsencode(workspace.resolve())).hexdigest()
    return Path(env["XDG_DATA_HOME"]) / "ngn/engine/workspaces" / key


@asynccontextmanager
async def process(argv: list[str], env: dict[str, str], log: Path) -> AsyncIterator[asyncio.subprocess.Process]:
    with log.open("wb") as output:
        child = await asyncio.create_subprocess_exec(
            *argv,
            env=env,
            stdout=output,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
        )
        try:
            yield child
        finally:
            if child.returncode is None:
                child.terminate()
                try:
                    await asyncio.wait_for(child.wait(), 10)
                except TimeoutError:
                    child.kill()
                    await child.wait()


async def wait_for[T](read: Callable[[], T | None], child: asyncio.subprocess.Process, failure: str) -> T:
    async with asyncio.timeout(TIMEOUT):
        while child.returncode is None:
            value = read()
            if value is not None:
                return value
            await asyncio.sleep(0.04)
    raise AssertionError(f"{failure}: subprocess exited {child.returncode}")


@asynccontextmanager
async def engine(
    env: dict[str, str], workspace: Path, log: Path
) -> AsyncIterator[tuple[asyncio.subprocess.Process, Any]]:
    args = [sys.executable, "-I", "-B", "-m", "nagents.engine.cli", "engine", "-C", str(workspace)]
    async with process(args, env, log) as child:
        registration = state_path(env, workspace) / "engine.json"

        def own_record() -> Any:
            if not registration.is_file():
                return None
            record = json.loads(registration.read_bytes())
            return record if record["pid"] == child.pid else None

        yield child, await wait_for(own_record, child, "Python engine did not register")


async def browser_phase(
    browser: Browser,
    binary: Path,
    env: dict[str, str],
    workspace: Path,
    fixture: EngineFixture,
    output: Path,
    checks: set[str],
) -> None:
    async with AsyncExitStack() as stack:
        original, registered = await stack.enter_async_context(engine(env, workspace, output / "engine-initial.log"))
        log = output / "gateway.log"
        gateway = await stack.enter_async_context(
            process([str(binary), "-C", str(workspace), "serve", "--port", "0"], env, log)
        )

        def location() -> str | None:
            match = re.search(r"Open (http://127\.0\.0\.1:\d+)", log.read_text(encoding="utf-8"))
            return match[1] if match else None

        url = await wait_for(location, gateway, "Rust browser gateway did not start")
        if "browser" in checks:
            await browser_checks(browser, url, workspace, fixture, output)
        if "tasks" in checks:
            await inspect_child(browser, url, fixture, output)
        if "restart" not in checks:
            return
        page = await browser.new_page()
        try:
            await page.goto(url)
            await connected(page)
            selected = page.locator(".session-row.selected")
            previous = await selected.get_attribute("data-session-id") or ""
            await page.locator(".sidebar-create").get_by_role("button", name="New session", exact=True).click()
            await expect(selected).not_to_have_attribute("data-session-id", previous)
            await connected(page)
            await page.locator("#composer").fill(CRASH_PROMPT)
            await page.get_by_role("button", name="Send", exact=True).click()
            dialog = approval_dialog(page)
            await expect(dialog).to_be_visible()
            await expect(dialog).to_contain_text(CRASH_FILE)
            assert not (workspace / CRASH_FILE).exists() and fixture.crash_requests == 1
            async with (
                ClientSession(headers={"Authorization": f"Bearer {registered['token']}"}) as client,
                client.get(registered["url"] + "/api/v1/state") as response,
            ):
                response.raise_for_status()
                pending = (await response.json())["approvals"][0]
            original.kill()
            await original.wait()
            await expect(page.locator(".topbar .native-connection")).to_have_text("Reconnecting…")
            replacement, renewed = await stack.enter_async_context(
                engine(env, workspace, output / "engine-restarted.log")
            )
            assert renewed["token"] != registered["token"] and replacement.pid != original.pid
            await connected(page)
            conversation = root_conversation(page)
            await expect(conversation.locator('.execution-state[data-state="Interrupted"]')).to_be_visible()
            await conversation.locator('details[data-category="notices"] > summary').click()
            await expect(
                conversation.get_by_text(re.compile("^Interrupted before the turn finished(?: —|$)"))
            ).to_be_visible()
            await expect(dialog).to_be_hidden()
            async with (
                ClientSession(headers={"Authorization": f"Bearer {renewed['token']}"}) as client,
                client.post(
                    renewed["url"] + "/api/v1/approvals/" + pending["id"],
                    json={"run_id": pending["run_id"], "call_id": pending["call"]["call_id"], "allow": True},
                ) as response,
            ):
                assert response.status == 409
            assert not (workspace / CRASH_FILE).exists() and fixture.crash_requests == 1
            await page.screenshot(path=str(output / "python-recovered-without-replay.png"))
            gateway.terminate()
            await asyncio.wait_for(gateway.wait(), 10)
            assert gateway.returncode == 0 and replacement.returncode is None
        finally:
            await page.close()


async def inspect_child(browser: Browser, url: str, fixture: EngineFixture, output: Path) -> None:
    page = await browser.new_page(viewport={"width": 1360, "height": 880})
    sockets: list[str] = []
    page.on("websocket", lambda socket: sockets.append(socket.url))
    try:
        await page.goto(url)
        await connected(page)
        selected = page.locator(".session-row.selected")
        previous = await selected.get_attribute("data-session-id") or ""
        await page.locator(".sidebar-create").get_by_role("button", name="New session", exact=True).click()
        await expect(selected).not_to_have_attribute("data-session-id", previous)
        await connected(page)
        await page.locator("#composer").fill(DELEGATE)
        await page.get_by_role("button", name="Send", exact=True).click()
        inspect = page.get_by_role("button", name=re.compile("Open Inspection fixture conversation"))
        await expect(inspect).to_be_visible()
        socket_count = len(sockets)
        await inspect.click()
        child = page.get_by_role("region", name="Inspection fixture conversation", exact=True)
        await expect(child.get_by_text(CHILD_PROMPT, exact=True)).to_be_visible()
        fixture.release_child.set()
        await expect(child.get_by_text(CHILD_TEXT, exact=True)).to_be_visible()
        assert len(sockets) == socket_count, "Inspection opened another WebSocket"
        identity = inspect.locator(".team-agent-session-id")
        status = inspect.locator(".team-state")
        await expect(identity).to_have_attribute("title", re.compile("^child-[a-f0-9]{32}$"))
        await expect(identity).to_have_css("text-overflow", "ellipsis")
        await expect(status).to_have_text("Done")
        identity_box, status_box = await identity.bounding_box(), await status.bounding_box()
        assert identity_box and status_box and identity_box["x"] + identity_box["width"] <= status_box["x"], (
            "A long child ID covered the agent's state"
        )
        await page.screenshot(path=str(output / "python-child-inspection.png"))
        await child.get_by_role("button", name="Assistant", exact=True).click()
        await expect(root_conversation(page).get_by_text(ROOT_TEXT, exact=True).first).to_be_visible()
        await expect(root_conversation(page).get_by_text(CHILD_TEXT, exact=True)).to_have_count(0)
    finally:
        fixture.release_child.set()
        await page.close()


def pty_phase(
    binary: Path,
    env: dict[str, str],
    workspace: Path,
    registered: Any,
    fixture: EngineFixture,
    release: Callable[[], None],
    output: Path,
) -> None:
    master, slave = pty.openpty()
    termios.tcsetwinsize(slave, (42, 132))
    original = termios.tcgetattr(slave)
    child = subprocess.Popen(
        [str(binary), "-C", str(workspace), "--no-animations"],
        env=env,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
    )
    os.close(slave)
    terminal = bytearray()

    def drain() -> None:
        if select.select([master], [], [], 0.04)[0]:
            with suppress(OSError):
                terminal.extend(os.read(master, 65536))

    def clean(at: int = 0) -> bytes:
        return b" ".join(re.sub(rb"\x1b\[[0-?]*[ -/]*[@-~]", b" ", terminal[at:]).split())

    def until[T](read: Callable[[], T | None], label: str) -> T:
        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline and child.poll() is None:
            drain()
            value = read()
            if value:
                return value
        raise AssertionError(f"PTY did not reach {label}: {clean()[-1800:]!r}")

    def call(path: str, body: dict[str, object] | None = None) -> Any:
        headers = {"Authorization": f"Bearer {registered['token']}", "Content-Type": "application/json"}
        data = json.dumps(body).encode() if body is not None else None
        with urlopen(Request(registered["url"] + "/api/v1" + path, data=data, headers=headers), timeout=3) as response:
            return json.load(response)

    try:
        ready = WorkspaceReady(master)
        until(lambda: ready.observe(terminal, COMPOSER_TITLE in clean()), "resized composer")
        os.write(master, b"pty batch\r")
        until(fixture.pty_started["pty batch"].is_set, "loopback model")
        active = until(lambda: call("/state")["runs"], "durable run")[0]
        for prompt in ("pty first steering", "pty second steering"):
            os.write(master, f"/steer {prompt}\r".encode())
        steering = f"/runs/{active['id']}/steer"
        until(
            lambda: [entry for entry in call(steering)["receipts"] if entry["status"] == "pending"][1:],
            "two scoped pending receipts",
        )
        release()
        at = len(terminal)
        until(lambda: b"y allow" in clean(at), "actual approval prompt")
        approved = call("/state")["approvals"][0]
        assert not (workspace / "pty-batch-0.txt").exists()
        decision = {"run_id": approved["run_id"], "call_id": approved["call"]["call_id"], "allow": True}
        call(f"/approvals/{approved['id']}", decision)
        try:
            call(f"/approvals/{approved['id']}", decision)
        except HTTPError as error:
            assert error.code == 409
        else:
            raise AssertionError("A second approval won")
        until(lambda: b"pty-batch-1.txt" in clean(at), "second tool")
        assert (workspace / "pty-batch-0.txt").read_text() == "tool 0\n"
        assert not (workspace / "pty-batch-1.txt").exists()
        until(lambda: b"Allow write to pty-batch-1.txt?" in clean(at), "second approval prompt")
        # The TUI ignores keys for 300 ms after a prompt appears so typing cannot approve.
        time.sleep(0.6)
        os.write(master, b"\x19")  # Ctrl+Y, not a direct API decision this time.
        until(
            lambda: [entry for entry in call(steering)["receipts"] if entry["status"] == "applied"][1:],
            "persistent steering at the model boundary",
        )
        until(lambda: b"both persisted steering" in clean(at), "rendered completion")
        assert "pty" in fixture.checked
        session = active["session_id"]

        def outcome(run_id: str) -> str | None:
            events = call(f"/sessions/{session}/history")["events"]
            return next(
                (
                    entry["event"]["status"]
                    for entry in events
                    if entry["run_id"] == run_id and entry["event"]["type"] == "run.finished"
                ),
                None,
            )

        assert until(lambda: outcome(active["id"]), "steering turn completed") == "completed"
        at = len(terminal)
        os.write(master, b"\x1b[18~")  # F7
        until(lambda: b"Steering receipts" in clean(at), "receipt panel")
        # A lone Escape on a PTY can be held as the start of a key sequence.
        # Toggle F7 with an unambiguous complete CSI; the prompt follows it
        # in the same ordered input stream.
        os.write(master, b"\x1b[18~" + PTY_DELEGATE.encode() + b"\r")

        def delegated(status: str) -> Any:
            return next((task for task in call(f"/sessions/{session}/tasks") if task["status"] == status), None)

        task = until(lambda: delegated("running"), "exact running delegation")
        assert task["title"] == "Terminal fixture" and task["parent_session_id"] == session
        at = len(terminal)
        os.write(master, b"\x1b[15~")  # F5
        # The picker lists children as their task records load; Enter on an
        # empty list would close it instead of inspecting the delegation.
        until(lambda: b"Enter inspect" in clean(at) and b"Terminal fixture" in clean(at), "agent picker")
        at = len(terminal)
        os.write(master, b"\r")
        until(lambda: PTY_CHILD_PROMPT.encode() in clean(at), "isolated child history")
        os.write(master, b"\x18")  # Ctrl+X stops the selected activation only.
        stopped = until(lambda: delegated("cancelled"), "exact child cancellation")
        assert stopped["run_id"] == task["run_id"]

        assert until(lambda: outcome(task["root_run_id"]), "root completed independently") == "completed"
        os.write(master, b"\x11")  # Ctrl+Q
        deadline = time.monotonic() + 10
        while child.poll() is None and time.monotonic() < deadline:
            drain()
        assert child.wait(timeout=2) == 0
        mask = termios.ICANON | termios.ECHO | termios.ISIG
        assert termios.tcgetattr(master)[3] & mask == original[3] & mask
    finally:
        (output / "python-tui.txt").write_bytes(clean())
        if child.poll() is None:
            child.kill()
            child.wait()
        os.close(master)


async def private_run(binary: Path, env: dict[str, str], workspace: Path, output: Path) -> None:
    argv = [str(binary), "--standalone", "--engine-demo", "-C", str(workspace), "run", "--json", "hello offline"]
    # stdout is structured output only; the Python readiness token is consumed
    # internally on a separate owner-only pipe.
    with (output / "standalone.log").open("wb") as diagnostics:
        child = await asyncio.create_subprocess_exec(
            *argv, env=env, stdout=asyncio.subprocess.PIPE, stderr=diagnostics, stdin=asyncio.subprocess.DEVNULL
        )
        try:
            records, _ = await asyncio.wait_for(child.communicate(), TIMEOUT)
        finally:
            if child.returncode is None:
                child.kill()
                await child.wait()
    assert child.returncode == 0, "Private engine run failed; see standalone.log"
    result = [json.loads(line) for line in records.splitlines()]
    assert any(frame["event"]["type"] == "text.completed" for frame in result)
    assert result[-1]["event"] == {"type": "run.finished", "status": "completed", "error": ""}
    assert not (state_path(env, workspace) / "engine.json").exists()
    write_object(output / "standalone-events.json", result)


def legacy_database(env: dict[str, str], workspace: Path, prompt: str, reply: str) -> Path:
    """Write a schema-5 `host.sqlite3` where ngn <= 0.100.46 kept this workspace's history."""
    digest = hashlib.sha256(str(workspace.resolve()).encode()).hexdigest()
    folder = Path(env["XDG_DATA_HOME"]) / "ngn-rust" / "workspaces" / digest
    folder.mkdir(parents=True, mode=0o700)
    (folder / "host.lock").touch(mode=0o600)
    database = folder / "host.sqlite3"
    with closing(sqlite3.connect(database)) as db, db:
        db.executescript(
            "PRAGMA user_version=5;"
            "CREATE TABLE sessions(id TEXT PRIMARY KEY, title TEXT, updated_at_ms INTEGER);"
            "CREATE TABLE runs(id TEXT PRIMARY KEY, session_id TEXT, request_id TEXT, prompt TEXT, status TEXT);"
            "CREATE TABLE events(sequence INTEGER PRIMARY KEY, run_id TEXT, session_id TEXT, "
            "timestamp_ms INTEGER, payload TEXT);"
            "CREATE TABLE subagents(child_session_id TEXT PRIMARY KEY);"
            "INSERT INTO sessions VALUES ('legacy', 'Earlier ngn session', 1700000000000);"
        )
        db.execute("INSERT INTO runs VALUES ('r1', 'legacy', 'old-1', ?, 'completed')", (prompt,))
        for payload in (
            {"event": "run_started", "prompt": prompt},
            {"event": "text_completed", "text": reply},
            {"event": "run_finished", "status": "completed", "error": ""},
        ):
            db.execute(
                "INSERT INTO events(run_id, session_id, timestamp_ms, payload) VALUES ('r1', 'legacy', 1700000000000, ?)",
                (json.dumps(payload),),
            )
    database.chmod(0o600)
    return database


async def import_phase(browser: Browser, binary: Path, env: dict[str, str], root: Path, output: Path) -> Path:
    """An upgraded user's old Rust host history appears read-only on first start."""
    workspace = root / "import-workspace"
    workspace.mkdir()
    legacy = legacy_database(env, workspace, IMPORT_PROMPT, IMPORT_REPLY)
    original = legacy.read_bytes()
    log = output / "host-import-gateway.log"
    async with process([str(binary), "-C", str(workspace), "serve", "--port", "0"], env, log) as gateway:

        def location() -> str | None:
            match = re.search(r"Open (http://127\.0\.0\.1:\d+)", log.read_text(encoding="utf-8"))
            return match[1] if match else None

        page = await browser.new_page()
        try:
            await page.goto(await wait_for(location, gateway, "Archive browser gateway did not start"))
            await connected(page)
            await page.locator(".session-row[data-session-id^='host-archive-']").click()
            await connected(page)
            await expect(
                page.get_by_text("Read-only Rust host archive. Start a new session to send messages.")
            ).to_be_visible()
            conversation = root_conversation(page)
            await expect(conversation.get_by_text(IMPORT_PROMPT, exact=True)).to_be_visible()
            await expect(conversation.get_by_text(IMPORT_REPLY, exact=True)).to_be_visible()
            await expect(page.locator("#composer")).to_have_count(0)
            await page.screenshot(path=str(output / "python-host-archive.png"))
        finally:
            await page.close()
    assert legacy.read_bytes() == original, "Importing modified the old Rust host database"
    return legacy


async def main(binary: Path, output: Path, chromium: str, checks: set[str]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    fixture = EngineFixture()
    app = web.Application()
    app.router.add_route("*", "/v1/{tail:.*}", fixture.handle)
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=0.1)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    try:
        with tempfile.TemporaryDirectory(prefix="ngn-python-smoke-") as directory:
            root = Path(directory).resolve()
            env = environment(root, runner.addresses[0][1])
            browser_workspace, pty_workspace, private_workspace = (
                root / name for name in ("browser", "pty", "private")
            )
            for workspace in (browser_workspace, pty_workspace, private_workspace):
                workspace.mkdir()
            if "private" in checks:
                await private_run(binary, env, private_workspace, output)
            expected_legacy: set[Path] = set()
            if checks & {"browser", "restart", "tasks", "import"}:
                async with async_playwright() as playwright:
                    browser = await playwright.chromium.launch(
                        executable_path=chromium or None, args=["--no-sandbox", "--disable-dev-shm-usage"]
                    )
                    try:
                        if checks & {"browser", "restart", "tasks"}:
                            await browser_phase(browser, binary, env, browser_workspace, fixture, output, checks)
                        if "import" in checks:
                            expected_legacy.add(await import_phase(browser, binary, env, root, output))
                    finally:
                        await browser.close()
            if "pty" in checks:
                async with engine(env, pty_workspace, output / "engine-pty.log") as (owner, registered):
                    loop = asyncio.get_running_loop()

                    def release() -> None:
                        loop.call_soon_threadsafe(fixture.release_pty["pty batch"].set)

                    await asyncio.to_thread(pty_phase, binary, env, pty_workspace, registered, fixture, release, output)
                    assert owner.returncode is None, "An attached terminal stopped someone else's engine"
            assert set(root.rglob("host.sqlite3")) == expected_legacy, "The engine created Rust host state"
        write_object(
            output / "evidence.json",
            {
                "clients": sorted(fixture.checked),
                "crash_model_requests": fixture.crash_requests,
                "checked": sorted(
                    checks
                    | {"no Rust host state created"}
                    | ({"terminal child inspection and cancellation"} if "pty" in checks else set())
                ),
            },
        )
        print(f"PASS: {', '.join(sorted(checks))} against the Python engine")
    finally:
        fixture.release_browser.set()
        fixture.release_child.set()
        fixture.release_pty_child.set()
        for event in fixture.release_pty.values():
            event.set()
        await runner.cleanup()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--browser", default="")
    choices = ["private", "browser", "restart", "pty", "tasks", "import"]
    parser.add_argument("--check", action="append", choices=choices)
    args = parser.parse_args()
    asyncio.run(
        main(
            args.binary.resolve(),
            args.output.resolve(),
            args.browser,
            set(args.check or choices),
        )
    )
