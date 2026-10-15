"""Customer-facing coding harness, using the library's unmodified Agent loop."""

import asyncio
import copy
import hashlib
import logging
import uuid
from collections.abc import AsyncGenerator
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Iterator
from contextlib import aclosing
from contextlib import contextmanager
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite

from nagents._async import join_owned as _await_cleanup
from nagents.agent import Agent
from nagents.compaction import ModelCompaction
from nagents.events import DoneEvent
from nagents.events import ErrorEvent
from nagents.observation import scope as observation_scope
from nagents.provider.openai import OpenAIProvider
from nagents.session import SessionManager
from nagents.session import forks
from nagents.types import ContentPart
from nagents.types import GenerationConfig
from nagents.types import ReasoningEffort
from nagents.types import TextContent

from .agent_catalog import AgentCatalog
from .auth import OpenAIAuth
from .commands import CommandRegistry
from .config import AgentProfile
from .connection import build_provider
from .credentials import ProviderLogin
from .credentials import ProviderLoginStore
from .default_instructions import load_default_instructions
from .execution import ExecutionBridge
from .execution import _is_owned_adapter
from .execution import _register_owned_adapter_type
from .execution import capture_write
from .execution import host_run_id
from .execution import observe_host_event
from .execution import publish_anchor
from .followups import FollowupReady
from .followups import RunInput
from .followups import RunInputs
from .followups import RunTurn
from .model_bindings import ModelBinding
from .model_bindings import ResolvedModelBinding
from .model_bindings import resolve_model_binding
from .model_catalog import ModelCatalog
from .provider import DemoCompaction
from .provider import HarnessProvider
from .provider import UnavailableModelProvider
from .providers import ProviderProfile
from .providers import ProviderRegistry
from .providers import ScopedProviderRegistryStore
from .reasoning import ReasoningPreference
from .reasoning import resolve_effort
from .resources import HarnessResources
from .skills import HarnessSkillDiscoverer
from .subagents import SubagentManager
from .tool_config import HarnessToolRegistry
from .tool_config import WorkspaceTools
from .tools import CodingTools
from .tools import HarnessExecutor
from .types import ApprovalRequest
from .types import HarnessEvent
from .types import Notice
from .types import SessionInfo
from .types import TaskActivity
from .types import TaskCompleted
from .types import TaskMessage
from .types import ToolOutput

if TYPE_CHECKING:
    import sqlite3

    from nagents.context_stats import ContextStats
    from nagents.events import CompactionDoneEvent
    from nagents.provider import Provider
    from nagents.types import Message
    from nagents.types import ToolArguments
    from nagents.types import ToolDefinition

    from .auth import DeviceAuthorization
    from .config import HarnessConfig
    from .model_catalog import ModelCapabilities
    from .types import ApprovalHandler
    from .types import WakeupHandler

_INHERIT_MODEL = ModelBinding()


async def _deny(request: ApprovalRequest) -> bool:
    return False


def _prompt_text(prompt: str | list["ContentPart"]) -> str:
    """Plain-text projection of a run prompt for titles and wake-up plumbing."""
    if isinstance(prompt, str):
        return prompt
    return "\n".join(part.text for part in prompt if isinstance(part, TextContent))


class _HarnessSession(SessionManager):
    """Let local DB operations release their connections before propagating cancellation.

    In particular, cancelling aiosqlite during connection setup can orphan the
    connection created by its worker. This adapter is local to the harness;
    model requests and tool execution remain immediately cancellable.
    """

    async def get_or_create_session(self, session_id: str, user_id: str) -> str:
        return await _await_cleanup(asyncio.create_task(super().get_or_create_session(session_id, user_id)))

    async def get_history(self, session_id: str, limit: int | None = None) -> list["Message"]:
        return await _await_cleanup(asyncio.create_task(super().get_history(session_id, limit)))

    async def context_identity(self, session_id: str) -> int:
        return await _await_cleanup(asyncio.create_task(super().context_identity(session_id)))

    async def session_exists(self, session_id: str) -> bool:
        return await _await_cleanup(asyncio.create_task(super().session_exists(session_id)))

    async def add_message(self, session_id: str, message: "Message") -> int:
        reservation = capture_write(self, session_id, message)
        row_id = await _await_cleanup(asyncio.create_task(super().add_message(session_id, message)))
        if reservation is not None and type(row_id) is int and row_id > 0:
            reservation.row_id = row_id
            await publish_anchor(self, session_id, reservation)
        return row_id

    async def replace_context(self, session_id: str, messages: list["Message"]) -> None:
        await _await_cleanup(asyncio.create_task(super().replace_context(session_id, messages)))


_register_owned_adapter_type(_HarnessSession)


