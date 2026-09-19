"""Offline definition, runtime, transport capture and web designer regressions."""

from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import patch

import httpx
import pytest
from aiohttp import web

from nagents.designer.runtime import DesignedHarness
from nagents.designer.schema import STARTER
from nagents.designer.schema import AgentDefinition
from nagents.designer.schema import Instructions
from nagents.designer.schema import Invocation
from nagents.designer.schema import MCPDefinition
from nagents.designer.schema import MCPSelection
from nagents.designer.schema import ProviderDefinition
from nagents.designer.schema import ToolSelection
from nagents.designer.schema import parse
from nagents.designer.schema import serialize
from nagents.designer.store import DesignStore
from nagents.designer.store import Recorder
from nagents.designer.store import TraceStore
from nagents.events import DoneEvent
from nagents.events import Event
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.harness.config import HarnessConfig
from nagents.observation import observer
from nagents.provider import Provider
from nagents.provider import ProviderType
from nagents.types import GenerationConfig
from nagents.types import Message
from nagents.types import ToolDefinition
from nagents.web.app import create_app
from nagents.web.designer import Designer

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.harness.types import ApprovalRequest


@pytest.mark.parametrize("suffix", ["\nid: repeated\n", "\nunknown: true\n", "\nlayout: &a {x: *a}\n"])
def test_definition_rejects_ambiguous_yaml(suffix: str) -> None:
    with pytest.raises(ValueError):
        parse(STARTER + suffix)


def test_designer_chatgpt_auth_is_endpoint_bound_and_demo_stays_offline(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="default OpenAI"):
        ProviderDefinition(type="openai", model="model", auth="chatgpt", base_url="https://example.com")

    async def drive() -> None:
        design = parse(STARTER)
        design.providers["primary"] = ProviderDefinition(type="openai", model="gpt-5.6-terra", auth="chatgpt")
        design.secrets = {}
        design = parse(serialize(design))
        harness = DesignedHarness(HarnessConfig(tmp_path, data_dir=tmp_path / "data", demo=True), design)
        try:
            with (
                patch.object(
                    harness.openai_auth, "credentials", side_effect=AssertionError("Demo must not resolve credentials")
                ),
                patch.object(
                    harness.tools,
                    "instructions",
                    side_effect=AssertionError("Designed agents must not read implicit workspace instructions"),
                ),
            ):
                events = [event async for event in harness.run("Hello")]
            assert isinstance(events[-1], DoneEvent)
        finally:
            await harness.close()

    asyncio.run(drive())


def test_designer_example_uses_current_provider_configuration(tmp_path: Path) -> None:
    config = HarnessConfig(
        tmp_path,
        provider="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_key_env="OPENROUTER_API_KEY",
        api="chat_completions",
        api_version="2025-01-01",
    )
    state = SimpleNamespace(
        harness=SimpleNamespace(
            config=config,
            agent=SimpleNamespace(provider=SimpleNamespace(model="deepseek/deepseek-chat")),
            login_store=SimpleNamespace(selection=lambda: None),
        )
    )
    designer = Designer.__new__(Designer)
    designer.state = state
    designer.config = HarnessConfig(tmp_path, provider="openai", model="gpt-4.1")

    design = parse(designer.example())
    provider = design.providers["primary"]
    assert provider.type == "openrouter"
    assert provider.model == "deepseek/deepseek-chat"
    assert provider.base_url == "https://openrouter.ai/api/v1"
    assert provider.api == "chat_completions"
    assert provider.api_version == "2025-01-01"
    assert design.secrets["primary_key"].name == "OPENROUTER_API_KEY"


def test_definition_roundtrip_and_atomic_conflict(tmp_path: Path) -> None:
    design = parse(STARTER)
    assert parse(serialize(design)) == design
    store = DesignStore(tmp_path)
    saved = store.save(STARTER, "")
    with pytest.raises(FileExistsError):
        store.save(STARTER.replace("helpful", "thoughtful"), "")
    assert store.read(design.id)["source"] == STARTER
    store.save(STARTER.replace("helpful", "thoughtful"), str(saved["revision"]))
    assert store.list() == [design.id]
    # Materializing form defaults must not force sampling settings onto providers.
    assert (
        parse(serialize(type(design).model_validate(design.document()))).agents["assistant"].generation.model_fields_set
        == set()
    )


