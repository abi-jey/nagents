"""Bind Agent.run and its public ToolExecutor boundary to a Rust host."""

from __future__ import annotations

import asyncio
import os
import re
import stat
from contextlib import ExitStack
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

from .auth import Auth
from .auth import auth_factory
from .auth import identity_guard
from .auth import joined_cleanup
from .auth import store_path
from .protocol import Emitter
from .protocol import Json
from .protocol import ProtocolError
from .protocol import fields
from .protocol import mapping
from .protocol import text
from .steering import _Steering
from .steering import _SteeringPlugin


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
    auth_store: Path | None = None
    steering: bool = False

    @classmethod
    def parse(cls, message: dict[str, object]) -> Start:
        fields(message, {"type", "run"})
        if message["type"] != "start":
            raise ProtocolError("Expected a start message")
        run = mapping(message["run"])
        fields(
            run,
            {"session_id", "prompt", "system_prompt", "database", "provider", "tools", "max_tool_rounds"},
            frozenset({"auth_store", "steering"}),
        )
        steering = run.get("steering", False)
        if type(steering) is not bool:
            raise ProtocolError("Invalid host steering acknowledgment")
        provider = mapping(run["provider"])
        mode = validate_provider(provider)
        auth_store = None
        if mode == "chatgpt":
            auth_store = store_path(text(run, "auth_store", 4096))
        elif "auth_store" in run:
            raise ProtocolError("auth_store is only allowed for ChatGPT authentication")
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
        if auth_store is not None and database.parent == auth_store.parent:
            raise ProtocolError("Runtime conversation storage must be separate from authentication storage")
        return cls(
            text(run, "session_id", 80),
            text(run, "prompt"),
            text(run, "system_prompt", 96_000),
            database,
            provider,
            definitions,
            rounds,
            auth_store,
            steering,
        )


class HostExecutor(ToolExecutor):
    """Never invoke a Python callable. All tools are executed by the Rust host."""

    def __init__(self, registry: ToolRegistry, run_id: str, emit: Emitter) -> None:
        super().__init__(registry)
        self.run_id = run_id
        self.emit = emit
        self.pending: dict[str, tuple[str, asyncio.Future[ToolResultEvent]]] = {}
        self.seen: set[str] = set()
        self.steering = _Steering(run_id, emit)

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
                # The wire contract uses an empty error string for success;
                # nagents persists success only when ToolResultEvent.error is None.
                error=text(result, "error", 32_000, empty=True) or None,
            )
        )

    def bind_registry(self, registry: ToolRegistry) -> None:
        super().__init__(registry)


def validate_provider(settings: dict[str, object]) -> str:
    fields(settings, {"kind", "model", "api", "base_url", "api_key_env"}, frozenset({"auth"}))
    for key in settings:
        text(settings, key, 4096, empty=key in {"api", "base_url", "api_key_env"})
    mode = text(settings, "auth", 16) if "auth" in settings else "api_key"
    if mode not in {"api_key", "chatgpt"}:
        raise ProtocolError("Unsupported provider authentication mode")
    if mode == "chatgpt":
        if (
            settings["kind"] != "openai"
            or settings["api_key_env"] != ""
            or settings["api"] not in {"", "auto", "responses"}
            or settings["base_url"] not in {"", "https://api.openai.com/v1", "https://api.openai.com/v1/"}
            or re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", text(settings, "model", 128)) is None
        ):
            raise ProtocolError(
                "ChatGPT auth requires kind openai, a bounded model ID, auto/responses, no key environment setting and no custom endpoint"
            )
    elif re.fullmatch(r"[A-Za-z0-9_]{1,128}", text(settings, "api_key_env", 128)) is None:
        raise ProtocolError("api_key_env must name an environment variable")
    return mode


def create_provider(settings: dict[str, object]) -> Provider:
    if validate_provider(settings) != "api_key":
        raise ProtocolError("ChatGPT requires an explicit host-owned auth store and credentials callback")
    kind = ProviderType(text(settings, "kind", 128))
    variable = text(settings, "api_key_env", 128)
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


def create_agent(
    start: Start,
    executor: HostExecutor,
    *,
    provider: Provider | None = None,
    plugin: _SteeringPlugin | None = None,
) -> Agent:
    start.database.parent.mkdir(parents=True, exist_ok=True)
    if start.database.exists() or start.database.is_symlink():
        metadata = start.database.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ProtocolError("Runtime storage must be an unlinked regular file")
    descriptor = os.open(start.database, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.close(descriptor)
    start.database.chmod(0o600)
    agent = Agent(
        provider=provider if provider is not None else create_provider(start.provider),
        session_manager=SessionManager(start.database),
        system_prompt=start.system_prompt,
        max_tool_rounds=start.max_tool_rounds,
        streaming=True,
        tool_executor=executor,
        save_tool_outputs=False,
        compaction_strategy=None,
        plugins=(plugin,) if plugin is not None else (),
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
    auth: Auth | None = None
    provider: Provider | None = None
    ownership = ExitStack()
    status = "failed"
    error = "Agent ended without a successful terminal event"
    try:
        if validate_provider(start.provider) == "chatgpt":
            if start.auth_store is None:
                raise ProtocolError("ChatGPT requires an explicit host-owned auth store")
            # Keep a shared guard in the actual runtime too. An orphaned runner
            # must finish cancellation/cleanup before CLI identity changes win.
            ownership.enter_context(identity_guard(store_path(str(start.auth_store)), exclusive=False))
            auth = auth_factory()(store_path(str(start.auth_store)))
            from nagents.provider.openai import OpenAIProvider

            # Explicit callback bypasses CODEX_HOME/discovery and all API-key fallbacks.
            # A default public base_url is validated above, never forwarded to OAuth.
            provider = OpenAIProvider(credentials=auth.credentials, model=text(start.provider, "model", 128))
        else:
            if start.auth_store is not None:
                raise ProtocolError("auth_store is only allowed for ChatGPT authentication")
            provider = create_provider(start.provider)
        plugin = _SteeringPlugin(executor.steering, start.session_id) if start.steering else None
        agent = create_agent(start, executor, provider=provider, plugin=plugin)
        failed = False
        prompt = start.prompt
        while True:
            completed = False
            async with aclosing(agent.run(prompt, session_id=start.session_id, user_id="ngn-rust")) as events:
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
            if not completed or failed:
                break
            if plugin is None:
                # An old host may ignore our Ready capability. Without its
                # explicit Start acknowledgment, remain strictly baseline v1.
                status, error = "completed", ""
                break
            # No-tools final replies have no after_tools hook. Ask explicitly
            # after Agent.run is closed and its assistant response is persisted.
            offer = await executor.steering.boundary("final")
            if offer is None:
                status, error = "completed", ""
                break
            plugin.continuation = offer
            prompt = offer.prompt
            # Explicitly continue the SAME persisted session and host activation.
    except asyncio.CancelledError:
        status, error = "cancelled", ""
    except ProtocolError as exception:
        error = str(exception)
    except Exception:
        error = "Agent/provider failed. Verify the runtime installation, provider configuration and credentials. No automatic tool retry was performed."
    finally:

        async def cleanup() -> None:
            try:
                if agent is not None:
                    await agent.close()
                elif provider is not None:
                    await provider.close()
            finally:
                if auth is not None:
                    await auth.close()

        try:
            await joined_cleanup(cleanup())
        except asyncio.CancelledError:
            status, error = "cancelled", ""
        except Exception:
            status, error = "failed", "Agent resource cleanup failed"
        finally:
            ownership.close()
        emit(executor.run_id, {"type": "finished", "status": status, "error": error})
