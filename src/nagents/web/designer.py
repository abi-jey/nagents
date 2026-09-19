"""Designer routes, sharing the web host's single execution slot."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import aclosing
from contextlib import suppress
from dataclasses import asdict
from dataclasses import replace
from typing import TYPE_CHECKING

import yaml
from fastapi import FastAPI
from fastapi import HTTPException
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import ValidationError

from nagents.cli import _event_record
from nagents.designer.runtime import DesignedHarness
from nagents.designer.runtime import resolve_secret
from nagents.designer.schema import BUILTINS
from nagents.designer.schema import STARTER
from nagents.designer.schema import AgentDefinition
from nagents.designer.schema import Design
from nagents.designer.schema import Instructions
from nagents.designer.schema import Invocation
from nagents.designer.schema import Position
from nagents.designer.schema import parse
from nagents.designer.schema import serialize
from nagents.designer.store import DesignStore
from nagents.designer.store import Recorder
from nagents.designer.store import TraceStore
from nagents.events import ErrorEvent
from nagents.harness.subagents import _await_cleanup
from nagents.mcp import MCPManager
from nagents.mcp import MCPServerConfig
from nagents.observation import observer
from nagents.provider.codex import CodexProvider

from .service import Pending
from .service import Run

if TYPE_CHECKING:
    from collections.abc import Callable

    from nagents.harness.types import ApprovalRequest

    from .service import WebState


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source: str = Field(max_length=49152)
    revision: str = ""


class TestRun(Source):
    agent: str = ""
    prompt: str = Field(min_length=1, max_length=16000)
    previous_run: str = ""


class Discovery(Source):
    server: str


class Designer:
    def __init__(self, state: WebState) -> None:
        self.state = state
        self.files = DesignStore(state.harness.workspace)
        self.traces = TraceStore(state.harness.agent.session.db_path.parent / "designer.db")
        self.config = replace(state.harness.config, data_dir=state.harness.config.data_dir / "designer")

    def example(self) -> str:
        design = parse(STARTER)
        design.id = "delegation-demo"
        design.entrypoint = "coordinator"
        config = self.state.harness.config
        provider = design.providers["primary"]
        provider.type = config.provider
        provider.model = self.state.harness.agent.provider.model
        provider.base_url = config.base_url
        provider.api = config.api
        provider.api_version = config.api_version
        if isinstance(self.state.harness.agent.provider, CodexProvider):
            provider.auth = "chatgpt"
            provider.secret = ""
            design.secrets = {}
        else:
            design.secrets["primary_key"].name = config.api_key_env
            selection = self.state.harness.login_store.selection()
            if selection and selection.provider == config.provider and not selection.base_url and not config.base_url:
                design.secrets["primary_key"].source = "saved"
                design.secrets["primary_key"].name = config.provider
        design.agents = {
            "coordinator": AgentDefinition(
                name="Coordinator",
                max_tool_rounds=6,
                instructions=Instructions(
                    text="For an initial user task, delegate it to analyst exactly once, then end your turn briefly. When a BACKGROUND TASK NOTIFICATION arrives, give the final concise answer using its result; do not delegate again."
                ),
                invokes=[
                    Invocation(
                        agent="analyst", description="Check calculations and explain the result in one sentence."
                    )
                ],
            ),
            "analyst": AgentDefinition(
                name="Analyst",
                max_tool_rounds=3,
                instructions=Instructions(
                    text="Solve the delegated arithmetic task carefully. Return the result and a one-sentence verification. Use no tools."
                ),
            ),
        }
        design.layout = {"coordinator": Position(x=90, y=150), "analyst": Position(x=380, y=150)}
        return serialize(design)

    async def run(self, body: TestRun) -> dict[str, str]:
        with self.state.idle():
            session_id = ""
            if body.previous_run:
                previous = await self.traces.read(body.previous_run)
                # Continuations always use the immutable resolved snapshot.
                design = parse(str(previous["design"]))
                name = str(previous["agent"])
                session_id = str(previous["session_id"])
                if await self.state.designed_channels.pinned(session_id):
                    raise HTTPException(
                        409, "Continue channel conversations in their bound web session or channel chat."
                    )
            else:
                design = parse(body.source).resolved(self.config.workspace)
                name = body.agent or design.entrypoint
            if name not in design.agents:
                raise ValueError("Unknown agent")
            recorder = Recorder()
            harness = DesignedHarness(self.config, design, name, recorder)
            if session_id:
                harness.session_id = session_id
            run = Run(harness.session_id)
            await self.traces.start(run.id, run.session_id, harness.agent_id, serialize(harness.design))
            self.state.active = run
            run.task = asyncio.create_task(self.execute(run, harness, recorder, body.prompt), name=f"designer-{run.id}")
            self.state.status()
            return {"run_id": run.id, "session_id": run.session_id}

    async def execute(self, run: Run, harness: DesignedHarness, recorder: Recorder, prompt: str) -> None:
        token = observer.set(recorder)
        finished = asyncio.Event()

        async def approve(request: ApprovalRequest) -> bool:
            pending = Pending(uuid.uuid4().hex, request.id, asyncio.get_running_loop().create_future())
            pending.record = {**asdict(request), "approval_id": pending.id, "run_id": run.id}
            run.pending = pending
            recorder("approval", pending.record)
            try:
                return await asyncio.wait_for(pending.answer, 300)
            except TimeoutError:
                return False
            finally:
                if not pending.answer.done():
                    pending.answer.set_result(False)
                run.pending = None

        async def persist() -> None:
            while not finished.is_set():
                await recorder.flush(self.traces, run.id)
                with suppress(TimeoutError):
                    await asyncio.wait_for(finished.wait(), 0.2)
            await recorder.flush(self.traces, run.id)

        harness.approval_handler = approve
        writer: asyncio.Task[None] | None = None
        try:
            writer = asyncio.create_task(persist())
            recorder("user_message", {"text": prompt, "agent_id": harness.agent_id})
            async with aclosing(harness.run(prompt)) as events:
                async for event in events:
                    if isinstance(event, ErrorEvent) and not event.recoverable:
                        run.outcome = "failed"
                    recorder("execution_event", _event_record(event))
        except asyncio.CancelledError:
            run.outcome = "cancelled"
            raise
        except Exception as error:
            run.outcome = "failed"
            recorder("error", {"message": str(error), "type": type(error).__name__})
        finally:

            async def cleanup() -> None:
                try:
                    await harness.close()
                finally:
                    recorder("run_finished", {"status": run.outcome})
                    finished.set()
                    try:
                        if writer is not None:
                            await writer
                        await self.traces.finish(run.id, run.outcome)
                    finally:
                        self.state.finish(run)

            try:
                await _await_cleanup(asyncio.create_task(cleanup()))
            finally:
                observer.reset(token)


def register(app: FastAPI, get: Callable[[], Designer]) -> None:
    def invalid(error: Exception) -> HTTPException:
        if isinstance(error, FileExistsError):
            return HTTPException(409, str(error))
        if isinstance(error, ValidationError):
            detail = "; ".join(
                f"{'.'.join(map(str, item['loc']))}: {item['msg']}" for item in error.errors(include_input=False)
            )
        elif isinstance(error, yaml.YAMLError):
            detail = "Invalid YAML syntax. Check indentation and field values."
        else:
            detail = str(error)
        return HTTPException(422, detail)

    @app.get("/api/designer")
    async def catalog() -> dict[str, object]:
        designer = get()
        return {
            "designs": designer.files.list(),
            "starter": STARTER,
            "tools": list(BUILTINS),
            "runs": await designer.traces.runs(),
            "demo": designer.config.demo,
            "example": designer.example(),
        }

    @app.get("/api/designer/designs/{name}")
    async def load(name: str) -> dict[str, object]:
        try:
            return get().files.read(name)
        except (ValueError, OSError, yaml.YAMLError) as error:
            raise invalid(error) from None

    @app.post("/api/designer/save")
    async def save(body: Source) -> dict[str, object]:
        try:
            return get().files.save(body.source, body.revision)
        except (ValueError, OSError, yaml.YAMLError) as error:
            raise invalid(error) from None

    @app.get("/api/designer/channels")
    async def channels() -> dict[str, object]:
        host = get().state.channels
        return {
            "connections": [
                {
                    "id": id,
                    "plugin": connection.plugin,
                    "enabled": connection.enabled,
                    "status": host.catalog.status.get(id, ("disabled", ""))[0],
                }
                for id, connection in host.catalog.connections.items()
            ],
            "routes": await get().state.designed_channels.routes(),
        }

    @app.post("/api/designer/channels")
    async def apply_channels(body: Source) -> dict[str, object]:
        designer = get()
        with designer.state.idle():
            try:
                design = parse(body.source)
                saved = designer.files.read(design.id)
                if saved["revision"] != body.revision or saved["source"] != body.source:
                    raise HTTPException(409, "Save this design before applying its channel routes.")
                return {"routes": await designer.state.designed_channels.apply(design)}
            except (ValueError, OSError, yaml.YAMLError) as error:
                raise invalid(error) from None

    @app.post("/api/designer/validate")
    async def validate(body: Source) -> dict[str, object]:
        try:
            design = parse(body.source)
            resolved = design.resolved(get().config.workspace)
            previews: dict[str, object] = {}
            for name in resolved.agents:
                harness = DesignedHarness(get().config, resolved, name)
                previews[name] = {
                    "instructions": harness.agent.system_prompt,
                    "provider": harness.config.provider,
                    "model": harness.agent.provider.model,
                    "tools": [
                        {"name": tool.name, "description": tool.description, "parameters": tool.parameters}
                        for tool in harness.agent.tool_registry.get_all()
                    ],
                }
                await harness.close()
            return {"design": design.document(), "preview": previews}
        except (ValueError, OSError, yaml.YAMLError) as error:
            raise invalid(error) from None

    @app.post("/api/designer/serialize")
    async def encode(body: Design) -> dict[str, str]:
        return {"source": serialize(body)}

    @app.post("/api/designer/discover")
    async def discover(body: Discovery) -> dict[str, object]:
        designer = get()
        with designer.state.idle():
            manager = MCPManager([])
            try:
                if designer.config.demo:
                    raise ValueError("MCP discovery is unavailable in offline demo")
                design = parse(body.source)
                if body.server not in design.mcp_servers:
                    raise ValueError("Unknown MCP server")
                server = design.mcp_servers[body.server]
                recorder = Recorder()
                env = {
                    **server.env,
                    **{key: resolve_secret(design, ref, recorder) for key, ref in server.secrets.items()},
                }
                await manager.add_server(
                    MCPServerConfig(body.server, server.command, server.args, env, str(designer.config.workspace))
                )
                return {
                    "tools": [
                        {"name": tool.name, "description": tool.description, "parameters": tool.parameters}
                        for tool in await manager.get_tool_definitions()
                    ]
                }
            except Exception:
                raise HTTPException(
                    422, "MCP discovery failed. Check server configuration and secret references."
                ) from None
            finally:
                await _await_cleanup(asyncio.create_task(manager.disconnect_all()))

    @app.post("/api/designer/run")
    async def run(body: TestRun) -> dict[str, str]:
        try:
            return await get().run(body)
        except (ValueError, OSError, yaml.YAMLError) as error:
            raise invalid(error) from None

    @app.get("/api/designer/runs/{run_id}/{after}")
    async def trace(run_id: str, after: int) -> dict[str, object]:
        try:
            result = await get().traces.read(run_id, max(0, after))
            active = get().state.active
            result["approval"] = active.pending.record if active and active.id == run_id and active.pending else {}
            result["channel_session"] = await get().state.designed_channels.pinned(str(result["session_id"]))
            return result
        except ValueError as error:
            raise invalid(error) from None

    @app.delete("/api/designer/runs/{run_id}")
    async def remove(run_id: str) -> dict[str, str]:
        active = get().state.active
        if active and active.id == run_id:
            raise HTTPException(409, "Finish or cancel this run first")
        await get().traces.delete(run_id)
        return {"status": "deleted"}