class Harness:
    supports_child_custom_tools = False

    def reasoning_effort(self) -> ReasoningEffort:
        return resolve_effort(
            self._reasoning_effort_override or self.current_profile().reasoning_effort,
            self.config.reasoning_effort,
        )

    def generation_config(self) -> "GenerationConfig | None":
        effort = self.reasoning_effort()
        if not effort and not self.config.reasoning_summaries:
            return None
        return GenerationConfig(
            reasoning_summary="auto" if self.config.reasoning_summaries else "off", reasoning_effort=effort
        )

    def create_defined_child(self, name: str) -> "Harness | None":
        """Optional definition-driven construction; ordinary profiles use cloning."""
        return None

    def load_project_instructions(self) -> None:
        """Coding harnesses discover workspace instructions during initialization."""
        self.tools.instructions(Path("AGENTS.md"))

    @property
    def providers(self) -> ProviderRegistry:
        return self._providers

    @providers.setter
    def providers(self, registry: ProviderRegistry) -> None:
        self._providers = replace(registry, providers=dict(registry.providers))
        self.config.providers = {
            **({"": self.config.providers[""]} if "" in self.config.providers else {}),
            **registry.providers,
        }

    def __init__(
        self,
        config: "HarnessConfig",
        *,
        allow_subagents: bool = True,
        provider_registry: ProviderRegistry | None = None,
        shared_auth: OpenAIAuth | None = None,
        model_binding: ModelBinding = _INHERIT_MODEL,
    ) -> None:
        """Build a runtime, optionally inheriting an accepted connection snapshot.

        Independent roots load current connection settings. Children can retain
        their parent's accepted routing without rereading an operator's later
        edits. Shared authentication remains owned by the supplying runtime.
        """
        self._reasoning_effort_override: ReasoningPreference = ""
        self.config = config
        self._agent_catalog = AgentCatalog(config)
        self._current_profile_name = config.agent
        self._current_profile = replace(config.profile(config.agent))
        self._generation_profiles: dict[str, AgentProfile] = {}
        self._generation_source_profiles: dict[str, AgentProfile] = {}
        self._generation_model_bindings: dict[str, ModelBinding] = {}
        self._generation_model_errors: dict[str, str] = {}
        self._agent_generation_active = False
        self._preserve_first_profile = False
        self.allow_subagents = allow_subagents
        self._is_subagent = False
        self.subagent_depth = 0
        self._task_id = ""
        self._task_name = ""
        self._activation = 0
        self._delivery_definition: ToolDefinition | None = None
        self._delivery_binding: ToolDefinition | None = None
        self._permission_ceiling = "reviewer" if config.read_only else "build"
        self._permission_parents: tuple[Harness, ...] = ()
        self._inherited_tool_profiles: tuple[AgentProfile, ...] = ()
        self._approval_lock = asyncio.Lock()
        self._owns_auth = shared_auth is None
        self._parent_instructions = ""
        self._inherited_profile_instructions: tuple[str, ...] = ()
        self._generated_system_prompt = ""
        self.workspace: Path = config.workspace.resolve()
        if not self.workspace.is_dir():
            raise ValueError(f"Workspace is not a directory: {self.workspace}")
        scope = hashlib.sha256(str(self.workspace).encode("utf-8")).hexdigest()[:32]
        self.session_id = f"ngn-{uuid.uuid4().hex[:16]}"
        self.approval_handler: ApprovalHandler = _deny
        self.wakeup_handler: WakeupHandler | None = None
        self.instructions: dict[str, str] = {}
        self.diagnostics: list[str] = list(config.diagnostics)
        self.loaded_plugins: list[str] = []
        self.openai_auth = OpenAIAuth() if shared_auth is None else shared_auth
        self.login_store = ProviderLoginStore()
        self.provider_store = ScopedProviderRegistryStore(self.workspace, paths=config.provider_paths)
        self.providers = self.provider_store.load() if provider_registry is None else provider_registry
        initial_profile = config.profile(config.agent)
        self._default_provider_name = config.provider or config.unavailable_model_binding.provider
        self._selected_model = config.model
        selected_binding = (
            model_binding
            if model_binding.configured
            else self._resolve_agent_model(config.agent, initial_profile, require_available=False).binding
        )
        missing_connection = bool(
            selected_binding.provider and selected_binding.provider not in self.providers.providers
        )
        if selected_binding.configured:
            config.provider = "" if missing_connection else selected_binding.provider
            config.model = selected_binding.id
        config.unavailable_model_binding = selected_binding if missing_connection else ModelBinding()
        self._built_provider_name = config.provider
        self._built_provider_profile = config.provider_profile()
        self._api_model = config.model
        self._selected_model_explicit = config.model_explicit
        self._initialized = False
        self._session_created = False
        self._closed = False
        self._initialization_error = ""
        self._init_lock = asyncio.Lock()
        self._busy = ""
        self._queue: asyncio.Queue[HarnessEvent | RunTurn] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._closing: asyncio.Task[None] | None = None
        self.tools = CodingTools(self)
        self.tool_settings = WorkspaceTools(self.workspace)
        self.agent = Agent(
            provider=(
                UnavailableModelProvider(selected_binding)
                if missing_connection
                else build_provider(config.providers[config.provider], config, self.openai_auth)
                if config.provider
                else HarnessProvider(config, self.login_store if not config.provider else None)
            ),
            session_manager=_HarnessSession(config.data_dir / scope / "sessions.db"),
            streaming=True,
            max_tool_rounds=config.max_tool_rounds,
            save_tool_outputs=False,
            compaction_strategy=DemoCompaction() if config.demo else ModelCompaction(),
            skill_discoverer=HarnessSkillDiscoverer(self.tools),
            skill_token_limit=config.skill_token_limit,
        )
        self.agent.tool_registry = HarnessToolRegistry(self)
        self.tools.register()
        self.commands = CommandRegistry(self)
        self.tasks = SubagentManager(self)
        self.followups = RunInputs()
        self.tasks.register()
        self.resources = HarnessResources(self)
        self.agent.tool_executor = HarnessExecutor(self, self.tools)
        self.agent.workspace = self.workspace
        if initial_profile.model or model_binding.configured:
            self.config.model_explicit = True
        self._defer_prompt_capabilities = True
        self.refresh_instructions()
        self._defer_prompt_capabilities = False

    @property
    def mode(self) -> str:
        return self.mode_for_profile(self.config.agent)

    @property
    def profile_revision(self) -> str:
        """Opaque catalog revision; contains no instruction bodies."""
        return self._agent_catalog.revision

    def resolved_model_binding(self, agent: str = "") -> ResolvedModelBinding:
        """Resolve a catalog model selection without changing the running provider."""
        name = agent or self.config.agent
        return self._resolve_agent_model(name, self.config.profile(name))

    def clone_configuration(self) -> "HarnessConfig":
        """Copy host defaults without promoting this runtime's agent override."""
        config = copy.deepcopy(self.config)
        missing = bool(self._default_provider_name and self._default_provider_name not in config.providers)
        config.provider = "" if missing else self._default_provider_name
        config.model = self._selected_model
        config.unavailable_model_binding = (
            ModelBinding(self._default_provider_name, self._selected_model) if missing else ModelBinding()
        )
        return config

    def _default_model_choices(self) -> tuple[ModelBinding, ModelBinding, ModelBinding]:
        builtin = ModelBinding(self._default_provider_name, self._selected_model)
        if self.config.model_config_explicit:
            return ModelBinding(), ModelBinding(), builtin
        if not self._default_provider_name and "" in self.config.providers:
            workspace = self.provider_store.model_store("workspace").load_binding()
            global_default = self.provider_store.model_store("global").load_binding()
        else:
            workspace = self.provider_store.model_binding("workspace")
            global_default = self.provider_store.model_binding(
                "global", legacy_provider=self._default_provider_name or self.providers.active
            )
        return workspace, global_default, builtin

    def default_model_binding(self) -> ResolvedModelBinding:
        """Workspace/global inheritance without any agent definition or overlay."""
        workspace, global_default, builtin = self._default_model_choices()
        local_names = self.provider_store.load_scope("workspace").providers
        return resolve_model_binding(
            providers=self.providers.providers,
            workspace=workspace,
            global_default=global_default,
            builtin=builtin,
            origins={name: "workspace" if name in local_names else "global" for name in self.providers.providers},
            sources={
                "workspace": str(self.provider_store.model_store("workspace").path),
                "global": str(self.provider_store.model_store("global").path),
            },
        )

    def definition_model_binding(self, name: str) -> ModelBinding:
        """The source definition's selection, ignoring its editable UI overlay."""
        profile = self.config.profile(name)
        if not (profile.model or profile.provider):
            return ModelBinding()
        default = self.default_model_binding().binding
        return ModelBinding(profile.provider or default.provider, profile.model or default.id)

    def _resolve_agent_model(
        self, name: str, profile: AgentProfile, *, require_available: bool = True
    ) -> ResolvedModelBinding:
        preference = self.provider_store.agent_models.load(name)
        workspace, global_default, builtin = self._default_model_choices()
        # Existing embeddings can supply an unnamed provider and scalar model.
        # Persisted selections always contain a named connection and model ID.
        route = profile.provider or workspace.provider or global_default.provider or builtin.provider
        source = str(preference.path)
        if preference.revision != "0" * 64:
            agent_binding = preference.binding
        elif (profile.model or profile.provider) and route:
            agent_binding = ModelBinding(route, profile.model or workspace.id or global_default.id or builtin.id)
            source = profile.source
        else:
            agent_binding = ModelBinding()
            if profile.model:
                # Scalar profile models predate named connections. Their
                # precedence is still agent-level; do not demote them beneath
                # a legacy global ID merely because this embedding is unnamed.
                return ResolvedModelBinding(ModelBinding("", profile.model), "agent", profile.source)
        local_names = self.provider_store.load_scope("workspace").providers
        return resolve_model_binding(
            providers=self.providers.providers,
            agent=agent_binding,
            workspace=workspace,
            global_default=global_default,
            builtin=builtin,
            origins={name: "workspace" if name in local_names else "global" for name in self.providers.providers},
            sources={
                "agent": source,
                "workspace": str(self.provider_store.model_store("workspace").path),
                "global": str(self.provider_store.model_store("global").path),
            },
            require_available=require_available,
        )

    def delegation_model_binding(self, name: str, *, model: str = "", provider: str = "") -> ModelBinding:
        if model and provider:
            # A complete invocation selection takes precedence even when a
            # profile's saved default references an unavailable connection.
            if provider not in self.providers.providers:
                raise ValueError(f"Model references unavailable provider connection {provider!r}")
            return ModelBinding(provider, model)
        if self._agent_generation_active:
            if name in self._generation_model_errors:
                raise ValueError(self._generation_model_errors[name])
            if name not in self._generation_model_bindings:
                raise ValueError(f"Agent {name!r} was not available in this model generation")
            resolved = self._generation_model_bindings[name]
        else:
            resolved = self._resolve_agent_model(name, self.delegation_profile(name)).binding
        if model or provider:
            return ModelBinding(provider or resolved.provider, model or resolved.id)
        return resolved

    def refresh_agent_definitions(self) -> bool:
        """Refresh the discoverable catalog without changing an accepted tool batch."""
        return self._agent_catalog.refresh(self.config)

    def current_profile(self) -> AgentProfile:
        """The active identity survives source removal until this runtime is retired."""
        if self.config.agent != self._current_profile_name:
            self._current_profile_name = self.config.agent
            self._current_profile = replace(self.config.profile(self.config.agent))
        elif (
            not self._agent_generation_active
            and not self._preserve_first_profile
            and self.config.agent in self.config.profile_names
        ):
            self._current_profile = replace(
                self.config.profile(self.config.agent),
                model=self._current_profile.model,
                provider=self._current_profile.provider,
            )
        return self._current_profile

    def activate_agent_definitions(self) -> None:
        """Accept current definitions only at a new model-generation boundary."""
        profile = self.current_profile()
        if self._preserve_first_profile:
            self._preserve_first_profile = False
        elif self.config.agent in self.config.profile_names:
            fresh = self.config.profile(self.config.agent)
            # Routing belongs to the accepted runtime. Editing a profile's route
            # affects new selections/children, never swaps a live provider owner.
            self._current_profile = replace(fresh, model=profile.model, provider=profile.provider)
        self._generation_profiles = {name: replace(value) for name, value in self.config.profiles.items()}
        self._generation_source_profiles = {
            name: replace(value) for name, value in self.config.agent_source_profiles.items()
        }
        self._agent_generation_active = True
        self._generation_model_bindings = {}
        self._generation_model_errors = {}
        for name in ("assistant", *sorted(self._generation_profiles)):
            try:
                binding = self._resolve_agent_model(name, self.delegation_profile(name)).binding
            except (OSError, ValueError) as error:
                self._generation_model_errors[name] = str(error)
            else:
                self._generation_model_bindings[name] = binding

    def delegation_profiles(self) -> dict[str, AgentProfile]:
        """Profiles advertised to this generation, or the idle catalog."""
        return self._generation_profiles if self._agent_generation_active else self.config.profiles

    def delegation_source_profiles(self) -> dict[str, AgentProfile]:
        return self._generation_source_profiles if self._agent_generation_active else self.config.agent_source_profiles

    def delegation_profile(self, name: str) -> AgentProfile:
        if name == "assistant":
            return self.config.profile(name)
        profiles = self.delegation_profiles()
        if name not in profiles:
            raise ValueError(f"Unknown agent profile {name!r}")
        return profiles[name]

    def mode_for_profile(self, name: str) -> str:
        """Effective permissions for a profile, including inherited ceilings."""
        profile = self.current_profile() if name == self.config.agent else self.config.profile(name)
        if (
            self.config.read_only
            or self._permission_ceiling == "reviewer"
            or (self._is_subagent and self.tasks.root.harness.mode == "reviewer")
            or any(parent.mode == "reviewer" for parent in self._permission_parents)
        ):
            return "reviewer"
        return profile.mode

    def inherit_tool_permissions(self, parent: "Harness", *, continuing: bool = False) -> None:
        """Retain the original ceiling and enforce later ancestor restrictions."""
        parent.refresh_tool_policy()
        self._permission_parents = parent._permission_parents if continuing else (*parent._permission_parents, parent)
        denied = tuple(
            name for name, enabled in parent.tool_settings.agents.get(parent.config.agent, {}).items() if not enabled
        )
        self._inherited_tool_profiles = (
            *parent._inherited_tool_profiles,
            replace(parent.current_profile()),
            AgentProfile(denied_tools=denied),
        )

    def refresh_tool_policy(self) -> None:
        self.tool_settings.load()
        for parent in self._permission_parents:
            parent.tool_settings.load()

    def tool_enabled(self, name: str) -> bool:
        profiles = (
            self.current_profile(),
            *self._inherited_tool_profiles,
            *(parent.current_profile() for parent in self._permission_parents),
        )
        return all(
            (profile.tools is None or name in profile.tools) and name not in profile.denied_tools
            for profile in profiles
        ) and all(owner.tool_settings.enabled(owner.config.agent, name) for owner in (self, *self._permission_parents))

    @property
    def delegatable_profile_names(self) -> tuple[str, ...]:
        policies = (
            self.current_profile(),
            *self._inherited_tool_profiles,
            *(parent.current_profile() for parent in self._permission_parents),
        )
        return tuple(
            name
            for name in ("assistant", *sorted(self.delegation_profiles()))
            if self.delegation_profile(name).subagent_available
            and all(profile.allowed_agents is None or name in profile.allowed_agents for profile in policies)
        )

    def delegation_catalog(self) -> str:
        return "; ".join(
            f"{name}: {self.delegation_profile(name).description or 'Configured agent'}"
            for name in self.delegatable_profile_names
        )

    @property
    def can_delegate(self) -> bool:
        root = self.tasks.root.harness if self._is_subagent else self
        return (
            self.allow_subagents
            and root.allow_subagents
            and self.subagent_depth < min(self.config.max_subagent_depth, root.config.max_subagent_depth)
            and bool(self.delegatable_profile_names)
        )

    @contextmanager
    def operation(self, name: str) -> Iterator[None]:
        if self._closed:
            raise RuntimeError("Harness is closed")
        if self._busy:
            raise RuntimeError(f"Harness is busy ({self._busy}); concurrent runs and session mutations are not allowed")
        self._busy = name
        try:
            yield
        finally:
            self._busy = ""

    def inherited_run_instructions(self, *, continuing: bool = False) -> tuple[str, tuple[str, ...]]:
        """Inherit authored context without recursively copying generated scaffolding."""
        prompt = self.agent.system_prompt or ""
        if prompt != self._generated_system_prompt:
            # A caller may replace or edit any part of the prompt. Preserve it whole:
            # generated-looking text could now contain authored instructions.
            return prompt, ()
        profiles = self._inherited_profile_instructions
        instructions = self.current_profile().instructions
        if not continuing and instructions and instructions not in profiles:
            profiles += (instructions,)
        return self._parent_instructions, profiles

    def refresh_instructions(self) -> None:
        self.tools.refresh_limits()
        profile = self.current_profile()
        # Construction defers workspace configuration I/O to initialization,
        # where failures are recorded for clients. Every later refresh resolves
        # the effective contracts and propagates invalid settings.
        advertised = self.agent.tool_registry.get_all() if not self._defer_prompt_capabilities else []
        scheduler_available = any(tool.func == self.tools.schedule_wakeup for tool in advertised)
        delegation_names = [tool.name for tool in advertised if tool.func == self.tasks.delegate]
        delegation_name = "delegate" if "delegate" in delegation_names else next(iter(delegation_names), "delegate")
        delegation_available = self.can_delegate and (self._defer_prompt_capabilities or bool(delegation_names))
        base = (
            f"You are ngn, a coding assistant working in {self.workspace}. Active profile: {self.config.agent} ({self.mode}).\n"
            f"\n{load_default_instructions()}\n\n"
        )
        if scheduler_available:
            base += (
                "Delayed self-follow-up is available only when a scheduler tool is advertised in this request. "
                "If none is advertised, do not promise a delayed follow-up. Scheduling acknowledges immediately; "
                "timers and task handles are process-local and do not survive restart. "
                "Scheduling and waking grant no additional tool permissions.\n"
            )
        else:
            base += "No native wake-up scheduler is advertised; do not promise delayed self-follow-up without an advertised scheduler.\n"
        if profile.instructions:
            base += f"\nTrusted profile instructions:\n{profile.instructions}\n"
        for instructions in self._inherited_profile_instructions:
            if instructions != profile.instructions:
                base += f"\nParent profile instructions (your own permission ceiling applies):\n{instructions}\n"
        if self._parent_instructions:
            base += (
                "\nParent run context (does not grant tools or permissions; your own profile and permission ceiling apply):\n"
                + self._parent_instructions
                + "\n"
            )
        if delegation_available:
            example_agent = (
                "assistant" if "assistant" in self.delegatable_profile_names else self.delegatable_profile_names[0]
            )
            base += (
                "\nHandle small or tightly coupled work directly unless delegation is requested. "
                f"When available in your tools, use {delegation_name}(prompt, agent='{example_agent}') for substantial independent work, or select an explicitly configured agent profile. "
                f"Available agents: {self.delegation_catalog()}. "
                "Children cannot exceed your permission ceiling; writes and shell still pass through the host approval policy. It returns immediately; "
                "continue useful independent work while children run. When only waiting, end this model turn without tool calls; "
                "the harness waits and resumes you with their untrusted background results. Then complete the task. "
                "No wake-up scheduler is needed to receive child results. "
                "Do not poll or duplicate already delegated work. There are at most 3 simultaneous "
                "and 8 total child executions across the entire tree per root user run, including human follow-ups. "
                f"Your depth is {self.subagent_depth}; delegation stops at depth {self.config.max_subagent_depth}. "
                "Full quotas fail immediately, never wait for a slot. Jobs do not survive cancellation or process restart; "
                "closed child conversations may resume for a human follow-up, scheduled wake-up, or immediate child's completion, "
                "never by replaying old acknowledgements. Results go to the immediate parent, whose synthesis propagates upward.\n"
            )
        else:
            base += "\nYou cannot delegate using native tools in this request. Continue your own task using your permitted tools.\n"
        if self._is_subagent:
            base += (
                "\nOnly built-in tools are supported in children; parent plugins and custom tools are not inherited.\n"
            )
        for path, content in sorted(self.instructions.items(), key=lambda item: (len(Path(item[0]).parts), item[0])):
            base += f"\nApplicable project context ({path}):\n{content}\n"
        self.agent.system_prompt = base
        self._generated_system_prompt = base

    async def initialize(self, *, create_session: bool = True) -> None:
        """Initialize local sessions, instructions and trusted extensions. No provider I/O.

        ``create_session=False`` is for read-only callers (listing, picking,
        history) that must not register an empty session for the current run.
        The current session is still created later, on first use, by ``run``,
        ``new_session`` or a caller passing the default ``create_session=True``.
        """
        if self._closed:
            raise RuntimeError("Harness is closed")
        async with self._init_lock:
            if not self._initialized:
                if self._initialization_error:
                    raise RuntimeError(self._initialization_error)
                try:
                    # pathlib's parents=True ignores mode for intermediate directories.
                    missing: list[Path] = []
                    directory = self.config.data_dir
                    while not directory.exists():
                        missing.append(directory)
                        directory = directory.parent
                    for directory in reversed(missing):
                        directory.mkdir(mode=0o700, exist_ok=True)
                    self.agent.session.db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    await self.agent.session.initialize()
                    self.agent.session.db_path.chmod(0o600)
                    async with aiosqlite.connect(self.agent.session.db_path) as db:
                        await db.execute(
                            "CREATE TABLE IF NOT EXISTS harness_sessions (id TEXT PRIMARY KEY, title TEXT NOT NULL)"
                        )
                        await db.commit()
                    self.load_project_instructions()
                    await self.agent.refresh_skills()
                    self.refresh_instructions()
                    if (
                        not self.config.demo
                        and not isinstance(self.agent.provider, UnavailableModelProvider)
                        and not self.config.provider
                        and self.config.provider_profile().kind in {"openai", "openai_compatible"}
                        and not self.config.provider_profile().base_url
                        and self.config.provider_profile().api == "auto"
                        and self.config.provider_profile().auth != "api-key"
                        and (self.config.provider_profile().auth == "chatgpt" or self.openai_auth.logged_in())
                    ):
                        await self._use_chatgpt()
                    if self.config.demo and self.config.plugins:
                        self.diagnostics.append(
                            "OFFLINE DEMO: configured Python plugins were not imported (trusted code could perform I/O)."
                        )
                    await self.resources.reload(initial=True)
                    self.agent.plugins.append(self.resources)
                    # Startup provenance belongs to diagnostics, not every user
                    # turn (including turns submitted by Live delegation).
                    for text in self.diagnostics:
                        self.resources.logger.log(
                            logging.WARNING if text.startswith("Ignoring ") else logging.INFO, "%s", text
                        )
                    self._initialized = True
                except BaseException as exc:
                    self._initialization_error = f"Harness initialization failed: {type(exc).__name__}: {exc}"
                    self.diagnostics.append(self._initialization_error)
                    raise
            if create_session and not self._session_created:
                await self.create_session(self.session_id)
                self._session_created = True

    async def load_plugin(self, reference: str) -> None:
        """Add an explicitly trusted extension through the same staged loader."""
        previous = self.config.plugins, self.config.cli_plugins
        self.config.plugins = (*self.config.plugins, reference)
        self.config.cli_plugins = (*self.config.cli_plugins, reference)
        try:
            await self.resources.reload(initial=True)
        except BaseException:
            self.config.plugins, self.config.cli_plugins = previous
            raise

    async def create_session(self, session_id: str) -> None:
        await self.agent.session.get_or_create_session(session_id, "harness")
        if self._is_subagent:
            return  # Raw child history is retained, without a parent session-picker entry.
        async with aiosqlite.connect(self.agent.session.db_path) as db:
            await db.execute("INSERT OR IGNORE INTO harness_sessions (id, title) VALUES (?, '')", (session_id,))
            await db.commit()

    async def approve(
        self, tool: str, arguments: "ToolArguments", description: str, preview: str = "", call_id: str = ""
    ) -> None:
        root = self.tasks.root.harness
        if self._is_subagent:
            description = f"[{self._task_name}, depth {self.subagent_depth}] {description}"
        request = ApprovalRequest(
            call_id or uuid.uuid4().hex,
            tool,
            description,
            copy.deepcopy(arguments),
            preview,
            self._task_id,
            self._task_name,
            self.subagent_depth,
            self._activation,
        )
        task_info = self.tasks.root._infos.get(self._task_id) if self._is_subagent else None
        if task_info is not None:
            await self.tasks._emit_activity(
                task_info,
                {"type": "status", "phase": "waiting_approval", "detail": f"Waiting for approval: {tool}"},
            )
        async with root._approval_lock:
            if self._closed or root._closed:
                raise PermissionError("Harness closed before approval; no action was taken")
            approved = await root.approval_handler(request)
            if task_info is not None:
                await self.tasks._emit_activity(task_info, {"type": "status", "phase": "running"})
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise asyncio.CancelledError
            if self._closed or root._closed:
                raise PermissionError("Harness closed during approval; no action was taken")
            if approved is not True:
                raise PermissionError(f"Approval denied for {tool}; no action was taken")
            self.refresh_tool_policy()
            if not self.tool_enabled(tool):
                raise PermissionError(
                    f"Tool {tool} is disabled for this agent by its inherited tool policy or .ngn/tools.yaml"
                )

    async def emit(self, event: HarnessEvent) -> None:
        if self._queue is not None:
            if isinstance(event, ToolOutput):
                invocation = observation_scope.get()
                generation, index = invocation.get("tool_generation_id"), invocation.get("tool_index")
                if (
                    invocation.get("tool_call_id") == event.call_id
                    and invocation.get("tool_name") == event.tool
                    and isinstance(generation, str)
                    and generation
                    and type(index) is int
                    and 0 <= index < 1024
                ):
                    event = replace(event, extra={**event.extra, "generation_id": generation, "index": index})
            observe_host_event(self, event)
            await self._queue.put(event)

    def emit_activity(self, event: TaskActivity) -> bool:
        """Try public inspection telemetry without delaying model/tool execution.

        Mandatory lifecycle events still use emit(). The task manager coalesces
        overflow into a history refresh marker once the consumer makes space.
        """
        if self._queue is None:
            return False
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            return False
        observe_host_event(self, event)
        return True

    async def run(self, prompt: str | list[ContentPart]) -> AsyncGenerator[HarnessEvent, None]:
        async with aclosing(self._run(prompt)) as events:
            async for event in events:
                yield event

    def submit_followup(self, session_id: str, message: RunInput) -> bool:
        """Admit a host-authored input to this root's next complete run boundary."""
        if (
            self._is_subagent
            or self._closed
            or self._worker is None
            or self._worker.done()
            or self._worker.cancelling()
        ):
            return False
        if not self.followups.admit(session_id, message):
            return False
        self.tasks._changed.set()
        return True

    async def wake(self, prompt: str, *, task_id: str = "") -> AsyncGenerator[HarnessEvent, None]:
        """Automatically activate this root or a retained child, without a budget reset.

        The lifecycle owner must serialize this with other runs and select the
        originating root session. Consume or close the iterator to own cleanup.
        Busy runs reject; no timer, background consumer, or durable replay is created here.
        """
        if self._is_subagent:
            raise PermissionError("Wake-ups must be submitted to the root harness")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 16_000:
            raise ValueError("Wake-up prompt must contain 1 to 16000 characters and not be blank")
        if task_id:
            self.tasks._lookup(task_id)
        async with aclosing(self._run(prompt, task_id=task_id, trigger="wakeup")) as events:
            async for event in events:
                yield event

    async def continue_task(
        self, task_id: str, prompt: str, *, message: RunInput | None = None
    ) -> AsyncGenerator[HarnessEvent, None]:
        """Explicitly continue a retained child conversation in this root session.

        Idle: stream task notices/results, then the parent's normal synthesis turn.
        Busy running: yield an acceptance Notice; task events and parent synthesis
        use the already-active run stream. Busy children/other operations reject.
        Closing an idle continuation cancels its whole tree, like closing run().
        """
        if self._is_subagent:
            raise PermissionError("Human follow-ups must be submitted to the root harness")
        self.tasks._lookup(task_id)
        if self._busy == "run":
            info = self.tasks.continue_task(task_id, prompt, message=message)
            yield Notice(f"Human follow-up accepted for {info.name}; task events will arrive on the active run.")
            return
        async with aclosing(self._run(prompt, task_id=task_id, initial_input=message)) as events:
            async for event in events:
                yield event

    async def start_task(
        self, prompt: str, agent: str = "assistant", *, message: RunInput | None = None
    ) -> AsyncGenerator[HarnessEvent, None]:
        """Start a configured profile directly, retaining ordinary root ownership.

        No synthetic main user turn is inserted. Child completion returns to the
        main assistant through its existing notification and synthesis boundary.
        Starting work does not reset the shared execution budget.
        """
        if self._is_subagent:
            raise PermissionError("Host profile starts must be submitted to the root harness")
        if message is not None and message.prompt != prompt:
            raise ValueError("Initial task input must match its prompt")
        if self._busy == "run":
            info = await self.tasks.start_task(prompt, agent, message=message)
            yield Notice(f"Task accepted for {info.name}; task events will arrive on the active run.")
            return
        async with aclosing(self._run(prompt, initial_input=message, start_profile=agent)) as events:
            async for event in events:
                yield event

    async def task_history(self, task_id: str, limit: int = 100) -> list["Message"]:
        """Read detached child messages without resuming it; see tasks.history()."""
        return await self.tasks.history(task_id, limit)

    async def _run(
        self,
        prompt: str | list[ContentPart],
        *,
        task_id: str = "",
        trigger: str = "human",
        notifications: tuple[TaskCompleted | TaskMessage, ...] = (),
        initial_input: RunInput | None = None,
        start_profile: str = "",
    ) -> AsyncGenerator[HarnessEvent, None]:
        """Stream the same core Agent.run, merging bounded live tool output.

        Consumers ending early must close the iterator (``contextlib.aclosing``).
        Both cancellation and aclose cancel AND await the producer and all children.
        Background notifications are batched between complete Agent.run calls;
        only the final parent DoneEvent is delivered after every job settles.
        Queued task lifecycle observations are emitted before the next core
        Agent event. Waiting for a tool result or model event can delay the
        update; streamed shell output alone does not trigger this drain.
        Cancellation discards undelivered notifications, retaining local /tasks
        status and explicit continuation handles within this process. This is not a durable job queue.
        """
        with self.operation("run"):
            prompt_text = _prompt_text(prompt)
            if not prompt_text.strip():
                raise ValueError("Prompt must not be empty")
            await self.initialize()
            if self.followups in self.agent.plugins:
                self.agent.plugins.remove(self.followups)
            self.agent.plugins.append(self.followups)  # Boundary signal follows every normal host/plugin hook.
            # Client capabilities and workspace selections may change between runs.
            # Preserve callers' authored prompt overrides.
            if self.agent.system_prompt == self._generated_system_prompt:
                self.refresh_instructions()
            if not self._is_subagent:
                async with aiosqlite.connect(self.agent.session.db_path) as db:
                    await db.execute(
                        "UPDATE harness_sessions SET title = ? WHERE id = ? AND title = ''",
                        (" ".join(prompt_text.split())[:80], self.session_id),
                    )
                    await db.commit()
            if self._closed:
                raise RuntimeError("Harness was closed before the run started")
            queue: asyncio.Queue[HarnessEvent | RunTurn] = asyncio.Queue(maxsize=64)
            self._queue = queue
            session_id = self.session_id
            run_id = host_run_id(self)

            async def produce() -> None:
                self.tasks.begin(
                    session_id,
                    reset_budget=not self._is_subagent and not task_id and not start_profile and trigger == "human",
                )
                self.followups.begin(session_id)
                message: RunInput | None = initial_input or self.followups.first(prompt)
                final: DoneEvent | None = None
                try:
                    if (
                        self._delivery_definition is not None
                        and not self._is_subagent
                        and _is_owned_adapter(self.agent.session)
                    ):
                        self.agent._execution_bridge = ExecutionBridge(self, self.agent.session, run_id)
                    if start_profile:
                        await self.tasks.start_task(prompt_text, start_profile, message=initial_input)
                        message = await self.followups.next(self.tasks, wait_for_tasks=True)
                    elif task_id:
                        self.tasks.continue_task(task_id, prompt_text, trigger=trigger, message=initial_input)
                        message = await self.followups.next(self.tasks, wait_for_tasks=True)
                    elif notifications:
                        self.tasks._ready.extend(notifications)
                        message = await self.followups.next(self.tasks)
                    elif trigger == "wakeup":
                        message = RunInput(await self.tasks.wakeup_notification(prompt_text), kind="wakeup")
                    while message is not None:
                        failed = False
                        await queue.put(self.followups.start_turn(message))
                        try:
                            async with (
                                message.context(),
                                aclosing(
                                    self.agent.run(
                                        message.prompt,
                                        session_id=session_id,
                                        user_id="harness",
                                        config=self.generation_config(),
                                    )
                                ) as events,
                            ):
                                async for event in events:
                                    if isinstance(event, DoneEvent):
                                        if event.session_id == session_id:
                                            final = event
                                    else:
                                        if isinstance(event, ErrorEvent) and not event.recoverable:
                                            failed = True
                                        await self.tasks.flush_observed()
                                        await self.emit(event)
                        except FollowupReady as boundary:
                            if boundary.owner is not self.followups:
                                raise
                        if failed:
                            # Preserve completed-task lifecycle evidence without
                            # constructing notifications or making another model call.
                            await self.tasks.flush_observed()
                            break
                        # A new Agent.run preserves all configured hooks and repairs
                        # incomplete persisted calls before adding the user notification.
                        message = await self.followups.next(self.tasks)
                    await self.tasks.end()
                    if final is not None:
                        await self.emit(final)
                finally:
                    self.followups.end()
                    self.agent._execution_bridge = None
                    await self.tasks.end()

            worker = asyncio.create_task(produce(), name=f"ngn-run-{self.session_id}")
            self._worker = worker
            pending: asyncio.Task[HarnessEvent | RunTurn] | None = None
            observed = False
            try:
                if self.config.demo:
                    yield Notice(
                        "OFFLINE DEMO: no network, workspace writes, shell, or Python plugins. Sessions are stored locally."
                    )
                while True:
                    if worker.done() and queue.empty():
                        observed = True
                        await worker
                        break
                    pending = asyncio.create_task(queue.get())
                    await asyncio.wait((pending, worker), return_when=asyncio.FIRST_COMPLETED)
                    if pending.done():
                        event = pending.result()
                        self.tasks.drain_activity()
                        if isinstance(event, RunTurn):
                            self.followups.notify(event)
                        else:
                            yield event
                        pending = None
                    else:
                        pending.cancel()
                        with suppress(asyncio.CancelledError):
                            await pending
                        pending = None
            finally:
                if pending is not None:
                    pending.cancel()
                    with suppress(asyncio.CancelledError):
                        await pending
                worker.cancel()
                try:
                    if not observed:
                        with suppress(asyncio.CancelledError):
                            await _await_cleanup(worker)
                finally:
                    self._worker = None
                    self._queue = None

    async def history(self) -> list["Message"]:
        await self.initialize(create_session=False)
        return await self.agent.session.get_history(self.session_id)

    async def context_stats(self, session_id: str = "") -> "ContextStats":
        """Estimate the request context for a session without running the model.

        Read-only and safe to call while idle; it never changes the selected
        session, prompt content, or model behavior.
        """
        await self.initialize()
        return await self.agent.context_stats(session_id or self.session_id)

    async def list_sessions(self) -> list[SessionInfo]:
        await self.initialize(create_session=False)
        async with aiosqlite.connect(self.agent.session.db_path) as db:
            cursor = await db.execute(
                "SELECT h.id, h.title, s.updated_at, COALESCE(f.forked_from, '') "
                "FROM harness_sessions h JOIN v2_sessions s ON s.id = h.id "
                "LEFT JOIN ngn_session_forks f ON f.session_id = h.id "
                "ORDER BY h.title = '', s.updated_at DESC, s.rowid DESC"
            )
            rows = await cursor.fetchall()
        return [SessionInfo(str(row[0]), str(row[1]) or "New session", str(row[2]), str(row[3])) for row in rows]

    async def fork_session(self, title: str = "") -> str:
        """Select an independent context copy; runtime work and approvals stay with the source."""
        with self.operation("fork session"):
            await self.initialize()
            if self.wakeup_handler is not None:
                raise forks.SessionForkError("Use the host's fork action when scheduled work has a lifecycle owner.")
            if any(not task.done() for task in self.tasks.root._workers.values()):
                raise forks.SessionForkError("Finish descendant work before forking this conversation.")
            source = self.session_id

            async def change() -> str:
                target = await forks.transaction(
                    self.agent.session.db_path, lambda db: forks.fork_in(db, source, title)
                )
                self.session_id = target
                self._session_created = True
                self.tools.read_hashes.clear()
                return target

            return await _await_cleanup(asyncio.create_task(change()))

    async def rename_session(self, title: str) -> None:
        """Set a nonempty title, which later automatic title generation preserves."""
        with self.operation("rename session"):
            await self.initialize()

            def change(db: "sqlite3.Connection") -> str:
                forks.rename_in(db, self.session_id, title)
                return self.session_id

            await forks.transaction(self.agent.session.db_path, change)

    async def resume(self, id: str) -> None:
        with self.operation("resume"):
            await self.initialize(create_session=False)
            if id not in {session.id for session in await self.list_sessions()}:
                raise ValueError(f"Session {id!r} does not exist in this workspace")
            self.session_id = id
            self._session_created = True
            self.tools.read_hashes.clear()

    async def new_session(self) -> str:
        with self.operation("new session"):
            await self.initialize(create_session=False)
            session_id = f"ngn-{uuid.uuid4().hex[:16]}"
            await self.create_session(session_id)
            self.session_id = session_id
            self._session_created = True
            self.tools.read_hashes.clear()
            return session_id

    async def compact(self) -> "CompactionDoneEvent":
        with self.operation("compaction"):
            await self.initialize()
            return await self.agent.compact(self.session_id)

    async def set_agent(self, name: str) -> None:
        with self.operation("set agent"):
            self.refresh_agent_definitions()
            profile = self.config.profile(name)
            if not profile.user_invocable:
                raise ValueError(f"Agent {name!r} is available only for delegation")
            binding = self._resolve_agent_model(name, profile).binding
            candidate = replace(self.config, agent=name, provider=binding.provider, model=binding.id)
            await self.reconfigure_provider(candidate)
            self.config.agent = name
            self._current_profile_name = name
            self._current_profile = replace(profile)
            self.config.model_explicit = bool(profile.model) or self._selected_model_explicit
            self.refresh_instructions()

    async def set_model(self, model: str) -> None:
        with self.operation("set model"):
            if not model.strip():
                raise ValueError("Model must not be empty")
            self.config.model = model
            self.config.model_explicit = True
            self._selected_model = model
            self._selected_model_explicit = True
            self.agent.provider.model = model

    async def reconfigure_provider(self, config: "HarnessConfig") -> None:
        """Adopt a validated provider selection, e.g. a saved web-settings override.

        Only the allowlisted routing fields may differ from the running config;
        administrator-owned fields (demo mode, storage, credential ceiling) are
        rejected. Swapping closes the previous provider client exactly once;
        reusing the same routing only updates the model.
        """
        config.validate()
        if config.demo is not self.config.demo:
            raise ValueError("Provider overrides cannot change demo mode")
        unavailable = config.unavailable_model_binding if not config.provider else _INHERIT_MODEL
        if unavailable.configured and unavailable.id != config.model:
            raise ValueError("The unavailable model reference must match the requested model")
        profile = config.provider_profile()
        if config.provider:
            selected = self.provider_store.load().providers.get(config.provider)
            if selected is None:
                raise ValueError("Named provider connection was removed; reload settings")
            profile = selected
        # Saving a connection publishes its registry before selecting it. The
        # mutable config can therefore already describe the new connection while
        # the running client still uses the old auth/endpoint. Compare against
        # the immutable profile that actually constructed that client.
        changing = (
            unavailable.configured
            or isinstance(self.agent.provider, UnavailableModelProvider)
            or ((config.provider, profile) != (self._built_provider_name, self._built_provider_profile))
        )
        if changing:
            replacement: Provider
            if unavailable.configured:
                replacement = UnavailableModelProvider(unavailable)
            elif config.provider:
                replacement = build_provider(profile, config, self.openai_auth)
            elif config.provider_profile().auth == "chatgpt":
                if not (isinstance(self.agent.provider, OpenAIProvider) and self.agent.provider.uses_chatgpt_auth):
                    self._api_model = self.agent.provider.model
                replacement = OpenAIProvider(self.openai_auth.credentials, model=config.model)
            else:
                replacement = HarnessProvider(config, self.login_store)
            try:
                await self.agent.close()
            finally:
                self.agent.provider = replacement
            self._built_provider_name = config.provider
            self._built_provider_profile = profile
        self.config.provider = config.provider
        self.config.providers = dict(config.providers)
        self.config.model = config.model
        self.config.unavailable_model_binding = unavailable
        self.config.model_explicit = True
        if self.agent.provider.model != config.model:
            self.agent.provider.model = config.model

    async def _select_provider(self, name: str, *, model: str = "") -> None:
        registry = self.provider_store.load()
        profile = registry.providers.get(name)
        if profile is None:
            raise ValueError(f"Unknown provider connection {name!r}")
        candidate = replace(
            self.config,
            provider=name,
            providers=dict(registry.providers),
            model=model or self.config.model,
        )
        await self.reconfigure_provider(candidate)
        self.providers = registry

    def _model_connection_fixed(self, *, include_agent: bool = True) -> bool:
        """A complete model reference is independent of registry activation."""
        if include_agent:
            preference = self.provider_store.agent_models.load(self.config.agent)
            if preference.revision != "0" * 64:
                if preference.binding.configured:
                    return bool(preference.binding.provider)
            elif self.current_profile().provider:
                return True
        if self.config.model_config_explicit:
            return bool(self._default_provider_name)
        for scope in ("workspace", "global"):
            binding = self.provider_store.model_store(scope).load_binding()
            if binding.configured:
                return bool(binding.provider)
        return False

    async def activate_provider(self, name: str, revision: str, scope: str = "workspace") -> ProviderRegistry:
        with self.operation("select provider"):
            current = self.provider_store.load_scope(scope)
            if current.revision != revision or name not in self.provider_store.load().providers:
                raise ValueError("Provider configuration changed; reload before selecting")
            if scope == "global" and name not in current.providers:
                raise ValueError("Select global connections in global settings")
            effective_scope = scope == "workspace" or not self.provider_store.load_scope("workspace").active
            if effective_scope and not self._model_connection_fixed():
                selected_model = self._resolve_agent_model(self.config.agent, self.current_profile()).binding.id
                await self._select_provider(name, model=selected_model)
            saved = self.provider_store.save_scope(
                ProviderRegistry(active=name, providers=current.providers), expected=revision, scope=scope
            )
            self.providers = self.provider_store.load()
            if effective_scope and not self._model_connection_fixed(include_agent=False):
                self._default_provider_name = name
            return saved

    async def inherit_provider(self, revision: str) -> ProviderRegistry:
        """Drop the workspace selection and follow the global active connection."""
        with self.operation("inherit provider"):
            current = self.provider_store.load_scope("workspace")
            if revision != current.revision:
                raise ValueError("Provider configuration changed; reload before selecting")
            global_active = self.provider_store.load_scope("global").active
            if not global_active:
                raise ValueError("No global provider default is configured")
            if not self._model_connection_fixed():
                selected_model = self._resolve_agent_model(self.config.agent, self.current_profile()).binding.id
                await self._select_provider(global_active, model=selected_model)
            saved = self.provider_store.save_scope(
                ProviderRegistry(providers=current.providers), expected=revision, scope="workspace"
            )
            self.providers = self.provider_store.load()
            if not self._model_connection_fixed(include_agent=False):
                self._default_provider_name = global_active
            return saved

    async def save_provider(
        self, name: str, profile: ProviderProfile, revision: str, scope: str = "workspace"
    ) -> ProviderRegistry:
        from .providers import NAME

        with self.operation("save provider"):
            current = self.provider_store.load_scope(scope)
            if current.revision != revision:
                raise ValueError("Provider configuration changed; reload before saving")
            if not NAME.fullmatch(name):
                raise ValueError("Invalid provider connection name")
            other_scope = "global" if scope == "workspace" else "workspace"
            if name not in current.providers and name in self.provider_store.load_scope(other_scope).providers:
                raise ValueError("Connection name already exists in the other scope")
            profile.validate()
            entries = {**current.providers, name: profile}
            active = current.active or (name if not self.provider_store.load().active else "")
            saved = self.provider_store.save_scope(
                ProviderRegistry(active=active, providers=entries), expected=revision, scope=scope
            )
            self.providers = self.provider_store.load()
            if self.providers.active == name and not self._model_connection_fixed(include_agent=False):
                self._default_provider_name = name
            selected = self._resolve_agent_model(
                self.config.agent, self.current_profile(), require_available=False
            ).binding
            if selected.provider == name and self.providers.providers[name] == profile:
                await self._select_provider(name, model=selected.id)
            return saved

    async def delete_provider(self, name: str, revision: str, scope: str = "workspace") -> ProviderRegistry:
        with self.operation("delete provider"):
            current = self.provider_store.load_scope(scope)
            if current.revision != revision or name not in current.providers:
                raise ValueError("Provider configuration changed; reload before deleting")
            self.refresh_agent_definitions()
            model_connections = {self.provider_store.model_binding(layer).provider for layer in ("global", "workspace")}
            agent_models = self.provider_store.agent_models
            agent_names = set(self.config.profile_names)
            if agent_models.directory.exists():
                # Retired definitions can still have a saved model override.
                # Keep that reference intact if the profile is later restored.
                for index, path in enumerate(agent_models.directory.iterdir()):
                    if index >= 1024:
                        raise ValueError("Too many agent model preferences to safely delete a provider")
                    if path.suffix == ".md":
                        agent_names.add(path.stem)
            model_connections.update(agent_models.load(agent).binding.provider for agent in agent_names)
            if (
                name == current.active
                or name == self.provider_store.load().active
                or any(profile.provider == name for profile in self.config.profiles.values())
                or name in model_connections
            ):
                raise ValueError(
                    "Select another connection and remove default or agent model bindings before deleting this provider"
                )
            entries = {key: profile for key, profile in current.providers.items() if key != name}
            saved = self.provider_store.save_scope(
                ProviderRegistry(active=current.active, providers=entries), expected=revision, scope=scope
            )
            self.providers = self.provider_store.load()
            return saved

    async def provider_models(self, name: str, *, scope: str = "workspace") -> list[str]:
        return (await self.provider_catalog(name, scope=scope)).models

    async def provider_catalog(self, name: str, *, scope: str = "workspace") -> ModelCatalog:
        """Discover a saved connection without selecting it or fetching metadata twice."""
        if scope not in {"global", "workspace"}:
            raise ValueError("Provider scope must be global or workspace")
        registry = self.provider_store.load_scope("global") if scope == "global" else self.provider_store.load()
        profile = registry.providers.get(name)
        if profile is None:
            raise ValueError(f"Unknown provider connection {name!r}")
        candidate = replace(
            self.config,
            provider=name,
            providers=dict(registry.providers),
        )
        provider = build_provider(profile, candidate, self.openai_auth)
        try:
            if isinstance(provider, HarnessProvider):
                provider.credentials()
            models = await provider.get_model_list()
            snapshot = provider.context_metadata_snapshot()
            metadata: dict[str, ModelCapabilities] = {}
            for model in models:
                capabilities: ModelCapabilities = {}
                configured = profile.model_context_windows.get(model, profile.context_window)
                if configured:
                    capabilities["context_window"] = configured
                    capabilities["context_window_source"] = "configured"
                elif limit := snapshot.limits.get(model, 0):
                    capabilities["context_window"] = limit
                    capabilities["context_window_source"] = "provider"
                if model in snapshot.reasoning_efforts:
                    capabilities["reasoning_efforts"] = list(snapshot.reasoning_efforts[model])
                if capabilities:
                    metadata[model] = capabilities
            if name == self._built_provider_name and profile == self._built_provider_profile:
                self.agent.provider.restore_context_metadata(snapshot)
            return ModelCatalog(models, metadata)
        finally:
            await provider.close()

    async def _use_chatgpt(self) -> None:
        if not (isinstance(self.agent.provider, OpenAIProvider) and self.agent.provider.uses_chatgpt_auth):
            self._api_model = self.agent.provider.model
        profile = self.provider_store.load().providers.get(self.config.provider) if self.config.provider else None
        replacement = OpenAIProvider(
            self.openai_auth.credentials, model=self.config.model, timeout=profile.request_timeout if profile else 120.0
        )
        if profile is not None:
            replacement.configure_context_window(profile.context_window, profile.model_context_windows)
        try:
            await self.agent.close()
        finally:
            self.agent.provider = replacement
        self._built_provider_name = self.config.provider
        self._built_provider_profile = replace(self.config.provider_profile(), auth="chatgpt")

    async def login(self, show_code: Callable[["DeviceAuthorization"], Awaitable[None]]) -> None:
        """User-initiated device login. Codes and tokens never enter agent history."""
        with self.operation("login"):
            if self.config.demo:
                raise ValueError("Login is disabled in offline demo. Restart ngn without --demo, then use /login.")
            if (
                self.config.provider_profile().kind not in {"openai", "openai_compatible"}
                or self.config.provider_profile().base_url
                or self.config.provider_profile().api != "auto"
            ):
                raise ValueError(
                    "Device login is for the default OpenAI provider, without a custom base_url or api override."
                )
            await self.initialize()
            authorization = await self.openai_auth.start_device_login()
            await show_code(authorization)
            await self.openai_auth.complete_device_login(authorization)
            self.config.providers[self.config.provider] = replace(self.config.provider_profile(), auth="chatgpt")
            await self._use_chatgpt()
            # Remember the selection so later runs use ChatGPT without flags.
            self.login_store.save(ProviderLogin(provider="openai", model=self.config.model, auth="chatgpt"))

    async def login_api(self, login: ProviderLogin) -> None:
        """Adopt and persist a provider API login; the key never enters history.

        The candidate selection is fully validated before anything is saved.
        A stored key counts as available credentials even when the environment
        variable is unset, and the previous selection is restored if the
        provider swap fails.
        """
        with self.operation("login"):
            if self.config.demo:
                raise ValueError("Login is disabled in offline demo. Restart ngn without --demo, then use ngn login.")
            candidate = replace(
                self.config,
                provider="",
                providers={
                    **self.config.providers,
                    "": ProviderProfile(
                        kind=login.provider,
                        base_url=login.base_url,
                        api=login.api,
                        auth="api-key",
                        api_key_env=login.api_key_env or self.config.provider_profile().key_env,
                    ),
                },
                model=self.config.model,
            )
            if not login.api_key and not login.api_key_env:
                raise ValueError(
                    "No API key or key environment variable was provided; provide one or reference an environment "
                    "variable with --api-key-env."
                )
            await self.initialize()
            stored = replace(
                login,
                model=candidate.model,
                base_url=candidate.provider_profile().base_url,
                api=candidate.provider_profile().api,
                api_key_env=candidate.provider_profile().key_env,
            )
            previous = self.login_store.selection()
            self.login_store.save(stored)
            try:
                await self.reconfigure_provider(candidate)
            except BaseException:
                if previous is None:
                    self.login_store.remove()
                else:
                    self.login_store.save(previous)
                raise
            self.diagnostics.append(
                f"Signed in to provider {candidate.provider_profile().kind} using saved credentials"
            )

    async def logout(self) -> None:
        """Remove only ngn's local logins, not other clients' credentials."""
        with self.operation("logout"):
            if self.config.demo:
                raise ValueError("Logout is disabled in offline demo; your saved credentials were not touched.")
            self.openai_auth.logout()
            self.login_store.remove()
            if self.config.provider:
                profile = self.provider_store.load().providers[self.config.provider]
                self.config.providers[self.config.provider] = profile
                if isinstance(self.agent.provider, OpenAIProvider) and self.agent.provider.uses_chatgpt_auth:
                    replacement = build_provider(profile, self.config, self.openai_auth)
                    try:
                        await self.agent.close()
                    finally:
                        self.agent.provider = replacement
                    self._built_provider_name = self.config.provider
                    self._built_provider_profile = profile
                return
            self.config.providers[self.config.provider] = replace(self.config.provider_profile(), auth="api-key")
            if isinstance(self.agent.provider, OpenAIProvider) and self.agent.provider.uses_chatgpt_auth:
                self.config.model = self._api_model
                replacement = HarnessProvider(self.config, self.login_store)
                try:
                    await self.agent.close()
                finally:
                    self.agent.provider = replacement
                self._built_provider_name = self.config.provider
                self._built_provider_profile = self.config.provider_profile()

    def auth_status(self) -> str:
        if isinstance(self.agent.provider, UnavailableModelProvider):
            return self.agent.provider.reason
        if self.config.demo:
            return "Offline demo; authentication is disabled"
        if self.config.provider:
            profile = self.provider_store.load().providers[self.config.provider]
            if profile.auth == "entra":
                return "Microsoft Entra ID via DefaultAzureCredential (token resolved at request time)"
            if profile.auth == "codex" or (
                profile.auth == "auto"
                and isinstance(self.agent.provider, OpenAIProvider)
                and self.agent.provider.uses_chatgpt_auth
                and not self.openai_auth.logged_in()
            ):
                return "Local Codex authentication (resolved from CODEX_HOME or ~/.codex)"
            if profile.auth == "api-key":
                return f"API key from ${profile.key_env} (value never displayed)"
        if (
            isinstance(self.agent.provider, OpenAIProvider) and self.agent.provider.uses_chatgpt_auth
        ) or self.config.provider_profile().auth == "chatgpt":
            return self.openai_auth.status()
        if self.login_store.key_for(self.config.provider_profile().kind):
            return f"{self.config.provider_profile().kind} API key saved in ngn's private credential store (value never displayed)"
        return f"API key from ${self.config.provider_profile().key_env} (value never displayed)"

    def login_status(self) -> str:
        if self.config.demo:
            return "Offline demo; authentication is disabled"
        return f"ChatGPT: {self.openai_auth.status()}\nProvider login: {self.login_store.status()}"

    def config_header(self) -> str:
        """Compact run header: the effective provider, model and loaded config files."""
        loaded = ", ".join(str(path) for path in self.config.config_paths) or "built-in defaults"
        model = self.agent.provider.model or self.config.model
        lines = [
            f"Provider: {self.config.provider_profile().kind}",
            f"Model: {model}",
            f"Config: {loaded}",
        ]
        if self.config.demo:
            lines.append("Mode: offline demo (no provider calls)")
        return "\n".join(lines)

    def describe(self) -> str:
        skills = ", ".join(f"{skill.name}: {skill.description}" for skill in self.agent.skills.values()) or "none"
        return "\n".join(
            [
                "OFFLINE DEMO" if self.config.demo else "Live coding harness (network only on a live request)",
                f"Workspace: {self.workspace}",
                f"Session: {self.session_id}",
                f"Provider/model: {self.config.provider_profile().kind} / {self.agent.provider.model}",
                f"HTTP API: {self.config.provider_profile().api}",
                f"Endpoint: {self.agent.provider.base_url}",
                f"Authentication: {self.auth_status()}",
                f"Theme: {self.config.theme}; background: {self.config.theme_background}; animations: {self.config.animations}",
                f"Composer: submit_mode={self.config.submit_mode}; tab_action={self.config.tab_action}",
                f"Agent: {self.config.agent} ({self.mode}); profiles: {', '.join(self.config.profile_names)}",
                f"Subagents: max depth {self.config.max_subagent_depth} (root=0); shared 3 concurrent / 8 executions per run",
                f"Project configuration trusted: {self.config.trust_project}",
                f"Config files: {', '.join(str(path) for path in self.config.config_paths) or 'built-in defaults'}",
                *self.resources.mcp_diagnostics().describe(),
                f"Registered tools: {', '.join(self.agent.tool_registry.names())}",
                f"Commands: {', '.join('/' + command.name for command in self.commands.list())}",
                f"Plugins configured: {', '.join(self.config.plugins) or 'none'}",
                f"Plugins loaded: {', '.join(self.loaded_plugins) or 'none'}",
                f"Skills: {skills}",
                f"Skill loading: {self.config.skill_token_limit} approximate tokens; live discovery at message/tool boundaries",
                *self.tools.skill_diagnostics,
                f"Instructions: {', '.join(self.instructions) or 'none'}",
                f"Session database: {self.agent.session.db_path}",
                "Policy: read-only reviewer; build edits, shell, and custom tools pass through the host approval policy "
                "(one-call decision or a saved matching tool allowance); "
                "host-managed interactive channel_send does not; shell is NOT SANDBOXED.",
                "File tools: bounded UTF-8, no symlinks/credentials/.git; create parents with an explicitly approved shell command.",
                *self.diagnostics,
            ]
        )

    async def close(self) -> None:
        if self._closing is None:
            if self._busy and self._busy != "run":
                raise RuntimeError(f"Harness is busy ({self._busy})")
            self._closed = True

            async def stop() -> None:
                try:
                    if self._worker is not None:
                        self._worker.cancel()
                        with suppress(asyncio.CancelledError):
                            await _await_cleanup(self._worker)
                finally:
                    try:
                        await self.tasks.end()
                    finally:
                        try:
                            await self.agent.close()
                        finally:
                            try:
                                await self.resources.aclose()
                            finally:
                                if self._owns_auth:
                                    await self.openai_auth.close()

            self._closing = asyncio.create_task(stop(), name=f"ngn-close-{self.session_id}")
        await _await_cleanup(self._closing)
