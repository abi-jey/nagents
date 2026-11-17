"""Bind Agent.run and its public ToolExecutor boundary to a Rust host."""

from __future__ import annotations

import asyncio
import os
import stat
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from nagents import Agent
from nagents import Provider
from nagents import ProviderType
from nagents import SessionManager
from nagents.events import DoneEvent
from nagents.events import ErrorEvent
from nagents.events import FinishReason
from nagents.events import TextChunkEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolResultEvent
from nagents.tools import ToolExecutor
from nagents.tools import ToolRegistry
from nagents.types import JsonSchema
from nagents.types import ToolCall

from .protocol import Emitter
from .protocol import Json
from .protocol import ProtocolError
from .protocol import fields
from .protocol import mapping
from .protocol import text


@dataclass(frozen=True)
class Definition:
    name: str
    description: str
    parameters: JsonSchema


@dataclass(frozen=True)
class Start:
    session_id: str
    prompt: str
    system_prompt: str
    database: Path
    provider: dict[str, object]
    tools: list[Definition]
    max_tool_rounds: int

    @classmethod
    def parse(cls, message: dict[str, object]) -> Start:
        fields(message, {"type", "run"})
        if message["type"] != "start":
            raise ProtocolError("Expected a start message")
        run = mapping(message["run"])
        fields(run, {"session_id", "prompt", "system_prompt", "database", "provider", "tools", "max_tool_rounds"})
        provider = mapping(run["provider"])
        fields(provider, {"kind", "model", "api", "base_url", "api_key_env"})
        for key in provider:
            text(provider, key, 4096, empty=key in {"api", "base_url"})
        tools = run["tools"]
        if not isinstance(tools, list) or len(tools) > 128:
            raise ProtocolError("Invalid tool definitions")
        definitions: list[Definition] = []
        names: set[str] = set()
        for entry in tools:
            definition = mapping(entry)
            fields(definition, {"name", "description", "parameters"})
            name = text(definition, "name", 128)
            if name in names:
                raise ProtocolError("Duplicate tool name")
            names.add(name)
            parameters = mapping(definition["parameters"])
            if parameters.get("type") != "object":
                raise ProtocolError("Tool parameters must be an object schema")
            definitions.append(Definition(name, text(definition, "description", 8192), cast("JsonSchema", parameters)))
        rounds = run["max_tool_rounds"]
        if type(rounds) is not int or not 1 <= rounds <= 3000:
            raise ProtocolError("Invalid tool round limit")
        database = Path(text(run, "database", 4096))
        if not database.is_absolute():
            raise ProtocolError("Runtime storage path must be absolute")
        return cls(
            text(run, "session_id", 80),
            text(run, "prompt"),
            text(run, "system_prompt", 96_000),
            database,
            provider,
            definitions,
            rounds,
        )


class HostExecutor(ToolExecutor):
    """Never invoke a Python callable. All tools are executed by the Rust host."""

    def __init__(self, registry: ToolRegistry, run_id: str, emit: Emitter) -> None:
        super().__init__(registry)
        self.run_id = run_id
        self.emit = emit
        self.pending: dict[str, tuple[str, asyncio.Future[ToolResultEvent]]] = {}
        self.seen: set[str] = set()

    async def execute(self, tool_call: ToolCall) -> ToolResultEvent:
        if tool_call.id in self.seen or not tool_call.id or len(tool_call.id) > 200:
            raise ProtocolError("Duplicate or invalid call_id")
        self.seen.add(tool_call.id)
        future: asyncio.Future[ToolResultEvent] = asyncio.get_running_loop().create_future()
        self.pending[tool_call.id] = (tool_call.name, future)
        try:
            self.emit(
                self.run_id,
                {
                    "type": "tool_request",
                    "call": {"call_id": tool_call.id, "name": tool_call.name, "arguments": tool_call.arguments},
                },
            )
            return await future
        finally:
            self.pending.pop(tool_call.id, None)

    def accept(self, message: dict[str, object]) -> None:
        fields(message, {"type", "result"})
        result = mapping(message["result"])
        fields(result, {"call_id", "name", "result", "error"})
        call_id = text(result, "call_id", 200)
        name = text(result, "name", 128)
        pending = self.pending.get(call_id)
        if pending is None or pending[0] != name or pending[1].done():
            raise ProtocolError("Tool result is not bound to a pending call")
        pending[1].set_result(
            ToolResultEvent(
                id=call_id,
                name=name,
                result=cast("Json", result["result"]),
                error=text(result, "error", 32_000, empty=True),
            )
        )

    def bind_registry(self, registry: ToolRegistry) -> None:
        super().__init__(registry)


