"""Actual stdio MCP and Python generation replacement in the shared harness."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from nagents.events import DoneEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.events import ToolResultEvent
from nagents.harness import ApprovalRequest
from nagents.harness import load_config
from nagents.mcp import MCPServerConfig
from tests.support.providers import collect
from tests.support.providers import setup_harness

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.types import Message
    from tests.support.providers import FakeProvider


@pytest.fixture(autouse=True)
def isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))


def server(tmp_path: Path) -> MCPServerConfig:
    source = tmp_path / "server.py"
    source.write_text("""import json, os, sys
from pathlib import Path
version = Path("version").read_text()
with Path("starts").open("a") as f: f.write(str(os.getpid()) + "\\n")
try:
    for line in sys.stdin:
        request = json.loads(line)
        if "id" not in request: continue
        method = request["method"]
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fixture", "version": version}}
        elif method == "tools/list":
            result = {"tools": [{"name": "echo", "description": version, "inputSchema": {"type": "object", "properties": {"payload": {"type": ["object", "null"], "additionalProperties": True}}}}]}
            if Path("schema.json").exists(): result["tools"][0]["inputSchema"] = json.loads(Path("schema.json").read_text())
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": version + ":" + json.dumps(request["params"]["arguments"], sort_keys=True)}]}
        else: result = {}
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
finally:
    with Path("stops").open("a") as f: f.write(str(os.getpid()) + "\\n")
""")
    (tmp_path / "version").write_text("one")
    return MCPServerConfig("fixture", sys.executable, ["-u", str(source)], cwd=str(tmp_path))


def plugin(path: Path, version: str) -> None:
    path.write_text(f"""from nagents import AgentPlugin
from pathlib import Path
VERSION = {version!r}
class Hooks(AgentPlugin):
    async def before_model(self, context, request):
        request.messages.append(__import__("nagents").Message(role="system", content="plugin:" + VERSION))
        return request
    async def aclose(self):
        with Path({str(path.parent / "closed")!r}).open("a") as f: f.write(VERSION + "\\n")
def setup(harness):
    async def current_session():
        return VERSION + ":" + harness.session_id
    harness.agent.register_tool(current_session)
    harness.commands.register("custom", VERSION, prompt=VERSION)
    harness.commands.register_alias("custom-alias", "custom")
    return Hooks()