def test_connection_anchors_roundtrip_and_reject_invalid_offsets() -> None:
    design = parse(STARTER)
    design.agents["assistant"].invokes = [
        Invocation(agent="assistant", source_port="bottom", source_offset=0.25, target_port="top", target_offset=0.8)
    ]
    loaded = parse(serialize(design))
    assert loaded.agents["assistant"].invokes == design.agents["assistant"].invokes
    with pytest.raises(ValueError):
        Invocation(agent="assistant", source_offset=1.1)


def test_instruction_snapshot_is_independent_of_later_edits(tmp_path: Path) -> None:
    path = tmp_path / "instructions.md"
    path.write_text("Original", encoding="utf-8")
    design = parse(STARTER)
    design.agents["assistant"].instructions.file = "instructions.md"
    resolved = design.resolved(tmp_path)
    path.write_text("Changed", encoding="utf-8")
    assert "Original" in resolved.agents["assistant"].instructions.text
    assert resolved.agents["assistant"].instructions.file == ""
    assert "Changed" not in serialize(resolved)


def test_general_agent_tools_and_delegation_are_explicit(tmp_path: Path) -> None:
    async def drive() -> None:
        design = parse(STARTER)
        design.agents["researcher"] = AgentDefinition(instructions=Instructions(text="Only research."))
        design.agents["assistant"].invokes = [Invocation(agent="researcher", description="Research documents")]
        design.agents["researcher"].tools = [ToolSelection(ref="builtin.read_file", description="Read research notes")]
        # The standalone/TUI runtime never opens serve-only channel bindings.
        design.channels = {"telegram": "assistant"}
        root = DesignedHarness(HarnessConfig(tmp_path, data_dir=tmp_path / "data", demo=True), design)
        try:
            assert root.agent.tool_registry.names() == ["delegate"]
            assert "coding assistant" not in str(root.agent.system_prompt)
            child = root.create_defined_child("researcher")
            try:
                assert child.agent.system_prompt == "Only research."
                assert child.agent.tool_registry.names() == ["read_file"]
                assert child.agent.tool_registry.get_all()[0].description == "Read research notes"
            finally:
                await child.close()
            with pytest.raises(ValueError, match="not connected"):
                await root.delegate("do something", "assistant")
        finally:
            await root.close()

    asyncio.run(drive())


def test_delegated_agents_use_own_context_and_emit_traces(tmp_path: Path) -> None:
    async def drive() -> None:
        design = parse(STARTER)
        design.agents["researcher"] = AgentDefinition(instructions=Instructions(text="Research instructions"))
        design.providers["research"] = design.providers["primary"].model_copy(update={"model": "research-model"})
        design.agents["researcher"].provider = "research"
        design.agents["assistant"].invokes = [Invocation(agent="researcher")]
        root = DesignedHarness(HarnessConfig(tmp_path, data_dir=tmp_path / "data", demo=True), design)
        requests: list[list[Message]] = []
        models: list[str] = []
        recorder = Recorder()

        async def generate(
            self: Provider,
            messages: list[Message],
            tools: list[ToolDefinition] | None = None,
            config: GenerationConfig | None = None,
            stream: bool = True,
            verify_model: bool = False,
        ) -> AsyncIterator[Event]:
            requests.append(messages)
            models.append(self.model)
            if len(requests) == 1:
                yield ToolCallEvent(
                    id="delegate-1", name="delegate", arguments={"agent": "researcher", "prompt": "Research"}
                )
            else:
                yield TextDoneEvent(text="Complete")

        token = observer.set(recorder)
        try:
            with patch("nagents.designer.runtime.DesignProvider.generate", generate):
                events = [event async for event in root.run("Help")]
            assert any(isinstance(event, DoneEvent) for event in events)
            assert any(messages[0].content == "Research instructions" for messages in requests)
            assert "research-model" in models
            contexts = [record for record in recorder.pending if record["kind"] == "model_context"]
            assert len(contexts) >= 3
            assert "researcher" in json.dumps(contexts)
            assert all(info.status == "completed" for info in root.tasks.list())
        finally:
            observer.reset(token)
            await root.close()

    asyncio.run(drive())