def create_provider(settings: dict[str, object]) -> Provider:
    kind = ProviderType(text(settings, "kind", 128))
    variable = text(settings, "api_key_env", 128)
    if not variable.replace("_", "A").isascii() or not variable.replace("_", "A").isalnum():
        raise ProtocolError("api_key_env must name an environment variable")
    key = os.environ.get(variable, "")
    if not key.strip():
        raise ProtocolError("Configured provider API-key environment variable is not set")
    return Provider(
        provider_type=kind,
        api_key=key,
        model=text(settings, "model", 4096),
        api=text(settings, "api", 128, empty=True) or "auto",
        base_url=text(settings, "base_url", 4096, empty=True) or None,
    )


def _unbound(**arguments: object) -> None:
    raise RuntimeError("Host tools cannot execute inside Python")


def create_agent(start: Start, executor: HostExecutor) -> Agent:
    start.database.parent.mkdir(parents=True, exist_ok=True)
    if start.database.exists() or start.database.is_symlink():
        metadata = start.database.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ProtocolError("Runtime storage must be an unlinked regular file")
    descriptor = os.open(start.database, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.close(descriptor)
    start.database.chmod(0o600)
    agent = Agent(
        provider=create_provider(start.provider),
        session_manager=SessionManager(start.database),
        system_prompt=start.system_prompt,
        max_tool_rounds=start.max_tool_rounds,
        streaming=True,
        tool_executor=executor,
        save_tool_outputs=False,
        compaction_strategy=None,
    )
    executor.bind_registry(agent.tool_registry)
    # Public registry API; functions are inert because the executor is host-backed.
    for definition in start.tools:
        agent.tool_registry.register(
            _unbound, name=definition.name, description=definition.description, parameters=definition.parameters
        )
    return agent


async def run_agent(start: Start, executor: HostExecutor, emit: Emitter) -> None:
    agent: Agent | None = None
    status = "failed"
    error = "Agent ended without a successful terminal event"
    try:
        agent = create_agent(start, executor)
        completed = False
        failed = False
        async with aclosing(agent.run(start.prompt, session_id=start.session_id, user_id="ngn-rust")) as events:
            async for event in events:
                if isinstance(event, TextChunkEvent):
                    emit(executor.run_id, {"type": "text_delta", "text": event.chunk})
                elif isinstance(event, TextDoneEvent):
                    emit(executor.run_id, {"type": "text_completed", "text": event.text})
                elif isinstance(event, ErrorEvent):
                    failed |= not event.recoverable
                    emit(
                        executor.run_id,
                        {
                            "type": "notice",
                            "text": "Agent/provider reported an error. Check the selected provider, model and credentials; upstream payloads are not exposed.",
                        },
                    )
                elif isinstance(event, DoneEvent):
                    completed = event.finish_reason is FinishReason.STOP
        if completed and not failed:
            status, error = "completed", ""
    except asyncio.CancelledError:
        status, error = "cancelled", ""
    except ProtocolError as exception:
        error = str(exception)
    except Exception:
        error = "Agent/provider failed. Verify the runtime installation, provider configuration and credentials. No automatic tool retry was performed."
    finally:
        if agent is not None:
            try:
                await agent.close()
            except Exception:
                status, error = "failed", "Agent resource cleanup failed"
        emit(executor.run_id, {"type": "finished", "status": status, "error": error})