""")


@pytest.mark.requires_posix
@pytest.mark.asyncio
async def test_every_response_reloads_mcp_plugins_and_final_with_pinned_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configuration = tmp_path / "trusted.json"
    extension = tmp_path / "extension.py"
    plugin(extension, "one")
    mcp = server(tmp_path)

    def configure(enabled: bool = True) -> None:
        configuration.write_text(
            json.dumps(
                {
                    "plugins": [str(extension) + ":setup"] if enabled else [],
                    "mcp_servers": {"fixture": {"command": mcp.command, "args": mcp.args, "cwd": str(tmp_path)}}
                    if enabled
                    else {},
                }
            )
        )

    configure()
    calls = 0

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        nonlocal calls
        calls += 1
        if calls == 1:
            assert "mcp__fixture__echo" in provider.schemas[-1]
            assert messages[-1].content == "plugin:one"
            plugin(extension, "two")
            (tmp_path / "version").write_text("two")
            yield ToolCallEvent(id="mcp-one", name="mcp__fixture__echo", arguments={"payload": {"a-b": None}})
            yield ToolCallEvent(id="plugin-one", name="current_session", arguments={})
        elif calls == 2:
            assert messages[-1].content == "plugin:two"
            configure(False)
            yield ToolCallEvent(id="mcp-two", name="mcp__fixture__echo", arguments={"payload": None})
            yield ToolCallEvent(id="plugin-two", name="current_session", arguments={})
        else:
            assert "mcp__fixture__echo" not in provider.schemas[-1]
            assert "current_session" not in provider.schemas[-1]
            # Final response must install changes even without another request.
            plugin(extension, "tri")
            (tmp_path / "version").write_text("tri")
            configure()
            yield TextDoneEvent(text="finished")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    harness.config.resource_paths = (configuration,)
    approvals: list[str] = []

    async def approve(request: ApprovalRequest) -> bool:
        approvals.append(request.tool)
        return True

    harness.approval_handler = approve
    original_session = harness.session_id
    try:
        events = await collect(harness)
        results = [event for event in events if isinstance(event, ToolResultEvent)]
        assert [(event.name, event.error) for event in results] == [
            ("mcp__fixture__echo", None),
            ("current_session", None),
        ] * 2
        assert results[0].result == 'one:{"payload": {"a-b": null}}'
        assert results[1].result == "one:" + original_session
        assert results[2].result == 'two:{"payload": null}'
        assert results[3].result == "two:" + original_session
        assert harness.session_id == original_session
        assert len(approvals) == 4
        assert isinstance(events[-1], DoneEvent)
        assert harness.agent.tool_registry.get("mcp__fixture__echo").description == "tri"  # type: ignore[union-attr]
        assert harness.commands.get("custom-alias").description.startswith("Alias for /custom. tri")  # type: ignore[union-attr]
        assert (
            len((tmp_path / "starts").read_text().splitlines()) == 5
        )  # initialization, entry, populated responses and completed tool batches
    finally:
        await harness.close()
    assert sorted((tmp_path / "starts").read_text().splitlines()) == sorted(
        (tmp_path / "stops").read_text().splitlines()
    )
    assert sorted((tmp_path / "closed").read_text().splitlines()) == ["one", "one", "tri", "two", "two"]


@pytest.mark.requires_posix
@pytest.mark.asyncio
async def test_unchanged_mcp_restarts_after_each_response(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if len(provider.requests) < 3:
            yield ToolCallEvent(id=str(len(provider.requests)), name="mcp__fixture__echo", arguments={})
        else:
            yield TextDoneEvent(text="finished")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    harness.config.mcp_servers = (server(tmp_path),)

    async def approve(request: ApprovalRequest) -> bool:
        return True

    harness.approval_handler = approve
    try:
        events = await collect(harness)
        assert not [event.error for event in events if isinstance(event, ToolResultEvent) and event.error]
        assert len((tmp_path / "starts").read_text().splitlines()) == 7
    finally:
        await harness.close()
    assert sorted((tmp_path / "starts").read_text().splitlines()) == sorted(
        (tmp_path / "stops").read_text().splitlines()
    )


@pytest.mark.requires_posix
@pytest.mark.asyncio
async def test_failed_replacement_retains_tools_aliases_and_host_infrastructure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extension = tmp_path / "extension.py"
    plugin(extension, "one")

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        extension.write_text(
            'def setup(harness):\n    harness.agent.register_tool(lambda: "bad", name="current_session")\n    harness.agent.tool_executor = object()\n'
        )
        yield TextDoneEvent(text="done")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    harness.config.plugins = (str(extension) + ":setup",)
    executor = harness.agent.tool_executor
    try:
        await collect(harness)
        assert harness.agent.tool_executor is executor
        assert "host-owned agent.tool_executor" in harness.resources.last_error
        assert harness.commands.get("custom-alias") is not None
        definition = harness.agent.tool_registry.get("current_session")
        assert definition is not None and definition.func is not None
        assert await definition.func() == "one:" + harness.session_id
    finally:
        await harness.close()


@pytest.mark.requires_posix
@pytest.mark.asyncio
async def test_cancel_during_approval_closes_retained_mcp_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        yield ToolCallEvent(id="mcp", name="mcp__fixture__echo", arguments={})

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    harness.config.mcp_servers = (server(tmp_path),)
    waiting = asyncio.Event()

    async def approve(request: ApprovalRequest) -> bool:
        waiting.set()
        await asyncio.Event().wait()
        return True

    harness.approval_handler = approve
    worker = asyncio.create_task(collect(harness))
    try:
        await asyncio.wait_for(waiting.wait(), 10)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
        assert not harness.resources._running
    finally:
        await harness.close()
    assert sorted((tmp_path / "starts").read_text().splitlines()) == sorted(
        (tmp_path / "stops").read_text().splitlines()
    )


def test_mcp_config_trust_validation_and_precedence(tmp_path: Path) -> None:
    project = tmp_path / ".ngn" / "config.json"
    project.parent.mkdir()
    project.write_text(json.dumps({"mcp_servers": {"docs": {"command": "python", "args": ["server.py"], "cwd": ".."}}}))
    with pytest.warns(UserWarning):
        assert load_config(tmp_path).mcp_servers == ()
    config = load_config(tmp_path, trust_project=True)
    assert config.mcp_servers[0].cwd == str(tmp_path)
    assert project.resolve() in config.resource_paths
    project.write_text('{"mcp_servers": {"bad": {"url": "https://example.test"}}}')
    with pytest.raises(ValueError, match="stdio transport"):
        load_config(tmp_path, trust_project=True)


@pytest.mark.requires_posix
@pytest.mark.asyncio
async def test_serve_exposes_mcp_tools_and_reloads_after_final_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    from nagents.harness.providers import ProviderRegistry
    from nagents.web.app import create_app

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        assert "mcp__fixture__echo" in provider.schemas[-1]
        (tmp_path / "version").write_text("web")
        yield TextDoneEvent(text="web response")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    harness.config.mcp_servers = (server(tmp_path),)
    profile = harness.config.provider_profile()
    harness.config.provider = "fixture"
    harness.config.providers = {"fixture": profile}
    harness._built_provider_profile = profile
    harness._built_provider_name = "fixture"
    harness.provider_store.workspace_store.save(
        ProviderRegistry(active="fixture", providers={"fixture": profile}),
        expected=harness.provider_store.workspace_store.load().revision,
    )
    harness.providers = harness.provider_store.load()
    monkeypatch.setenv(profile.key_env, "fixture-key-never-used-for-network")
    assets = tmp_path / "static"
    (assets / "assets").mkdir(parents=True)
    (assets / "index.html").write_text("<html></html>")
    app = create_app(harness.config, assets=assets, harness_factory=lambda _: harness)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765") as client,
    ):
        bootstrap = (await client.get("/api/bootstrap")).json()
        headers = {"Origin": "http://127.0.0.1:8765", "X-Ngn-Token": bootstrap["token"]}
        snapshot = (await client.get("/api/tools", headers=headers)).json()
        assert any(tool["name"] == "mcp__fixture__echo" for tool in snapshot["tools"])
        response = await client.post(
            "/api/run", headers=headers, json={"session_id": harness.session_id, "prompt": "check MCP"}
        )
        assert response.status_code == 200 and "web response" in response.text
        definition = harness.agent.tool_registry.get("mcp__fixture__echo")
        assert definition is not None and definition.description == "web"
    assert sorted((tmp_path / "starts").read_text().splitlines()) == sorted(
        (tmp_path / "stops").read_text().splitlines()
    )


@pytest.mark.requires_posix
@pytest.mark.asyncio
async def test_tui_loads_and_reloads_same_mcp_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from nagents.tui import NagentsApp
    from tests.support.tui import idle
    from tests.support.tui import send

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        assert "mcp__fixture__echo" in provider.schemas[-1]
        (tmp_path / "version").write_text("tui")
        yield TextDoneEvent(text="tui response")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    harness.config.mcp_servers = (server(tmp_path),)
    app = NagentsApp(harness)
    async with app.run_test(size=(100, 32)) as pilot:
        await idle(app, pilot)
        await send(app, pilot, "check MCP")
        await idle(app, pilot)
        definition = harness.agent.tool_registry.get("mcp__fixture__echo")
        assert definition is not None and definition.description == "tui"
    assert sorted((tmp_path / "starts").read_text().splitlines()) == sorted(
        (tmp_path / "stops").read_text().splitlines()
    )


@pytest.mark.requires_posix
@pytest.mark.asyncio
async def test_mcp_handshake_cancellation_reaps_process(tmp_path: Path) -> None:
    from nagents.mcp.client import MCPClient

    script = tmp_path / "blocked.py"
    script.write_text(
        'import os,time\nfrom pathlib import Path\nPath("started").write_text(str(os.getpid()))\ntime.sleep(30)\n'
    )
    client = MCPClient(MCPServerConfig("blocked", sys.executable, [str(script)], cwd=str(tmp_path)))
    task = asyncio.create_task(client.connect())
    async with asyncio.timeout(10):
        while not (tmp_path / "started").exists():
            await asyncio.sleep(0.001)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 10)
    assert client._process is None and client._reader_task is None and client._stderr_task is None


@pytest.mark.requires_posix
@pytest.mark.asyncio
async def test_repeated_refresh_inside_tool_retires_unadvertised_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extension = tmp_path / "repeat.py"
    extension.write_text("""def setup(harness):
    async def refresh_twice():
        await harness.resources.reload()
        await harness.resources.reload()
        return "refreshed"
    harness.agent.register_tool(refresh_twice)