def test_gateway_records_actual_body_and_masks_credentials(tmp_path: Path) -> None:
    async def drive() -> None:
        received: list[str] = []

        async def endpoint(request: web.Request) -> web.Response:
            received.append(await request.text())
            return web.json_response(
                {"choices": [{"message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}]}
            )

        app = web.Application()
        app.router.add_post("/chat/completions", endpoint)
        runner = web.AppRunner(app)
        await runner.setup()
        server = web.TCPSite(runner, "127.0.0.1", 0)
        await server.start()
        port = runner.addresses[0][1]
        recorder = Recorder()
        recorder.secrets.add("private-token")
        provider = Provider(ProviderType.LITELLM, "private-token", "model", base_url=f"http://127.0.0.1:{port}")
        token = observer.set(recorder)
        try:
            _ = [event async for event in provider.generate([Message(role="user", content="Hello")], stream=False)]
            bodies = [record["data"] for record in recorder.pending if record["kind"] == "http_request_body"]
            assert len(bodies) == 1
            assert isinstance(bodies[0], dict)
            assert bodies[0]["body"] == received[0]
            assert "private-token" not in json.dumps(recorder.pending)
            assert any(record["kind"] == "http_stream" for record in recorder.pending)
        finally:
            observer.reset(token)
            await provider.close()
            await runner.cleanup()

    asyncio.run(drive())


def test_trace_persistence_and_interrupted_recovery(tmp_path: Path) -> None:
    async def drive() -> None:
        store = TraceStore(tmp_path / "trace.db")
        await store.initialize()
        await store.start("run", "session", "assistant", STARTER)
        recorder = Recorder()
        recorder.secrets.add('secret"value')
        recorder("request", {"body": 'secret"value'})
        recorder("request", {"body": json.dumps({"nested": 'secret"value'})})
        await recorder.flush(store, "run")
        await store.initialize()
        result = await store.read("run")
        assert result["status"] == "interrupted"
        assert "redacted" in json.dumps(result["events"])
        assert (await store.read("run", 2))["events"] == []
        await store.delete("run")
        assert await store.runs() == []

    asyncio.run(drive())


def test_designed_mcp_uses_selected_schema_credentials_and_owned_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "mcp-test-secret")
    server = tmp_path / "server.py"
    server.write_text(
        """import json, os, sys
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    method = message["method"]
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "test", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "search_docs", "description": "Search docs", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": message["params"]["arguments"]["query"] + (":authenticated" if os.environ.get("DOCS_TOKEN") == "mcp-test-secret" else ":missing")}]}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
""",
        encoding="utf-8",
    )

    async def drive() -> None:
        design = parse(STARTER)
        design.mcp_servers["docs"] = MCPDefinition(
            command=sys.executable, args=["-u", str(server)], secrets={"DOCS_TOKEN": "primary_key"}
        )
        design.agents["assistant"].mcp = [
            MCPSelection(server="docs", tools=[ToolSelection(ref="search_docs", description="Search selected docs")])
        ]
        harness = DesignedHarness(HarnessConfig(tmp_path, data_dir=tmp_path / "data"), design)
        approvals: list[str] = []

        async def approve(request: ApprovalRequest) -> bool:
            approvals.append(request.tool)
            return True

        async def generate(
            self: Provider,
            messages: list[Message],
            tools: list[ToolDefinition] | None = None,
            config: GenerationConfig | None = None,
            stream: bool = True,
            verify_model: bool = False,
        ) -> AsyncIterator[Event]:
            assert tools is not None and len(tools) == 1
            assert tools[0].description == "Search selected docs"
            assert tools[0].parameters["required"] == ["query"]
            if messages[-1].role == "tool":
                assert messages[-1].content == "question:authenticated"
                yield TextDoneEvent(text="Done")
            else:
                yield ToolCallEvent(id="mcp-call", name="mcp__docs__search_docs", arguments={"query": "question"})

        harness.approval_handler = approve
        try:
            with patch("nagents.designer.runtime.DesignProvider.generate", generate):
                events = [event async for event in harness.run("Research")]
            assert isinstance(events[-1], DoneEvent)
            assert approvals == ["mcp_connect", "mcp__docs__search_docs"]
            assert harness.mcp.is_connected
        finally:
            await harness.close()
        assert not harness.mcp.is_connected

    asyncio.run(drive())


@pytest.mark.requires_posix
def test_designer_approvals_busy_slot_and_cancellation(tmp_path: Path) -> None:
    async def drive() -> None:
        async def generate(
            self: Provider,
            messages: list[Message],
            tools: list[ToolDefinition] | None = None,
            config: GenerationConfig | None = None,
            stream: bool = True,
            verify_model: bool = False,
        ) -> AsyncIterator[Event]:
            if messages[-1].role == "tool":
                yield TextDoneEvent(text="Write was denied")
            else:
                yield ToolCallEvent(id="write-one", name="write", arguments={"path": "output.txt", "content": "test"})

        (tmp_path / "assets").mkdir()
        app = create_app(HarnessConfig(tmp_path, data_dir=tmp_path / "data"), assets=tmp_path)
        source = STARTER.replace("tools: []", "tools:\n      - ref: builtin.write")
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8765") as client,
        ):
            bootstrap = (await client.get("/api/bootstrap")).json()
            client.headers.update({"X-Ngn-Token": bootstrap["token"], "Origin": "http://127.0.0.1:8765"})
            with patch("nagents.designer.runtime.DesignProvider.generate", generate):
                for cancel in (False, True):
                    response = await client.post("/api/designer/run", json={"source": source, "prompt": "Write"})
                    assert response.status_code == 200, response.text
                    run_id = response.json()["run_id"]
                    active = app.state.web.active
                    assert active is not None
                    async with asyncio.timeout(5):
                        while active.pending is None:
                            await asyncio.sleep(0.01)
                    competing = await client.post("/api/designer/run", json={"source": source, "prompt": "Other"})
                    assert competing.status_code == 409
                    if cancel:
                        response = await client.post("/api/cancel", json={"run_id": run_id})
                    else:
                        response = await client.post(
                            "/api/approval",
                            json={
                                "run_id": run_id,
                                "approval_id": active.pending.id,
                                "call_id": active.pending.call_id,
                                "decision": "deny",
                            },
                        )
                    assert response.status_code == 200
                    await asyncio.gather(active.task, return_exceptions=True)
                    trace = (await client.get(f"/api/designer/runs/{run_id}/0")).json()
                    assert trace["status"] == ("cancelled" if cancel else "completed")
                    assert app.state.web.active is None
                    assert not (tmp_path / "output.txt").exists()

    asyncio.run(drive())


def test_web_designer_chat_and_pinned_continuation(tmp_path: Path) -> None:
    async def drive() -> None:
        (tmp_path / "assets").mkdir()
        app = create_app(
            HarnessConfig(tmp_path, data_dir=tmp_path / "data", demo=True),
            assets=tmp_path,
            harness_factory=lambda config: DesignedHarness(config, parse(STARTER)),
        )
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8765") as client,
        ):
            bootstrap = (await client.get("/api/bootstrap")).json()
            client.headers.update({"X-Ngn-Token": bootstrap["token"], "Origin": "http://127.0.0.1:8765"})
            response = await client.post("/api/designer/validate", json={"source": STARTER})
            assert response.status_code == 200, response.text
            assert response.json()["preview"]["assistant"]["tools"] == []
            response = await client.post("/api/designer/run", json={"source": STARTER, "prompt": "Hello"})
            assert response.status_code == 200, response.text
            run = response.json()
            active = app.state.web.active
            assert active is not None
            await active.task
            trace = (await client.get(f"/api/designer/runs/{run['run_id']}/0")).json()
            assert trace["status"] == "completed"
            assert any(record["kind"] == "model_context" for record in trace["events"])
            response = await client.post(
                "/api/designer/run",
                json={
                    "source": STARTER.replace("helpful", "changed"),
                    "prompt": "Again",
                    "previous_run": run["run_id"],
                },
            )
            assert response.status_code == 200
            assert response.json()["session_id"] == run["session_id"]
            active = app.state.web.active
            assert active is not None
            await active.task
            trace2 = (await client.get(f"/api/designer/runs/{response.json()['run_id']}/0")).json()
            assert trace2["revision"] == trace["revision"]

    asyncio.run(drive())
