"""Replaceable tool/MCP generations without replacing the live harness.

Setup receives staged registration surfaces. Callable closures retain a facade
which forwards to the live harness after setup, so session/approval ownership is
never cloned. A response's calls retain the generation actually advertised.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.util
import inspect
import json
import marshal
import sys
import uuid
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

from nagents._async import finish_on_cancel
from nagents.extensions import AgentPlugin
from nagents.extensions import ModelRequest
from nagents.extensions import RunContext
from nagents.mcp import MCPManager
from nagents.mcp import MCPServerConfig
from nagents.tools.registry import ToolRegistry
from nagents.types import Message
from nagents.types import ToolDefinition

from .commands import CommandRegistry
from .resource_config import resource_settings
from .types import Notice

if TYPE_CHECKING:
    from nagents.events import Event
    from nagents.events import ToolResultEvent
    from nagents.types import ToolCall

    from .config import HarnessConfig
    from .runtime import Harness

# Explicitly replaceable model behavior; host security and resource owners are
# deliberately absent (provider, executor, session, tasks and approval handler).
_AGENT_SETTINGS = frozenset({"max_tool_rounds", "compaction_strategy", "streaming", "system_prompt"})


class ReloadContractError(ValueError):
    """A setup attempted to replace infrastructure outside the reload contract."""


class _AgentScope:
    def __init__(self, owner: Harness, registry: ToolRegistry, hooks: list[AgentPlugin]) -> None:
        object.__setattr__(self, "_owner", owner)
        object.__setattr__(self, "_registry", registry)
        object.__setattr__(self, "_hooks", hooks)
        object.__setattr__(self, "_settings", {})
        object.__setattr__(self, "_ready", False)

    def __getattr__(self, name: str) -> object:
        if self._ready:
            return getattr(self._owner.agent, name)
        if name == "tool_registry":
            return self._registry
        if name == "register_tool":
            return self._registry.register
        if name == "plugins":
            return self._hooks
        if name in _AGENT_SETTINGS:
            return self._settings.get(name, getattr(self._owner.agent, name))
        raise ReloadContractError(
            f"Reloadable plugin setup cannot access agent.{name}; use lifecycle hooks for runtime access"
        )

    def __setattr__(self, name: str, value: object) -> None:
        if self._ready:
            setattr(self._owner.agent, name, value)
        elif name in _AGENT_SETTINGS:
            self._settings[name] = value
        else:
            raise ReloadContractError(f"Reloadable plugin setup cannot replace host-owned agent.{name}")

    _owner: Harness
    _registry: ToolRegistry
    _hooks: list[AgentPlugin]
    _settings: dict[str, object]
    _ready: bool


class _HarnessScope:
    def __init__(self, owner: Harness, agent: _AgentScope, commands: CommandRegistry) -> None:
        object.__setattr__(self, "_owner", owner)
        object.__setattr__(self, "_agent", agent)
        object.__setattr__(self, "_commands", commands)
        object.__setattr__(self, "_config", copy.deepcopy(owner.config))

    def __getattr__(self, name: str) -> object:
        if name == "agent":
            return self._agent
        if self._agent._ready:
            return getattr(self._owner, name)
        if name == "commands":
            return self._commands
        if name == "workspace":
            return self._owner.workspace
        if name == "config":
            return self._config
        raise ReloadContractError(
            f"Reloadable plugin setup cannot access harness.{name}; use lifecycle hooks for runtime access"
        )

    def __setattr__(self, name: str, value: object) -> None:
        if self._agent._ready:
            setattr(self._owner, name, value)
        else:
            raise ReloadContractError(f"Reloadable plugin setup cannot replace host-owned harness.{name}")

    _owner: Harness
    _agent: _AgentScope
    _commands: CommandRegistry
    _config: HarnessConfig


@dataclass(eq=False)
class _Generation:
    registry: ToolRegistry = field(default_factory=ToolRegistry)
    manager: MCPManager = field(default_factory=lambda: MCPManager([]))
    hooks: list[AgentPlugin] = field(default_factory=list)
    modules: list[str] = field(default_factory=list)
    references: tuple[str, ...] = ()
    settings: dict[str, object] = field(default_factory=dict)
    closed: bool = False
    used: bool = False
    mcp_names: set[str] = field(default_factory=set)
    owned_names: set[str] = field(default_factory=set)
    approval_identities: dict[str, str] = field(default_factory=dict)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        errors: list[BaseException] = []
        for hook in self.hooks:
            try:
                await hook.aclose()
            except BaseException as error:
                errors.append(error)
        try:
            await self.manager.disconnect_all()
        except BaseException as error:
            errors.append(error)
        for name in tuple(sys.modules):
            if any(name == root or name.startswith(root + ".") for root in self.modules):
                sys.modules.pop(name, None)
        if errors:
            raise BaseExceptionGroup("Extension generation cleanup failed", errors)


def _fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _callable_origin(tool: ToolDefinition) -> str:
    """Include imported callable code, rather than just the setup entry point."""
    function = tool.func.__func__ if inspect.ismethod(tool.func) else tool.func
    if not inspect.isfunction(function):
        return _fingerprint({"module": getattr(function, "__module__", ""), "type": type(function).__qualname__})
    source = inspect.getsourcefile(function)
    content = ""
    if source and Path(source).is_file():
        with Path(source).open("rb") as stream:
            content = hashlib.file_digest(stream, "sha256").hexdigest()
    return _fingerprint(
        {
            "code": hashlib.sha256(marshal.dumps(function.__code__)).hexdigest(),
            "path": source,
            "source": content,
        }
    )


def _mcp_origin(server: MCPServerConfig) -> str:
    """Capture direct script sources alongside declarations, without exposing them."""
    scripts: dict[str, str] = {}
    for argument in (server.command, *server.args):
        path = Path(argument)
        if argument.startswith("-") or path.suffix.lower() not in {".py", ".js", ".mjs", ".cjs", ".sh"}:
            continue
        path = (Path(server.cwd or ".") / path).resolve()
        if path.is_file():
            with path.open("rb") as source:
                digest = hashlib.file_digest(source, "sha256").hexdigest()
            scripts[str(path)] = digest
    return _fingerprint({"config": asdict(server), "scripts": scripts})


def _fresh_module(reference: str, workspace: Path) -> tuple[str, ModuleType, str, str]:
    module_name, separator, entry = reference.rpartition(":")
    if not separator or not module_name or not entry.isidentifier():
        raise ValueError("Invalid plugin reference; expected path.py:setup or installed.module:setup")
    package = ""
    package_paths: list[str] = []
    if module_name.endswith(".py"):
        path = (workspace / Path(module_name).expanduser()).resolve()
    else:
        spec = importlib.util.find_spec(module_name)
        if spec is None or not spec.origin or not spec.origin.endswith(".py"):
            raise ValueError("Reloadable plugins require a Python source module")
        path = Path(spec.origin)
        package = module_name.rpartition(".")[0]
        if spec.submodule_search_locations is not None:
            package_paths = list(spec.submodule_search_locations)
    name = f"{package + '.' if package else ''}_ngn_plugin_{uuid.uuid4().hex}"
    module = ModuleType(name)
    module.__spec__ = importlib.util.spec_from_file_location(
        name, path, submodule_search_locations=package_paths or None
    )
    if module.__spec__ is not None:
        module.__loader__ = module.__spec__.loader
    module.__file__, module.__package__ = str(path), name if package_paths else package
    if package_paths:
        module.__path__ = package_paths
    sys.modules[name] = module
    try:
        # Compile the current bytes, bypassing timestamp/size .pyc caches. Old
        # wrappers retain their distinct module globals until their calls finish.
        source = path.read_bytes()
        exec(compile(source, str(path), "exec"), module.__dict__)
    except BaseException:
        for loaded in tuple(sys.modules):
            if loaded == name or loaded.startswith(name + "."):
                sys.modules.pop(loaded, None)
        raise
    return name, module, entry, hashlib.sha256(source).hexdigest()


class HarnessResources(AgentPlugin):
    """Stable lifecycle dispatcher for newly loaded generations on every response."""

    def __init__(self, harness: Harness) -> None:
        self.harness = harness
        self.current = _Generation()
        self.active = self.current
        self.pinned: dict[str, ToolDefinition] = {}
        self._baseline_tools: dict[str, ToolDefinition] = {}
        self._owned_names: set[str] = set()
        self._retained_names: set[str] = set()
        self._expected: dict[str, ToolDefinition | None] = {}
        self._snapshot_ready = False
        self._commands: set[str] = set()
        self._aliases: set[str] = set()
        self._baseline_settings: dict[str, object] = {}
        self._message = Message(role="user", content="")
        self._running = False
        self._reload_lock = asyncio.Lock()
        self.last_error = ""

    def definition(self, name: str) -> ToolDefinition | None:
        if self._running and self._snapshot_ready:
            return self.pinned.get(name)
        return self.harness.agent.tool_registry.get(name)

    def owns_definition(self, definition: ToolDefinition) -> bool:
        """Whether a definition belongs to an active or ready reload generation."""
        return any(
            definition.name in generation.owned_names and generation.registry.get(definition.name) is definition
            for generation in (self.active, self.current)
        )

    def approval_identity(self, definition: ToolDefinition) -> str:
        """Captured origin digest for the exact advertised generation, never disk-now."""
        for generation in (self.active, self.current):
            original = generation.registry.get(definition.name)
            if original is not None and original.func is definition.func:
                identity = generation.approval_identities.get(definition.name, "")
                if identity:
                    return identity
        return ""

    def is_mcp(self, definition: ToolDefinition) -> bool:
        return any(
            definition.name in generation.mcp_names and generation.registry.get(definition.name) is definition
            for generation in (self.active, self.current)
        )

    def unchanged(self, name: str, definition: ToolDefinition) -> bool:
        """Permit our intentional generation swap, never a concurrent replacement."""
        latest = self.harness.agent.tool_registry.get(name)
        if latest is definition:
            return True
        return (
            self._running
            and self.pinned.get(name) is definition
            and name in self._retained_names
            and latest is self._expected.get(name)
        )

    async def reload(self, *, initial: bool = False) -> None:
        async with self._reload_lock:
            await self._reload(initial=initial)

    async def _reload(self, *, initial: bool) -> None:
        harness = self.harness
        if harness.config.demo or (harness._is_subagent and not harness.supports_child_custom_tools):
            return
        fresh = _Generation()
        registry = harness.agent.tool_registry
        # Baseline includes current host registrations, restoring definitions
        # shadowed by the retiring extension only when it still owns that slot.
        baseline = {tool.name: tool for tool in ToolRegistry.get_all(registry)}
        for name in self._owned_names:
            if baseline.get(name) is self.current.registry.get(name):
                baseline.pop(name, None)
                if name in self._baseline_tools:
                    baseline[name] = self._baseline_tools[name]
        fresh.registry._tools = {
            name: replace(tool, parameters=copy.deepcopy(tool.parameters)) for name, tool in baseline.items()
        }
        commands = CommandRegistry(harness)
        commands._registered = {
            name: value for name, value in harness.commands._registered.items() if name not in self._commands
        }
        commands._aliases = {
            name: value for name, value in harness.commands._aliases.items() if name not in self._aliases
        }
        instructions_before = dict(harness.instructions)
        prompt_before = harness.agent.system_prompt
        generated_before = harness._generated_system_prompt
        agent_scope = _AgentScope(harness, fresh.registry, fresh.hooks)
        scope = _HarnessScope(harness, agent_scope, commands)
        try:
            references, servers = resource_settings(harness.config)
            fresh.references = references
            scope._config.plugins = references
            scope._config.mcp_servers = servers
            config_identity = _fingerprint(asdict(scope._config))
            for reference in references:
                name, module, entry, source_digest = _fresh_module(reference, harness.workspace)
                before_plugin = dict(fresh.registry._tools)
                fresh.modules.append(name)
                setup = getattr(module, entry)
                if not callable(setup):
                    raise TypeError("Plugin setup entry must be callable")
                with commands.plugin_source(reference):
                    result = setup(scope)
                    if inspect.isawaitable(result):
                        result = await result
                if result is not None:
                    if not isinstance(result, AgentPlugin):
                        raise TypeError("setup(harness) must return AgentPlugin or None")
                    fresh.hooks.append(result)
                for tool in fresh.registry.get_all():
                    if before_plugin.get(tool.name) is not tool:
                        fresh.approval_identities[tool.name] = _fingerprint(
                            {
                                "kind": "python",
                                "reference": reference,
                                "source": source_digest,
                                "config": config_identity,
                                "tool": tool.name,
                                "function": getattr(tool.func, "__qualname__", ""),
                                "implementation": _callable_origin(tool),
                            }
                        )
            if _fingerprint(asdict(scope._config)) != config_identity:
                raise ReloadContractError("Reloadable plugin setup cannot mutate its configuration snapshot")
            if servers and harness.mode == "reviewer":
                raise PermissionError("Read-only agents cannot start MCP subprocesses")
            # Sequential adds make ownership immediate; failure aborts this
            # candidate and closes every already-started client.
            server_origins: dict[str, str] = {}
            for server in servers:
                origin = _mcp_origin(server)
                await fresh.manager.add_server(server, strict=True)
                if _mcp_origin(server) != origin:
                    raise ValueError("MCP source changed during startup; retry discovery")
                server_origins[server.name] = origin
            for tool in await fresh.manager.get_tool_definitions():
                if tool.func is not None:
                    fresh.registry.register(tool.func, tool.name, tool.description, tool.parameters)
                    fresh.mcp_names.add(tool.name)
                    server_name = fresh.manager._tool_map[tool.name].server_name
                    client = fresh.manager._clients[server_name]
                    fresh.approval_identities[tool.name] = _fingerprint(
                        {
                            "kind": "mcp",
                            "config": asdict(client.config),
                            "source": server_origins[server_name],
                            "server": client.server_info,
                            "tool": tool.name,
                            "description": tool.description,
                            "schema": tool.parameters,
                        }
                    )
            if any(not isinstance(hook, AgentPlugin) for hook in fresh.hooks):
                raise TypeError("Plugin hooks must be AgentPlugin instances")
            fresh.settings = dict(agent_scope._settings)
            if "streaming" in fresh.settings and type(fresh.settings["streaming"]) is not bool:
                raise ValueError("Plugin streaming must be a boolean")
            if "system_prompt" in fresh.settings and not isinstance(fresh.settings["system_prompt"], str | type(None)):
                raise ValueError("Plugin system_prompt must be text or None")
            if "max_tool_rounds" in fresh.settings and (
                type(fresh.settings["max_tool_rounds"]) is not int or not 1 <= fresh.settings["max_tool_rounds"] <= 1000
            ):
                raise ValueError("Plugin max_tool_rounds must be an integer between 1 and 1000")
            # Validate all policy/instruction/skill inputs before committing.
            harness.tool_settings.load()
            await harness.agent.refresh_skills()
            authored = harness.agent.system_prompt != harness._generated_system_prompt
            prompt = harness.agent.system_prompt
            harness.load_project_instructions()
            for name in tuple(harness.instructions):
                if name != "AGENTS.md":
                    harness.tools.instructions(Path(name))
            if authored:
                harness.agent.system_prompt = prompt
        except BaseException as error:
            cleanup_failed = False
            try:
                await finish_on_cancel(fresh.close())
            except Exception:
                cleanup_failed = True
            finally:
                harness.instructions = instructions_before
                harness.agent.system_prompt = prompt_before
                harness._generated_system_prompt = generated_before
            if not isinstance(error, Exception):
                raise
            if initial:
                raise RuntimeError(f"Extension initialization failed: {type(error).__name__}: {error}") from error
            # Do not echo plugin/config exception text: it can contain credentials.
            self.last_error = f"Resource reload failed ({type(error).__name__}); previous tools and MCP connections retained. Check trusted extension configuration."
            if isinstance(error, ReloadContractError):
                self.last_error += f" {error}"
            if cleanup_failed:
                self.last_error += " A rejected plugin's cleanup also failed."
            if self.last_error not in harness.diagnostics:
                harness.diagnostics.append(self.last_error)
            await harness.emit(Notice(self.last_error, "warning"))
            return
        previous_error = self.last_error
        if previous_error in harness.diagnostics:
            harness.diagnostics.remove(previous_error)
        self.last_error = ""
        if harness.instructions != instructions_before:
            # Refreshing the cache must not make an old edit snapshot appear
            # to have observed newly changed project instructions.
            harness.tools.read_hashes.clear()
        for name, original in baseline.items():
            replacement = fresh.registry.get(name)
            if replacement == original:
                fresh.registry._tools[name] = original
        old = self.current
        if self._running:
            self._retained_names.update(self._owned_names)
        self._owned_names = {
            name
            for name in baseline.keys() | fresh.registry._tools.keys()
            if baseline.get(name) is not fresh.registry.get(name)
        }
        fresh.owned_names = set(self._owned_names)
        self._baseline_tools = {name: baseline[name] for name in self._owned_names if name in baseline}
        self._commands = set(commands._registered) - (set(harness.commands._registered) - self._commands)
        self._aliases = set(commands._aliases) - (set(harness.commands._aliases) - self._aliases)
        registry._tools = dict(fresh.registry._tools)
        self._expected = {name: registry.get(name) for name in self.pinned}
        harness.commands._registered, harness.commands._aliases = commands._registered, commands._aliases
        for name in old.settings.keys() | fresh.settings.keys():
            if name not in self._baseline_settings:
                self._baseline_settings[name] = getattr(harness.agent, name)
            setattr(harness.agent, name, fresh.settings.get(name, self._baseline_settings[name]))
        object.__setattr__(agent_scope, "_ready", True)
        self.current = fresh
        harness.loaded_plugins[:] = references
        harness.config.plugins = references
        if initial:
            harness.diagnostics.extend(f"Loaded trusted Python plugin: {reference}" for reference in references)
        if not self._running:
            self.active = fresh
        if old is not self.active:
            await finish_on_cancel(old.close())
        if not authored and "system_prompt" not in fresh.settings:
            harness.refresh_instructions()

    async def before_run(self, context: RunContext, message: Message) -> Message:
        # Also refresh between user messages; the post-response path below is
        # unconditional and does not depend on a changed-file detector.
        await self.reload()
        self._running = True
        self.active = self.current
        self.active.used = True
        self._message = copy.deepcopy(message)
        for hook in self.active.hooks:
            message = await hook.before_run(context, message)
        return message

    async def before_model(self, context: RunContext, request: ModelRequest) -> ModelRequest:
        if self.active is not self.current:
            await self._retire_active(context)
            self.active = self.current
            self.active.used = True
            for hook in self.active.hooks:
                # New hook instances initialize for this continuous run. Their
                # before_run text does not rewrite the already persisted user turn.
                await hook.before_run(context, copy.deepcopy(self._message))
        for hook in self.active.hooks:
            request = await hook.before_model(context, request)
            if not isinstance(request, ModelRequest):
                raise TypeError("Plugin before_model must return ModelRequest")
        registry = self.harness.agent.tool_registry
        self.pinned = {}
        self._snapshot_ready = True
        self._retained_names.clear()
        for tool in request.tools:
            original = registry.get(tool.name)
            self.pinned[tool.name] = (
                original
                if original is not None and original.func is tool.func and original.parameters == tool.parameters
                else tool
            )
        # Legacy history may still call the hidden native scheduling alias.
        canonical, alias = self.pinned.get("schedule_wakeup"), registry.get("wake_up_in")
        if (
            canonical is not None
            and alias is not None
            and canonical.func == alias.func == self.harness.tools.schedule_wakeup
        ):
            self.pinned[alias.name] = alias
        return request

    async def after_model(self, context: RunContext) -> None:
        for hook in self.active.hooks:
            await hook.after_model(context)
        await self.reload()

    async def before_tool(self, context: RunContext, call: ToolCall) -> ToolCall:
        for hook in self.active.hooks:
            call = await hook.before_tool(context, call)
        return call

    async def after_tool(self, context: RunContext, result: ToolResultEvent) -> ToolResultEvent:
        for hook in self.active.hooks:
            result = await hook.after_tool(context, result)
        return result

    async def on_event(self, context: RunContext, event: Event) -> None:
        for hook in self.active.hooks:
            await hook.on_event(context, event)

    async def _retire_active(self, context: RunContext) -> None:
        old = self.active
        try:
            if old.used:
                old.used = False
                errors: list[BaseException] = []
                for hook in old.hooks:
                    try:
                        await hook.after_run(context)
                    except BaseException as error:
                        errors.append(error)
                if errors:
                    raise BaseExceptionGroup("Extension run cleanup failed", errors)
        finally:
            if old is not self.current:
                await finish_on_cancel(old.close())

    async def after_run(self, context: RunContext) -> None:
        try:
            await self._retire_active(context)
        finally:
            self.active = self.current
            self._running = False
            self.pinned.clear()
            self._snapshot_ready = False

    async def aclose(self) -> None:
        try:
            await self.active.close()
        finally:
            await self.current.close()