""")

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if len(provider.requests) == 1:
            yield ToolCallEvent(id="refresh", name="refresh_twice", arguments={})
        else:
            assert messages[-1].content == "refreshed"
            yield TextDoneEvent(text="done")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    harness.config.mcp_servers = (server(tmp_path),)
    harness.config.plugins = (str(extension) + ":setup",)

    async def approve(request: ApprovalRequest) -> bool:
        return True

    harness.approval_handler = approve
    try:
        await collect(harness)
        assert len((tmp_path / "starts").read_text().splitlines()) == 7
    finally:
        await harness.close()
    assert sorted((tmp_path / "starts").read_text().splitlines()) == sorted(
        (tmp_path / "stops").read_text().splitlines()
    )


@pytest.mark.requires_posix
@pytest.mark.asyncio
async def test_failed_candidate_cleanup_keeps_working_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extension = tmp_path / "extension.py"
    plugin(extension, "one")

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        extension.write_text("""from nagents import AgentPlugin
class Broken(AgentPlugin):
    async def aclose(self): raise ValueError("cleanup broke")
def setup(harness):
    harness.agent.plugins.append(Broken())
    harness.agent.tool_executor = object()
""")
        yield TextDoneEvent(text="done")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    harness.config.plugins = (str(extension) + ":setup",)
    try:
        events = await collect(harness)
        assert isinstance(events[-1], DoneEvent)
        assert "cleanup also failed" in harness.resources.last_error
        assert harness.commands.get("custom-alias") is not None
    finally:
        await harness.close()


@pytest.mark.requires_posix
@pytest.mark.asyncio
async def test_failed_mcp_candidate_closes_partial_start_and_keeps_previous_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    working = server(tmp_path)

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if len(provider.requests) == 1:
            harness.config.mcp_servers = (working, MCPServerConfig("bad", str(tmp_path / "missing-command")))
            yield ToolCallEvent(id="old", name="mcp__fixture__echo", arguments={})
        else:
            assert messages[-1].content == "one:{}"
            yield TextDoneEvent(text="done")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    harness.config.mcp_servers = (working,)

    async def approve(request: ApprovalRequest) -> bool:
        return True

    harness.approval_handler = approve
    try:
        events = await collect(harness)
        assert isinstance(events[-1], DoneEvent)
        assert "previous tools and MCP connections retained" in harness.resources.last_error
    finally:
        await harness.close()
    assert sorted((tmp_path / "starts").read_text().splitlines()) == sorted(
        (tmp_path / "stops").read_text().splitlines()
    )


@pytest.mark.requires_posix
@pytest.mark.asyncio
async def test_mcp_approval_identity_tracks_captured_configuration_schema_and_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nagents.types import ToolCall

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        yield TextDoneEvent(text="unused")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    configured = server(tmp_path)
    harness.config.mcp_servers = (configured,)

    async def identity() -> str:
        await harness.resources.reload()
        tool = harness.agent.tool_registry.get("mcp__fixture__echo")
        assert tool is not None
        return harness.resources.approval_identity(tool)

    async def approve(request: ApprovalRequest) -> bool:
        return True

    harness.approval_handler = approve
    alternate = tmp_path / "alternate"
    alternate.mkdir()
    (alternate / "version").write_text("one")
    try:
        await harness.initialize()
        first = await identity()
        assert first and await identity() == first
        changed: list[str] = [first]
        harness.config.mcp_servers = (replace(configured, env={"FIXTURE_OWNER": "changed"}),)
        changed.append(await identity())
        harness.config.mcp_servers = (replace(configured, args=[*configured.args, "new-argument"]),)
        changed.append(await identity())
        harness.config.mcp_servers = (replace(configured, cwd=str(alternate)),)
        changed.append(await identity())
        harness.config.mcp_servers = (configured,)
        (tmp_path / "schema.json").write_text(
            json.dumps({"type": "object", "properties": {"foo-bar": {"type": "string"}, "foo_bar": {"type": "string"}}})
        )
        changed.append(await identity())
        result = await harness.agent.tool_executor.execute(
            ToolCall("collision", "mcp__fixture__echo", {"foo-bar": "hyphen", "foo_bar": "underscore"})
        )
        assert result.error is None and result.result == 'one:{"foo-bar": "hyphen", "foo_bar": "underscore"}'
        (tmp_path / "version").write_text("changed-server-version")
        changed.append(await identity())
        source = tmp_path / "server.py"
        source.write_text(source.read_text() + "\n# source revision\n")
        changed.append(await identity())
        assert len(set(changed)) == len(changed)
    finally:
        await harness.close()
    for directory in (tmp_path, alternate):
        assert sorted((directory / "starts").read_text().splitlines()) == sorted(
            (directory / "stops").read_text().splitlines()
        )


@pytest.mark.requires_posix
@pytest.mark.asyncio
async def test_installed_package_plugin_relative_imports_and_private_module_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "reload_package"
    package.mkdir()
    (package / "__init__.py").write_text(
        "from .helper import value\ndef setup(harness):\n    harness.agent.register_tool(value)\n"
    )
    (package / "helper.py").write_text("def value() -> str:\n    return 'package helper'\n")
    monkeypatch.syspath_prepend(str(tmp_path))

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        yield TextDoneEvent(text="done")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    harness.config.plugins = ("reload_package:setup",)
    try:
        await harness.initialize()
        definition = harness.agent.tool_registry.get("value")
        assert definition is not None and definition.func is not None and definition.func() == "package helper"
        module_name = definition.func.__module__
        assert module_name in sys.modules
        await collect(harness)
        assert module_name not in sys.modules
    finally:
        modules = tuple(harness.resources.current.modules)
        await harness.close()
    assert not any(name == root or name.startswith(root + ".") for name in sys.modules for root in modules)


@pytest.mark.requires_posix
@pytest.mark.asyncio
async def test_instruction_refresh_invalidates_unseen_edit_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "AGENTS.md").write_text("First instructions")
    target = tmp_path / "target.txt"
    target.write_text("before")

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if len(provider.requests) == 1:
            yield ToolCallEvent(id="read", name="read_file", arguments={"path": "target.txt"})
        elif len(provider.requests) == 2:
            (tmp_path / "AGENTS.md").write_text("Changed instructions requiring review")
            yield ToolCallEvent(
                id="edit", name="edit", arguments={"path": "target.txt", "old": "before", "new": "after"}
            )
        else:
            assert "Changed instructions requiring review" in str(messages[0].content)
            yield TextDoneEvent(text="Need to reread before editing")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)

    async def approve(request: ApprovalRequest) -> bool:
        raise AssertionError("An obsolete read must fail before requesting approval")

    harness.approval_handler = approve
    try:
        events = await collect(harness)
        edited = next(event for event in events if isinstance(event, ToolResultEvent) and event.name == "edit")
        assert edited.error and target.read_text() == "before"
    finally:
        await harness.close()


@pytest.mark.requires_posix
@pytest.mark.asyncio
async def test_close_cancels_initial_reload_and_forbids_new_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nagents.mcp.client import MCPClient

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        yield TextDoneEvent(text="unused")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    configured = server(tmp_path)
    harness.config.mcp_servers = (configured,)
    connected = asyncio.Event()
    clients: list[MCPClient] = []
    original = MCPClient.connect

    async def hold(self: MCPClient) -> None:
        await original(self)
        clients.append(self)
        connected.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(MCPClient, "connect", hold)
    initializing = asyncio.create_task(harness.initialize())
    try:
        await asyncio.wait_for(connected.wait(), 10)
        await asyncio.wait_for(harness.close(), 10)
        with pytest.raises(asyncio.CancelledError):
            await initializing
        assert all(client._process is None for client in clients)
        with pytest.raises(RuntimeError, match="closed"):
            await harness.resources.reload()
    finally:
        initializing.cancel()
        await harness.close()
    assert sorted((tmp_path / "starts").read_text().splitlines()) == sorted(
        (tmp_path / "stops").read_text().splitlines()
    )


@pytest.mark.requires_posix
@pytest.mark.asyncio
async def test_installed_submodule_refreshes_same_size_relative_helper_without_evicting_host_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib
    import os

    package = tmp_path / "local_generation_package"
    package.mkdir()
    (package / "__init__.py").write_text("PACKAGE = 'fixture'\n")
    (package / "plugin.py").write_text(
        "from .helper import value\ndef setup(harness):\n    harness.agent.register_tool(value)\n"
    )
    helper = package / "helper.py"
    helper.write_text("def value() -> str:\n    return 'one'\n")
    previous_stat = helper.stat()
    monkeypatch.syspath_prepend(str(tmp_path))
    host_helper = importlib.import_module("local_generation_package.helper")

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if len(provider.requests) == 1:
            helper.write_text("def value() -> str:\n    return 'two'\n")
            os.utime(helper, ns=(previous_stat.st_atime_ns, previous_stat.st_mtime_ns))
            yield ToolCallEvent(id="old", name="value", arguments={})
        elif len(provider.requests) == 2:
            assert messages[-1].content == "one"
            yield ToolCallEvent(id="new", name="value", arguments={})
        else:
            assert messages[-1].content == "two"
            yield TextDoneEvent(text="done")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    harness.config.plugins = ("local_generation_package.plugin:setup",)
    identities: list[str] = []

    async def approve(request: ApprovalRequest) -> bool:
        definition = harness.resources.definition(request.tool)
        assert definition is not None
        identities.append(harness.resources.approval_identity(definition))
        return True

    harness.approval_handler = approve
    try:
        events = await collect(harness)
        assert [event.result for event in events if isinstance(event, ToolResultEvent)] == ["one", "two"]
        assert identities[0] != identities[1]
        assert sys.modules["local_generation_package.helper"] is host_helper
        assert host_helper.value() == "one"
    finally:
        await harness.close()
        sys.modules.pop("local_generation_package.helper", None)
        sys.modules.pop("local_generation_package", None)


@pytest.mark.requires_posix
@pytest.mark.asyncio
@pytest.mark.parametrize("host_value", [7, 17])
async def test_removed_plugin_override_restores_current_host_limit_and_generated_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_value: int
) -> None:
    extension = tmp_path / "settings.py"
    extension.write_text(
        "def setup(harness):\n    harness.agent.max_tool_rounds = 7\n    harness.agent.system_prompt = 'Plugin prompt'\n"
    )
    (tmp_path / "AGENTS.md").write_text("Old project instructions")

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        yield TextDoneEvent(text="unused")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    harness.config.plugins = (str(extension) + ":setup",)
    try:
        await harness.initialize()
        assert harness.agent.max_tool_rounds == 7 and harness.agent.system_prompt == "Plugin prompt"
        harness.config.max_tool_rounds = host_value
        harness.agent.max_tool_rounds = host_value
        (tmp_path / "AGENTS.md").write_text("New project instructions")
        harness.config.plugins = ()
        await harness.resources.reload()
        assert harness.agent.max_tool_rounds == host_value
        assert "New project instructions" in str(harness.agent.system_prompt)
        assert "Old project instructions" not in str(harness.agent.system_prompt)
        assert harness.agent.system_prompt == harness._generated_system_prompt
    finally:
        await harness.close()


@pytest.mark.requires_posix
@pytest.mark.asyncio
async def test_tool_side_source_and_mcp_configuration_edits_reach_immediately_next_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extension = tmp_path / "editable.py"
    extension.write_text(
        "def old_tool() -> str:\n    return 'one'\ndef setup(harness):\n    harness.agent.register_tool(old_tool)\n"
    )
    configuration = tmp_path / "trusted.json"
    configuration.write_text(json.dumps({"plugins": [str(extension) + ":setup"]}))
    configured = server(tmp_path)

    async def update_configuration() -> str:
        """Actually perform edits from a model-selected tool invocation."""
        extension.write_text(
            "def new_tool() -> str:\n    return 'two'\ndef setup(harness):\n    harness.agent.register_tool(new_tool)\n"
        )
        configuration.write_text(
            json.dumps(
                {
                    "plugins": [str(extension) + ":setup"],
                    "mcp_servers": {
                        "fixture": {"command": configured.command, "args": configured.args, "cwd": configured.cwd}
                    },
                }
            )
        )
        return "updated"

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if len(provider.requests) == 1:
            assert "old_tool" in provider.schemas[-1] and "new_tool" not in provider.schemas[-1]
            yield ToolCallEvent(id="update", name="update_configuration", arguments={})
        elif len(provider.requests) == 2:
            assert "old_tool" not in provider.schemas[-1] and "new_tool" in provider.schemas[-1]
            assert "mcp__fixture__echo" in provider.schemas[-1]
            yield ToolCallEvent(id="new", name="new_tool", arguments={})
            yield ToolCallEvent(id="mcp", name="mcp__fixture__echo", arguments={})
        else:
            yield TextDoneEvent(text="done")

    harness, _ = setup_harness(tmp_path, monkeypatch, script)
    harness.config.resource_paths = (configuration,)
    harness.agent.register_tool(update_configuration)

    async def approve(request: ApprovalRequest) -> bool:
        return True

    harness.approval_handler = approve
    try:
        events = await collect(harness)
        assert [event.result for event in events if isinstance(event, ToolResultEvent)] == ["updated", "two", "one:{}"]
    finally:
        await harness.close()
