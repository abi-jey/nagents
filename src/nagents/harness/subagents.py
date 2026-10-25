"""Process-local task trees with root-owned quotas and boundary-only notifications.

Delegation is acknowledged once, through the native tool protocol. Notifications
are untrusted user messages, never additional tool results. Closed children retain
their session identity for bounded follow-ups and notifications, not durable job replay.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import sqlite3
import time
import uuid
from collections import deque
from contextlib import aclosing
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from typing import TYPE_CHECKING
from typing import Literal

from nagents._async import join_owned as _await_cleanup
from nagents.error_presentation import remote_error_message
from nagents.error_presentation import safe_error_code
from nagents.error_presentation import subagent_error_message
from nagents.events import DoneEvent
from nagents.events import ErrorEvent
from nagents.events import TextDoneEvent
from nagents.provider._diagnostics import transport_diagnostic
from nagents.provider.openai import OpenAIProvider
from nagents.provider.reasoning import validate_reasoning_choice

from .agent_status import AgentStatusResult
from .agent_status import SessionEvidence
from .agent_status import make_status
from .agent_updates import format_batch
from .agent_updates import format_wakeup
from .execution import ExecutionBridge
from .followups import RunInput
from .followups import RunTurn
from .provider import HarnessProvider
from .reasoning import ReasoningPreference
from .reasoning import resolve_effort
from .reasoning import validate_effort
from .session_identity import SessionIdentityStore
from .session_identity import session_sequence
from .session_identity import validate_title
from .subagent_settings import LocalSubagentSettings
from .subagent_settings import SubagentLimits
from .subagent_settings import SubagentSettingsHost
from .subagent_settings import limit_scope
from .task_activity import ChildActivity
from .task_activity import TaskOptions
from .task_activity import compaction_snapshot
from .task_activity import public_payload
from .tools import READ_ONLY_TOOLS as _READ_ONLY_TOOLS
from .types import Notice
from .types import TaskActivity
from .types import TaskCompleted
from .types import TaskDeliveryWarning
from .types import TaskMessage
from .types import TaskNotification
from .types import TaskStarted

if TYPE_CHECKING:
    import builtins

    from nagents.types import Message
    from nagents.types import ReasoningEffort
    from nagents.types import ToolArguments

    from .config import AgentProfile
    from .model_bindings import ModelBinding
    from .providers import ProviderProfile
    from .runtime import Harness

MAX_RETAINED_TASKS = 64
MAX_FOLLOWUPS = 8
MAX_PROMPT = 16_000
MAX_RESULT = 12_000
DEFAULT_TASK_OPTIONS = TaskOptions()
logger = logging.getLogger(__name__)


@dataclass
class TaskInfo:
    session_id: str
    title: str
    status: Literal["running", "completed", "failed", "cancelled"] = "running"
    result: str = ""
    error: str = ""
    root_session_id: str = ""
    prompt: str = ""
    _storage_id: str = ""
    parent_session_id: str = ""
    _parent_storage_id: str = ""
    depth: int = 1
    profile: str = "assistant"
    mode: str = "build"
    followups: int = 0
    activation: int = 0
    trigger: str = "delegation"
    provider: str = ""
    provider_kind: str = ""
    model: str = ""
    phase: str = "starting"
    started_at: float = 0.0
    updated_at: float = 0.0
    completed_at: float = 0.0
    activity_sequence: int = 0
    context: dict[str, object] = field(default_factory=dict)
    compaction: dict[str, object] = field(default_factory=dict)
    next_settings: dict[str, object] = field(default_factory=dict)
    origin_history_id: str = ""
    origin_run_id: str = ""
    reasoning_effort: ReasoningEffort = ""
    activation_history_id: str = ""
    activation_run_id: str = ""
    activation_call_id: str = ""
    activation_call_position: int = -1
    parent_response_id: str = ""
    parent_response_session_id: str = ""

    @property
    def label(self) -> str:
        return self.title or self.session_id


@dataclass(frozen=True)
class _ContinuationSettings:
    options: TaskOptions
    binding: ModelBinding
    provider: ProviderProfile
    apply: bool
    revision: int


@dataclass(frozen=True)
class _ActivationOrigin:
    history_id: str = ""
    run_id: str = ""
    call_id: str = ""
    call_position: int = -1

    def apply(self, info: TaskInfo) -> None:
        info.activation_history_id = self.history_id
        info.activation_run_id = self.run_id
        info.activation_call_id = self.call_id
        info.activation_call_position = self.call_position


@dataclass(eq=False, frozen=True)
class _StartAdmission:
    owner: SubagentManager
    stops: tuple[asyncio.Event, ...]
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    workers: list[asyncio.Task[None]] = field(default_factory=list)


class SubagentManager:
    """A local inbox per harness, with one root-owned task registry and budget.

    Quotas reserve synchronously and fail fast; waiting parents still occupy a
    slot, so descendants must never wait on a semaphore. Only external root runs
    reset the execution budget. Follow-ups cost one execution too.
    """

    def __init__(self, harness: Harness, root: SubagentManager | None = None) -> None:
        self.harness = harness
        self.root = root or self
        self.settings_host: SubagentSettingsHost = LocalSubagentSettings(harness.config)
        self.limits = SubagentLimits.from_config(harness.config)
        self._shared_limits = self.limits
        self._limits_error = ""
        self._infos: dict[str, TaskInfo] = {} if root is None else root._infos
        self._workers: dict[str, asyncio.Task[None]] = {} if root is None else root._workers
        self._children: dict[str, Harness] = {} if root is None else root._children
        self._profile_snapshots: dict[str, tuple[dict[str, AgentProfile], dict[str, AgentProfile]]] = (
            {} if root is None else root._profile_snapshots
        )
        self._model_snapshots: dict[str, ModelBinding] = {} if root is None else root._model_snapshots
        self._options: dict[str, TaskOptions] = {} if root is None else root._options
        self._next_options: dict[str, TaskOptions] = {} if root is None else root._next_options
        self._next_revisions: dict[str, int] = {} if root is None else root._next_revisions
        self._provider_overrides: dict[str, ProviderProfile] = {} if root is None else root._provider_overrides
        self._next_providers: dict[str, ProviderProfile] = {} if root is None else root._next_providers
        self._inputs: dict[str, deque[RunInput]] = {} if root is None else root._inputs
        self._admitted: dict[str, dict[str, RunInput]] = {} if root is None else root._admitted
        self._initial_inputs: dict[str, RunInput] = {} if root is None else root._initial_inputs
        self._evidence: dict[str, SessionEvidence] = {} if root is None else root._evidence
        self._activity_gaps: set[str] = set() if root is None else root._activity_gaps
        self._ready: deque[TaskCompleted | TaskMessage] = deque()
        self._observed: deque[TaskCompleted | TaskMessage | ErrorEvent] = deque()
        self._pending: dict[str, list[TaskCompleted | TaskMessage]] = {}
        self._changed = asyncio.Event()
        self.notification_sources: tuple[str, ...] = ()
        self._session_id = ""
        self._active = False
        self._used = 0
        self._starting = 0
        self._new_starting = 0
        self._activating: set[str] = set()
        self._start_admissions: set[_StartAdmission] = set() if root is None else root._start_admissions
        self._stopped = asyncio.Event()
        self._stopped.set()
        self._identities: SessionIdentityStore = (
            SessionIdentityStore(harness.agent.session.db_path) if root is None else root._identities
        )
        self._shutdown: asyncio.Task[None] | None = None

    def register(self) -> None:
        # Register once even at depth zero. Advertisement and execution both
        # check current limits, so changing settings never replaces this binding.
        self.harness.tools.builtins["to_agent"] = self.to_agent
        self.harness.agent.register_tool(self.to_agent)
        for function in (self.agent_status, self.subagent_configuration, self.subagent_configure):
            self.harness.tools.builtins[function.__name__] = function
            self.harness.agent.register_tool(function)

    async def refresh_limits(self) -> None:
        """Accept one limits snapshot; never touch execution counts or workers."""
        try:
            snapshot = await self.root.settings_host.read("workspace")
        except (OSError, ValueError, sqlite3.Error) as error:
            name = type(error).__name__
            if name != self._limits_error:
                self._limits_error = name
                self.harness.resources.logger.log(
                    logging.WARNING,
                    "Subagent settings refresh failed: task=%s error_type=%s; previous limits retained",
                    self.harness._agent_session_id or "root",
                    name,
                )
                await self.harness.emit(
                    Notice("Subagent settings could not be refreshed; previous limits are kept.", "warning")
                )
            return
        self._limits_error = ""
        self.accept_limits(snapshot.effective)

    def accept_limits(self, limits: SubagentLimits) -> None:
        """Publish a validated turn (or idle host) snapshot without new capacity accounting."""
        previous = self.limits
        self.root._shared_limits = limits
        self.limits = limits
        for name, value in self.limits.values().items():
            setattr(self.harness.config, name, value)
        if previous != self.limits:
            self.harness.resources.logger.log(
                logging.INFO,
                "Subagent limits accepted: task=%s depth=%s limits=%s used_executions=%s",
                self.harness._agent_session_id or "root",
                self.harness.subagent_depth,
                self.limits.values(),
                self.root._used,
            )

    def admission_limits(self) -> SubagentLimits:
        # A newly lowered shared limit also constrains tools from an older
        # in-flight turn. Raising it becomes available at that turn's next boundary.
        return SubagentLimits(
            **{
                name: min(value, getattr(self.root._shared_limits, name))
                for name, value in self.limits.values().items()
            }
        )

    def usage(self) -> dict[str, object]:
        limits = self.admission_limits()
        occupied = (
            sum(
                info.status == "running"
                or (info.session_id in self._workers and not self._workers[info.session_id].done())
                for info in self._infos.values()
            )
            + self.root._starting
        )
        return {
            "limits": limits.values(),
            "used_executions": self.root._used,
            "remaining_executions": max(0, limits.max_subagent_executions - self.root._used),
            "occupied_slots": occupied,
            "available_slots": max(0, limits.max_concurrent_subagents - occupied),
            "retained_tasks": len(self._infos),
            "max_retained_tasks": MAX_RETAINED_TASKS,
            "depth": self.harness.subagent_depth,
            "reset": "Only a new external human root run resets used executions; settings reload does not.",
        }

    async def subagent_configuration(self, scope: str = "workspace") -> dict[str, object]:
        """Read scoped subagent limits and the shared tree's actual execution/slot usage.
        Read before proposing a change. Counts include nested work and follow-ups;
        waiting parents occupy slots. A new agent is not the same as an execution.
        """
        snapshot = await self.root.settings_host.read(limit_scope(scope))
        return {**snapshot.values(), "usage": self.usage()}

    async def subagent_configure(
        self,
        revision: str,
        max_concurrent_subagents: int,
        max_subagent_executions: int,
        max_subagent_depth: int,
        scope: str = "workspace",
    ) -> dict[str, object]:
        """Propose an exact scoped limit change for explicit one-time human approval.
        Read subagent_configuration first and use its revision. Every change needs
        approval; a general task request or saved tool allowance is insufficient.
        Approved settings apply at the next model turn without resetting accounting
        or cancelling work. Only a build-mode root can propose changes.
        """
        if self.harness._is_subagent or self.harness.mode != "build":
            raise PermissionError("Only the root build agent can propose shared subagent settings")
        selected = limit_scope(scope)
        limits = SubagentLimits(max_concurrent_subagents, max_subagent_executions, max_subagent_depth)
        before = await self.root.settings_host.read(selected)
        if before.revision != revision:
            raise ValueError("Subagent settings changed. Read the current settings before proposing again.")
        if limits == before.limits:
            return {**before.values(), "status": "unchanged", "usage": self.usage()}
        arguments: ToolArguments = {
            "scope": selected,
            "revision": revision,
            "current": before.limits.values(),
            "proposed": limits.values(),
            "apply_at": "next_model_turn",
        }
        preview = "\n".join(f"{name}: {old} → {getattr(limits, name)}" for name, old in before.limits.values().items())
        await self.harness.approve(
            "subagent_configure",
            arguments,
            f"Change {selected} subagent limits for the next model turn. Existing work and used executions are retained.",
            preview,
            self.harness.tools.call_id.get(),
            mandatory=True,
        )
        saved = await self.root.settings_host.save(selected, revision, limits)
        return {**saved.values(), "status": "saved", "usage": self.usage()}

    def list(self) -> list[TaskInfo]:
        """Detached snapshots of all descendants retained in this root session."""
        return [
            copy.deepcopy(info)
            for info in self._infos.values()
            if info.root_session_id == self.root.harness.session_id and self._belongs(info)
        ]

    def _belongs(self, info: TaskInfo) -> bool:
        if self is self.root:
            return True
        parent_id = info.parent_session_id
        seen: set[str] = set()
        while parent_id and parent_id not in seen:
            seen.add(parent_id)
            if parent_id == self.harness._agent_session_id:
                return True
            parent = self._infos.get(parent_id)
            parent_id = parent.parent_session_id if parent else ""
        return False

    async def agent_status(self, session_id: str = "", limit: int = 20, history_limit: int = 6) -> AgentStatusResult:
        """Read current child status without starting, waiting for, or modifying work.
        Omit session_id for a bounded list of your descendants; supply the short
        ID returned by to_agent for detail. Includes public latest text/tool
        output, reported token counts and effective model/provider. Reasoning
        and private provider data are excluded. Full transcripts remain available
        in the host's session inspector. Do not poll this tool when only waiting
        for normal completion updates.

        Args:
            session_id: Existing short child conversation ID, or empty for list mode.
            limit: Maximum list entries, from 1 to 64.
            history_limit: Public activity entries in detail mode, from 0 to 20.
        """
        return await self.status(session_id, limit=limit, history_limit=history_limit)

    async def status(self, session_id: str = "", *, limit: int = 20, history_limit: int = 6) -> AgentStatusResult:
        if not isinstance(session_id, str):
            raise ValueError("session_id must be a short child ID or an empty string")
        if type(limit) is not int or not 1 <= limit <= MAX_RETAINED_TASKS:
            raise ValueError("Status limit must be between 1 and 64")
        if type(history_limit) is not int or not 0 <= history_limit <= 20:
            raise ValueError("history_limit must be between 0 and 20")
        if session_id:
            info = self._lookup(session_id)
            return make_status(info, self._evidence.get(session_id, SessionEvidence()), history_limit)
        infos = self.list()
        return {
            "sessions": [
                make_status(info, self._evidence.get(info.session_id, SessionEvidence()), 0) for info in infos[:limit]
            ],
            "more": max(0, len(infos) - limit),
        }

    def _running(self) -> bool:
        return (
            self._has_start_admissions()
            or any(
                session_id in self.root._activating and self._belongs(info) for session_id, info in self._infos.items()
            )
            or any(
                not worker.done() and self._belongs(self._infos[task_id]) for task_id, worker in self._workers.items()
            )
        )

    def _owns_start_admission(self, admission: _StartAdmission) -> bool:
        if self is self.root or admission.owner is self:
            return True
        owner_id = admission.owner.harness._agent_session_id
        if owner_id == self.harness._agent_session_id:
            return True
        info = self._infos.get(owner_id)
        return info is not None and self._belongs(info)

    def _has_start_admissions(self) -> bool:
        return any(self._owns_start_admission(admission) for admission in self._start_admissions)

    def _start_admission(self) -> _StartAdmission:
        stops = [self._stopped, self.root._stopped]
        parent_id = self.harness._agent_session_id
        seen: set[str] = set()
        while parent_id and parent_id not in seen:
            seen.add(parent_id)
            info = self._infos.get(parent_id)
            if info is None:
                break
            parent = self._children.get(parent_id)
            if parent is not None and info.status == "running":
                stops.append(parent.tasks._stopped)
            parent_id = info.parent_session_id
        return _StartAdmission(self, tuple(dict.fromkeys(stops)))

    def _admission_changed(self) -> None:
        # A pending host start is visible to all relevant notification waiters,
        # including ancestors between model turns. Rejection must wake them too.
        self._changed.set()
        self.root._changed.set()
        for child in self._children.values():
            child.tasks._changed.set()

    @staticmethod
    def _check_start_admission(admission: _StartAdmission) -> None:
        if any(stop.is_set() for stop in admission.stops):
            raise RuntimeError("Agent start stopped before child execution")

    async def _emit_started(self, info: TaskInfo, prompt: str, admission: _StartAdmission) -> None:
        """Own the backpressured put without making shutdown join its producer."""
        self._check_start_admission(admission)
        self._check_active()
        # Install the lifecycle handle before a consumer can act on the
        # emitted start. The gate preserves start_task's receipt-binding
        # window: no child work runs before its caller returns.
        worker = asyncio.create_task(
            self._run_admitted_child(info, prompt, admission), name=f"ngn-subagent-{info.session_id}"
        )
        self._workers[info.session_id] = worker
        admission.workers.append(worker)
        emitted = asyncio.create_task(
            self.root.harness.emit(self._started(info, prompt)), name=f"ngn-agent-start-emit-{info.session_id}"
        )
        stopped = [
            asyncio.create_task(stop.wait(), name=f"ngn-agent-start-stop-{info.session_id}-{index}")
            for index, stop in enumerate(admission.stops)
        ]
        try:
            await asyncio.wait((emitted, *stopped), return_when=asyncio.FIRST_COMPLETED)
            self._check_start_admission(admission)
            await emitted
        finally:
            for task in (emitted, *stopped):
                if not task.done():
                    task.cancel()

            async def join() -> None:
                await asyncio.gather(emitted, *stopped, return_exceptions=True)

            await _await_cleanup(asyncio.create_task(join(), name=f"ngn-agent-start-join-{info.session_id}"))

    async def _run_admitted_child(self, info: TaskInfo, prompt: str, admission: _StartAdmission) -> None:
        await admission.ready.wait()
        await self._run_child(info, prompt)

    @staticmethod
    async def _cancel_start_workers(admission: _StartAdmission) -> None:
        for worker in admission.workers:
            if not worker.done():
                worker.cancel()

        async def join() -> None:
            await asyncio.gather(*admission.workers, return_exceptions=True)

        await _await_cleanup(asyncio.create_task(join(), name="ngn-agent-start-worker-cleanup"))

    def begin(self, session_id: str, *, reset_budget: bool = False) -> None:
        """Activate an inbox; only a new external root run requests a fresh budget."""
        if self._active:
            raise RuntimeError("A subagent task group is already active")
        if self._has_start_admissions() or (self is self.root and (self._starting or self._activating)):
            raise RuntimeError("Agent session admission is still pending; wait before starting another root run")
        if self._running():
            raise RuntimeError("A subagent task group is already active")
        if self is not self.root and not self.root._active:
            raise RuntimeError("The root task group is no longer active")
        if self is self.root and reset_budget:
            self._used = 0
        self._session_id = session_id
        self._ready.clear()
        self._observed.clear()
        self._pending.clear()
        self._changed.clear()
        self._shutdown = None
        self._stopped = asyncio.Event()
        self._active = True
        if self is not self.root:
            self.root._changed.set()

    def _check_active(self) -> None:
        if (
            not self._active
            or not self.root._active
            or self.harness._closed
            or self.root.harness._closed
            or self.harness.session_id != self._session_id
            or self.root.harness.session_id != self.root._session_id
        ):
            raise RuntimeError("Delegation requires an active parent run in the same session")

    def _check_budget(self, prompt: str, *, slot: bool = True, reserved: bool = False) -> None:
        self._check_active()
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT:
            raise ValueError(f"Delegation prompt must contain 1 to {MAX_PROMPT} characters")
        limits = self.admission_limits()
        if self.root._used - int(reserved) >= limits.max_subagent_executions:
            raise ValueError(
                f"Subagent budget exhausted: {self.root._used} of {limits.max_subagent_executions} executions used in this root run, including follow-ups. Read subagent_configuration before proposing an increase."
            )
        if slot and (
            self.root._starting
            - int(reserved)
            + sum(
                info.status == "running"
                or (info.session_id in self._workers and not self._workers[info.session_id].done())
                for info in self._infos.values()
            )
            >= limits.max_concurrent_subagents
        ):
            raise ValueError(
                f"At most {limits.max_concurrent_subagents} subagents may run concurrently, including waiting parents. Leave slots for descendants or work in waves."
            )

    async def to_agent(
        self,
        prompt: str,
        agent: str = "",
        session_id: str = "",
        title: str = "",
        model: str = "",
        provider: str = "",
        compact_trigger: str = "",
        compact_tokens: int = 200000,
        compact_messages: int = 100,
        reasoning_effort: ReasoningPreference = "",
    ) -> dict[str, str]:
        """Send work and immediately acknowledge the child's session_id and status.
        Omit session_id to start a child. Supply a previously returned short ID
        to continue that same idle conversation. Busy, cancelled, foreign,
        deleted and unavailable targets fail explicitly; nothing is replaced.
        This is acceptance, not completion: results arrive as later untrusted
        updates to the conversation's original parent. Use agent_status to
        inspect descendants without polling when you have no independent work.
        Continue useful independent work. When only waiting, end the current model
        turn without tool calls; the harness waits and resumes you with untrusted
        results. Then complete the task.
        Children inherit your permission ceiling; writes and shell pass through
        the host approval policy. The whole tree shares its configured slots and
        execution budget; read subagent_configuration for actual limits and usage.
        Full quotas fail immediately. Waiting parents occupy slots: leave capacity
        for their children or complete branches in waves. Only an explicitly
        approved subagent_configure proposal can raise saved limits.

        Args:
            prompt: A self-contained task; the child does not receive parent conversation history.
            agent: Omission selects assistant for a new session or preserves an existing target's profile.
            session_id: Short existing child ID, such as s1. Omission starts a new conversation.
            title: Optional one-line display title, at most 120 characters; blank preserves an existing title.
            model: Optional model identifier; omission inherits the agent, workspace, then global model selection.
            provider: Optional connection for this invocation; omission uses the resolved model's provider.
            compact_trigger: Optional auto, tokens, messages, or off; omission inherits parent compaction.
            compact_tokens: Context budget for tokens mode; compaction starts at seventy percent.
            compact_messages: Message threshold for messages mode.
            reasoning_effort: Optional model effort; omission inherits profile or parent, default clears to provider default.
        """
        if not isinstance(agent, str):
            raise ValueError("agent must name a profile or be an empty string")
        if not self.harness.can_delegate:
            raise PermissionError("Delegation is disabled at the configured subagent depth limit")
        title = validate_title(title)
        options = TaskOptions(provider, model, compact_trigger, compact_tokens, compact_messages, reasoning_effort)
        if session_id:
            session_sequence(session_id)
            info = self._lookup(session_id)
            if agent and agent != info.profile:
                raise ValueError("An existing session's agent profile cannot be changed")
            accepted = await self.continue_task(session_id, prompt, trigger="agent", options=options, title=title)
            return {"session_id": accepted.session_id, "status": accepted.status}
        if not isinstance(session_id, str):
            raise ValueError("session_id must be a short child ID or an empty string")
        agent = agent or "assistant"
        self._check_budget(prompt)
        profile = self.harness.delegation_profile(agent)
        options.validate(self.harness)
        if profile.a2a.configured and (compact_trigger or reasoning_effort):
            raise ValueError("Remote agents manage their own compaction and reasoning settings")
        binding = self.harness.delegation_model_binding(agent, model=model, provider=provider)
        if agent not in self.harness.delegatable_profile_names:
            raise PermissionError(f"Agent {agent!r} is not available for delegation from this profile")
        if len(self._infos) + self.root._new_starting >= MAX_RETAINED_TASKS:
            raise ValueError(
                f"Task retention limit reached ({MAX_RETAINED_TASKS} identities per root harness lifetime)"
            )
        accepted_root = self.root._session_id
        accepted_parent = self.harness.session_id
        activation_origin = self._activation_origin()
        profiles = {name: replace(value) for name, value in self.harness.delegation_profiles().items()}
        source_profiles = {name: replace(value) for name, value in self.harness.delegation_source_profiles().items()}
        accepted_providers = (
            {binding.provider: replace(self.harness.providers.providers[binding.provider])}
            if binding.provider and not profile.a2a.configured
            else {}
        )
        admission = self._start_admission()
        self._check_start_admission(admission)
        self._start_admissions.add(admission)
        reserved = False
        try:
            self._admission_changed()
            # Reserve before the first await. Metadata I/O must neither oversubscribe
            # quotas nor block the event loop carrying chat and audio traffic.
            self.root._used += 1
            reserved = True
            self.root._starting += 1
            self.root._new_starting += 1
            try:
                identity = await self._identities.reserve(accepted_root, self.harness._agent_session_id, title)
                self._check_budget(prompt, reserved=True)
                self._check_start_admission(admission)
                if not self.harness.can_delegate:
                    raise PermissionError("Delegation was disabled while accepting this request")
                if self.root._session_id != accepted_root or self.harness.session_id != accepted_parent:
                    raise RuntimeError("The parent conversation changed while accepting this request")
            finally:
                self.root._starting -= 1
                self.root._new_starting -= 1
            info = TaskInfo(
                identity.session_id,
                title,
                root_session_id=accepted_root,
                prompt=prompt,
                _storage_id=identity.storage_id,
                parent_session_id=self.harness._agent_session_id,
                _parent_storage_id=accepted_parent,
                depth=self.harness.subagent_depth + 1,
                profile=agent,
                mode="reviewer" if self.harness.mode == "reviewer" else profile.mode,
                provider=binding.provider,
                provider_kind=self.harness.config.provider_profile().kind,
                model=binding.id,
                reasoning_effort=resolve_effort(
                    reasoning_effort, resolve_effort(profile.reasoning_effort, self.harness.reasoning_effort())
                ),
                started_at=time.time(),
                updated_at=time.time(),
            )
            if profile.a2a.configured:
                info.provider, info.provider_kind, info.model, info.reasoning_effort = "a2a", "a2a", "", ""
            activation_origin.apply(info)
            if info.parent_session_id:
                parent_info = self._infos[info.parent_session_id]
                info.origin_history_id = parent_info.origin_history_id
                info.origin_run_id = parent_info.origin_run_id
            elif activation_origin.history_id:
                info.origin_history_id = activation_origin.history_id
                info.origin_run_id = activation_origin.run_id
            if info.provider and not profile.a2a.configured:
                info.provider_kind = accepted_providers[info.provider].kind
                self._provider_overrides[info.session_id] = accepted_providers[info.provider]
            self._infos[info.session_id] = info
            self._evidence[info.session_id] = SessionEvidence()
            self._profile_snapshots[info.session_id] = (profiles, source_profiles)
            self._model_snapshots[info.session_id] = binding
            self._options[info.session_id] = options
            self._inputs[info.session_id] = deque()
            self._admitted[info.session_id] = {}
            try:
                await self._emit_started(info, prompt, admission)
                self._check_active()
                self._check_start_admission(admission)
            except BaseException:
                info.status = "cancelled"
                info.error = "Delegation was interrupted before the child started."
                raise
            admission.ready.set()
            return {"session_id": info.session_id, "status": "running"}
        finally:
            try:
                if not admission.ready.is_set() and admission.workers:
                    await self._cancel_start_workers(admission)
            finally:
                if reserved and not admission.ready.is_set():
                    self.root._used -= 1
                self._start_admissions.discard(admission)
                self._admission_changed()

    def _activation_origin(self) -> _ActivationOrigin:
        """Capture only the current owned call, never provider-supplied lineage."""
        bridge = self.harness.agent._execution_bridge
        if (
            isinstance(bridge, ExecutionBridge)
            and bridge.live()
            and bridge.block is not None
            and not bridge.block.writing
            and bridge.block.row_id > 0
            and bridge.call is not None
            and bridge.call.name == "to_agent"
            and 0 <= bridge.position < len(bridge.block.calls)
            and bridge.block.calls[bridge.position] == bridge.call
        ):
            return _ActivationOrigin(str(bridge.block.row_id), bridge.run_id, bridge.call.id, bridge.position)
        return _ActivationOrigin()

    async def start_task(
        self,
        prompt: str,
        agent: str = "assistant",
        *,
        title: str = "",
        message: RunInput | None = None,
        options: TaskOptions = DEFAULT_TASK_OPTIONS,
    ) -> TaskInfo:
        """Host start with a receipt-bound initial input, using native delegation quotas."""
        if message is not None and message.prompt != prompt:
            raise ValueError("Initial task input must match the assigned prompt")
        result = await self.to_agent(
            prompt,
            agent=agent,
            title=title,
            model=options.model,
            provider=options.provider,
            compact_trigger=options.compact_trigger,
            compact_tokens=options.compact_tokens,
            compact_messages=options.compact_messages,
            reasoning_effort=options.reasoning_effort,
        )
        info = self._lookup(result["session_id"])
        # to_agent opens the worker's execution gate only after its final await.
        # Install this binding without yielding before child work can begin.
        if message is not None:
            accepted = replace(message, identifier=message.identifier or uuid.uuid4().hex)
            self._initial_inputs[info.session_id] = accepted
            self._admitted[info.session_id][accepted.identifier] = accepted
        return copy.deepcopy(info)

    def input_child(self, message_id: str) -> tuple[TaskInfo, Harness]:
        """Resolve an input context only from its exact child's producer task."""
        current = asyncio.current_task()
        if message_id and current is not None:
            for info in self.list():
                child = self._children.get(info.session_id)
                if child is not None and child._worker is current and child.followups.current.message_id == message_id:
                    return info, child
        raise RuntimeError("Input context does not belong to an active child producer")

    def _lookup(self, task_id: str) -> TaskInfo:
        session_sequence(task_id)
        info = self._infos.get(task_id)
        if info is None or info.root_session_id != self.root.harness.session_id or not self._belongs(info):
            raise ValueError("Unknown or unavailable child session in the current root tree; workers are not restored")
        return info

    async def history(self, session_id: str, limit: int = 100) -> builtins.list[Message]:
        """Detached child context, newest 1..200 messages in chronological order.

        This is inspection only, including while busy; a live snapshot may end in
        an unfinished tool block. Respects session compaction. No model calls,
        repairs, or job replay occur.
        """
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("History limit must be an integer between 1 and 200")
        info = self._lookup(session_id)
        return copy.deepcopy(await self.root.harness.agent.session.get_history(info._storage_id, limit))

    def child(self, session_id: str) -> Harness:
        """The exact authorized retained runtime; inspection must never initialize it."""
        self._lookup(session_id)
        child = self._children.get(session_id)
        if child is None:
            raise RuntimeError("Child is still starting or its setup failed")
        return child

    def configure_task(self, session_id: str, options: TaskOptions) -> TaskInfo:
        """Save choices for the next activation without modifying an active child.

        Empty provider/model/trigger retains the task's accepted setting. This
        never applies to notes admitted into the current activation. Explicit
        provider selection snapshots its configured connection at save time.
        """
        if self is not self.root:
            raise PermissionError("Child settings must be submitted to the root harness")
        info = self._lookup(session_id)
        if info.status == "cancelled":
            raise RuntimeError("Cancelled tasks cannot be configured for continuation")
        self._validate_options(info, options)
        self._next_options[session_id] = options
        self._next_revisions[session_id] = self._next_revisions.get(session_id, 0) + 1
        if options.provider:
            self._next_providers[session_id] = replace(self.harness.providers.providers[options.provider])
        else:
            self._next_providers.pop(session_id, None)
        info.next_settings = asdict(options)
        return copy.deepcopy(info)

    def _validate_options(self, info: TaskInfo, options: TaskOptions) -> ProviderProfile:
        """Validate without changing accepted or pending settings."""
        options.validate(self.harness)
        accepted = self.child(info.session_id)
        if accepted._remote_profile.configured:
            raise ValueError("The remote server manages this agent's model, reasoning and compaction settings")
        route = (
            self.harness.providers.providers[options.provider]
            if options.provider
            else accepted.config.provider_profile()
        )
        effort = resolve_effort(options.reasoning_effort, accepted.reasoning_effort())
        if not accepted.config.demo:
            model = options.model or accepted.agent.provider.model
            if route == accepted.config.provider_profile():
                validate_reasoning_choice(effort, accepted.agent.provider.cached_reasoning_efforts(model))
            else:
                validate_effort(effort, route.kind, model, api=route.api, auth=route.auth, base_url=route.base_url)
        return replace(route)

    def _continuation_settings(self, info: TaskInfo, options: TaskOptions) -> _ContinuationSettings:
        accepted = self.child(info.session_id)
        explicit = options != DEFAULT_TASK_OPTIONS
        staged = info.session_id in self._next_options
        selected = options if explicit else self._next_options.get(info.session_id, self._options[info.session_id])
        if explicit:
            route = self._validate_options(info, selected)
        elif staged and selected.provider:
            route = self._next_providers[info.session_id]
        else:
            route = replace(accepted.config.provider_profile())
        retained = self._model_snapshots[info.session_id]
        binding = (
            replace(
                retained,
                provider=selected.provider or retained.provider,
                id=selected.model or retained.id,
            )
            if explicit or staged
            else retained
        )
        return _ContinuationSettings(
            selected, binding, route, explicit or staged, self._next_revisions.get(info.session_id, 0)
        )

    async def context_stats(self, session_id: str) -> dict[str, object]:
        info = self._lookup(session_id)
        child = self.child(session_id)
        if child._remote_profile.configured:
            return {**(await child.context_stats(info._storage_id)).as_dict(), "compaction": {}}
        context = (await child.agent.context_stats(info._storage_id)).as_dict()
        return {**context, "compaction": compaction_snapshot(child)}

    def submit_message(self, session_id: str, message: RunInput) -> bool:
        """Reserve a bounded note for a running child's next complete response boundary.

        False means idle: the host can use ordinary retained-child continuation,
        with no quota consumed here. Unavailable/stopping tasks raise. Acceptance
        never satisfies approvals or interrupts a running tool batch. The host
        owns durable receipts via RunInput.context; process-local queues do not
        replay after restart. Pending notes remain unread if the child fails.
        """
        if self is not self.root:
            raise PermissionError("Child messages must be submitted to the root harness")
        info = self._lookup(session_id)
        worker = self._workers.get(session_id)
        if info.status != "running" and (worker is None or worker.done()):
            if info.status == "cancelled":
                raise RuntimeError("Cancelled children cannot accept messages")
            return False
        self._check_active()
        if worker is None or worker.done() or worker.cancelling() or info.phase in {"stopping", "cancelled", "failed"}:
            raise RuntimeError("Child is stopping; its message queue is closed")
        child = self._children.get(session_id)
        if child is not None and info.phase != "starting" and not child.followups.accepting:
            raise RuntimeError("Child is finishing; retry after completion")
        if not isinstance(message.prompt, str):
            raise ValueError("Child messages require text")
        self._check_budget(message.prompt, slot=False)
        if info.followups >= MAX_FOLLOWUPS:
            raise ValueError(
                f"At most {MAX_FOLLOWUPS} combined human follow-ups and agent follow-ups per child session"
            )
        # Assign an identity without mutating the caller's receipt-owned input.
        message = replace(message, identifier=message.identifier or uuid.uuid4().hex)
        self._inputs.setdefault(session_id, deque()).append(message)
        self._admitted.setdefault(session_id, {})[message.identifier] = message
        self._used += 1
        info.followups += 1
        info.updated_at = time.time()
        if child is not None:
            child.followups.changed()
            child.tasks._changed.set()
        self._changed.set()
        return True

    async def _emit_activity(self, info: TaskInfo, payload: dict[str, object], *, turn_id: str = "") -> None:
        if not payload:
            return
        payload = copy.deepcopy(payload)
        if "response_session_id" in payload:
            payload["response_session_id"] = info.session_id
        context = payload.get("context")
        if isinstance(context, dict):
            for field_name in ("session_id", "response_session_id"):
                if field_name in context:
                    context[field_name] = info.session_id
        info.activity_sequence += 1
        info.updated_at = time.time()
        phase = payload.get("phase")
        if isinstance(phase, str):
            info.phase = phase
        if payload.get("type") == "status":
            payload = {
                **payload,
                "provider": info.provider,
                "provider_kind": info.provider_kind,
                "model": info.model,
                "reasoning_effort": info.reasoning_effort,
            }
        if info.session_id not in self._evidence:
            self._evidence[info.session_id] = SessionEvidence()
        self._evidence[info.session_id].observe(payload)
        child = self._children.get(info.session_id)
        event = TaskActivity(
            info.session_id,
            info.root_session_id,
            info.parent_session_id,
            info.depth,
            info.activation,
            info.activity_sequence,
            info.updated_at,
            turn_id or (child.followups.current.id if child is not None else ""),
            payload,
        )
        if not self.root.harness.emit_activity(event):
            self._activity_gaps.add(info.session_id)

    def drain_activity(self) -> None:
        """Coalesce dropped observation fragments to one bounded refresh per task.

        A sequence gap plus this explicit marker tells consumers to rebuild from
        retained task metadata and persisted history. It never asserts that an
        unfinished streamed tool block was committed, or invents missing text.
        """
        if self is not self.root:
            return
        queue = self.harness._queue
        for task_id in tuple(self._activity_gaps):
            if queue is None or queue.full():
                return
            info = self._infos.get(task_id)
            if info is None or info.root_session_id != self.harness.session_id:
                self._activity_gaps.discard(task_id)
                continue
            info.activity_sequence += 1
            child = self._children.get(task_id)
            event = TaskActivity(
                info.session_id,
                info.root_session_id,
                info.parent_session_id,
                info.depth,
                info.activation,
                info.activity_sequence,
                info.updated_at,
                child.followups.current.id if child is not None else "",
                {"type": "resync_required", "phase": info.phase},
            )
            if self.harness.emit_activity(event):
                self._activity_gaps.discard(task_id)

    async def _input_started(self, info: TaskInfo, child: Harness, turn: RunTurn) -> None:
        message = self._admitted.get(info.session_id, {}).pop(turn.identifier, None)
        if message is not None:
            event = TaskMessage(
                info.session_id,
                info.title,
                "A human message was picked up by this agent.",
                info.parent_session_id,
                info.root_session_id,
                info.depth,
                info.profile,
                info.followups,
                message.message_id or message.identifier,
                info.activation,
            )
            self._publish(info, event)
            await self._emit_activity(
                info,
                {
                    "type": "user_message",
                    "text": str(message.prompt),
                    "message_id": message.message_id or message.identifier,
                    "status": "picked_up",
                },
            )

    async def continue_task(
        self,
        session_id: str,
        prompt: str,
        *,
        trigger: str = "human",
        notifications: tuple[TaskCompleted | TaskMessage, ...] = (),
        message: RunInput | None = None,
        options: TaskOptions = DEFAULT_TASK_OPTIONS,
        title: str = "",
    ) -> TaskInfo:
        """Accept one idle retained conversation without concurrent activations.

        A running target rejects explicitly. Human notes have their separate
        response-boundary queue. The existence check never creates history.
        """
        info = self._lookup(session_id)
        worker = self._workers.get(session_id)
        if (
            session_id in self.root._activating
            or info.status == "running"
            or (worker is not None and not worker.done())
        ):
            raise RuntimeError("Child session is busy; wait for completion before sending work")
        title = validate_title(title)
        self._check_budget(prompt)
        settings = self._continuation_settings(info, options)
        activation_origin = self._activation_origin() if trigger == "agent" else _ActivationOrigin()
        self._accept_continuation(
            session_id,
            prompt,
            trigger=trigger,
            notifications=notifications,
            message=message,
            settings=settings,
            title=title,
            activation_origin=activation_origin,
            validate_only=True,
        )
        expected_root, expected_actor = self.root._session_id, self.harness.session_id
        previous_title = info.title
        self.root._activating.add(session_id)
        self.root._starting += 1
        self.root._used += 1
        accepted = False
        title_attempted = False
        try:
            if not await self.root.harness.agent.session.session_exists(info._storage_id):
                raise ValueError("Child conversation was deleted; no replacement session was created")
            self._check_budget(prompt, reserved=True)
            if title and title != previous_title:
                title_attempted = True
                await self._identities.save_title(info.root_session_id, session_id, title)
            self._check_budget(prompt, reserved=True)
            if expected_root != self.root._session_id or expected_actor != self.harness.session_id:
                raise RuntimeError("The parent conversation changed while accepting this request")
            result = self._accept_continuation(
                session_id,
                prompt,
                trigger=trigger,
                notifications=notifications,
                message=message,
                settings=settings,
                title=title,
                activation_origin=activation_origin,
            )
            accepted = True
            return result
        except BaseException:
            if title_attempted:
                try:
                    await _await_cleanup(
                        asyncio.create_task(
                            self._identities.save_title(info.root_session_id, session_id, previous_title)
                        )
                    )
                except Exception as error:
                    logger.warning(
                        "Child title rollback failed: session=%s error_type=%s", session_id, type(error).__name__
                    )
            raise
        finally:
            self.root._starting -= 1
            self.root._activating.discard(session_id)
            if not accepted:
                self.root._used -= 1
            self._admission_changed()

    def _accept_continuation(
        self,
        task_id: str,
        prompt: str,
        *,
        settings: _ContinuationSettings,
        trigger: str = "human",
        notifications: tuple[TaskCompleted | TaskMessage, ...] = (),
        message: RunInput | None = None,
        title: str = "",
        activation_origin: _ActivationOrigin,
        validate_only: bool = False,
    ) -> TaskInfo:
        """Accept a retained-child activation atomically on the active root run.

        Only completed/failed children with a retained conversation can continue.
        Busy/cancelled tasks are rejected, never queued or silently replayed.
        """
        if trigger not in {"human", "agent", "wakeup", "notification"}:
            raise ValueError("Unknown child activation trigger")
        if message is not None and message.prompt != prompt:
            raise ValueError("Continuation input must match its prompt")
        info = self._lookup(task_id)
        worker = self._workers.get(task_id)
        if info.status == "running" or (worker is not None and not worker.done()):
            raise RuntimeError("Child is busy; wait for completion before sending a follow-up")
        child = self._children.get(task_id)
        if (
            info.status == "cancelled"
            or (worker is not None and worker.cancelled())
            or child is None
            or not child._initialized
        ):
            raise RuntimeError(
                "Child cannot continue after cancellation or incomplete setup; start a new session with to_agent"
            )
        if child.tasks._running():
            raise RuntimeError("Child subtree is busy; wait for descendants before continuing its parent")
        if not self.harness.allow_subagents or not 0 < info.depth <= self.admission_limits().max_subagent_depth:
            raise PermissionError("Continuation is disabled at the configured subagent depth limit")
        ancestor = info
        seen: set[str] = set()
        while True:
            retained = self._children.get(ancestor.session_id)
            ancestor_worker = self._workers.get(ancestor.session_id)
            if (
                ancestor.session_id in seen
                or ancestor.root_session_id != self.root.harness.session_id
                or retained is None
                or not retained._initialized
                or retained.session_id != ancestor._storage_id
                or ancestor.status == "cancelled"
                or (ancestor_worker is not None and ancestor_worker.cancelled())
            ):
                raise RuntimeError("Task ancestry is unavailable or cancelled; results cannot be rerouted to Main")
            if (ancestor.status == "running" or (ancestor_worker is not None and not ancestor_worker.done())) and (
                retained._closed or not retained.tasks._active
            ):
                raise RuntimeError("Ancestor is starting or stopping; wait before continuing its descendant")
            if info.depth > retained.config.max_subagent_depth:
                raise PermissionError("Continuation is disabled at the configured subagent depth limit")
            seen.add(ancestor.session_id)
            if not ancestor.parent_session_id:
                if ancestor._parent_storage_id != self.root.harness.session_id or ancestor.depth != 1:
                    raise RuntimeError("Task ancestry does not match the current root session")
                break
            parent = self._infos.get(ancestor.parent_session_id)
            if (
                parent is None
                or parent._storage_id != ancestor._parent_storage_id
                or parent.depth != ancestor.depth - 1
            ):
                raise RuntimeError("Immediate parent is unavailable; results cannot be rerouted to Main")
            ancestor = parent
        if trigger in {"human", "agent"} and info.followups >= MAX_FOLLOWUPS:
            raise ValueError(
                f"At most {MAX_FOLLOWUPS} combined human follow-ups and agent follow-ups per child session"
            )
        if info.profile not in self.harness.delegatable_profile_names:
            raise PermissionError("The child session's agent profile is no longer available to this caller")
        if validate_only:
            return copy.deepcopy(info)
        if settings.apply:
            previous_binding = self._model_snapshots[info.session_id]
            self._model_snapshots[info.session_id] = settings.binding
            self._options[info.session_id] = settings.options
            if settings.binding.provider:
                self._provider_overrides[info.session_id] = settings.provider
            info.provider, info.provider_kind, info.model = (
                settings.binding.provider,
                settings.provider.kind,
                settings.binding.id,
            )
            info.reasoning_effort = resolve_effort(settings.options.reasoning_effort, child.reasoning_effort())
            if previous_binding != settings.binding:
                info.context = {}
            if settings.options.compact_trigger:
                info.compaction = {}
        if self._next_revisions.get(info.session_id, 0) == settings.revision:
            self._next_options.pop(info.session_id, None)
            self._next_providers.pop(info.session_id, None)
            info.next_settings = {}
        if title:
            info.title = title
        activation_origin.apply(info)
        info.activation += 1
        info.trigger = trigger
        if trigger in {"human", "agent"}:
            info.followups += 1
        info.status, info.result, info.error = "running", "", ""
        info.phase, info.completed_at = "starting", 0.0
        info.parent_response_id = info.parent_response_session_id = ""
        self._evidence[info.session_id] = SessionEvidence()
        info.updated_at = time.time()
        accepted = message or RunInput(prompt)
        accepted = replace(accepted, identifier=accepted.identifier or uuid.uuid4().hex)
        self._inputs[info.session_id] = deque()
        self._admitted[info.session_id] = {accepted.identifier: accepted} if trigger == "human" else {}
        self._workers[info.session_id] = asyncio.create_task(
            self._run_child(info, prompt, message=accepted, notifications=notifications),
            name=f"ngn-subagent-{info.session_id}",
        )
        return replace(info)

    def _started(self, info: TaskInfo, prompt: str) -> TaskStarted:
        return TaskStarted(
            info.session_id,
            info.title,
            prompt,
            info.parent_session_id,
            info.root_session_id,
            info.depth,
            info.profile,
            info.followups,
            info.activation,
            info.trigger,
            info.provider,
            info.provider_kind,
            info.model,
            info.started_at,
            info.origin_history_id,
            info.origin_run_id,
            info.reasoning_effort,
            info.activation_history_id,
            info.activation_run_id,
            info.activation_call_id,
            info.activation_call_position,
        )

    def _create_child(self, agent: str, info: TaskInfo | None = None, *, continuing: bool = False) -> Harness:
        from .runtime import Harness

        parent = self._children[info.session_id] if continuing and info is not None else self.harness
        options = self._options.get(info.session_id, TaskOptions()) if info else TaskOptions()
        if continuing:
            parent.refresh_agent_definitions()
        profiles, source_profiles = (
            self._profile_snapshots[info.session_id]
            if not continuing and info is not None and info.session_id in self._profile_snapshots
            else (parent.config.profiles, parent.config.agent_source_profiles)
        )
        profile = parent.config.profile("assistant") if agent == "assistant" else profiles.get(agent)
        if profile is None or not profile.subagent_available:
            raise PermissionError(f"Agent {agent!r} is no longer available for delegation")
        if continuing and parent._remote_profile.configured:
            # Retained work belongs to its accepted connection, even after a catalog edit.
            profile = replace(profile, a2a=parent._remote_profile)
            profiles = {**profiles, agent: profile}
        if not continuing and agent not in parent.delegatable_profile_names:
            raise PermissionError(f"Agent {agent!r} is not available for delegation from this profile")
        if continuing:
            for owner in parent._permission_parents:
                if agent not in owner.delegatable_profile_names:
                    raise PermissionError(f"Agent {agent!r} is no longer allowed by an ancestor")
        chatgpt = isinstance(parent.agent.provider, OpenAIProvider) and parent.agent.provider.uses_chatgpt_auth
        if (
            not profile.a2a.configured
            and not isinstance(parent.agent.provider, HarnessProvider)
            and not chatgpt
            and not parent.config.provider
        ):
            raise ValueError(
                "Custom provider cloning for subagents is not implemented; no fallback provider is substituted"
            )
        profiles = {name: replace(profile) for name, profile in profiles.items()}
        binding = (
            self._model_snapshots[info.session_id]
            if info is not None and info.session_id in self._model_snapshots
            else parent.delegation_model_binding(agent, model=options.model, provider=options.provider)
        )
        config = replace(
            parent.clone_configuration(),
            agent=agent,
            model_explicit=bool(options.model) or parent.config.model_explicit,
            reasoning_effort=resolve_effort(options.reasoning_effort, parent.reasoning_effort()),
            profiles=profiles,
            agent_source_profiles={name: replace(value) for name, value in source_profiles.items()},
            plugins=(),
            diagnostics=(),
            demo=parent.config.demo or self.root.harness.config.demo,
            providers={
                **parent.config.providers,
                parent.config.provider: replace(
                    parent.config.provider_profile(),
                    auth=parent.config.provider_profile().auth
                    if parent.config.provider
                    else "chatgpt"
                    if chatgpt
                    else "api-key",
                ),
                **(
                    {binding.provider: self._provider_overrides[info.session_id]}
                    if info is not None and binding.provider and info.session_id in self._provider_overrides
                    else {}
                ),
            },
            max_tool_rounds=min(parent.config.max_tool_rounds, 12),
            max_subagent_depth=min(parent.config.max_subagent_depth, self.root.harness.config.max_subagent_depth),
        )
        ceiling = (
            "reviewer"
            if parent.mode == "reviewer"
            or self.root.harness.mode == "reviewer"
            or config.profile(agent).mode == "reviewer"
            else "build"
        )
        # A retained ancestor may have been restricted since this child last ran.
        ancestor = info
        while ancestor is not None:
            retained = self._children.get(ancestor.session_id)
            if ancestor.mode == "reviewer" or (retained is not None and retained.mode == "reviewer"):
                ceiling = "reviewer"
            ancestor = self._infos[ancestor.parent_session_id] if ancestor.parent_session_id else None
        child = Harness(
            config,
            allow_subagents=self.root.harness.allow_subagents,
            provider_registry=replace(
                parent.providers,
                providers={name: profile for name, profile in config.providers.items() if name},
            ),
            shared_auth=self.root.harness.openai_auth,
            model_binding=binding,
        )
        child._reasoning_effort_override = options.reasoning_effort or (
            parent._reasoning_effort_override if continuing else ""
        )
        if child._built_provider_profile == parent._built_provider_profile:
            child.agent.provider.restore_context_metadata(parent.agent.provider.context_metadata_snapshot())
        options.apply_compaction(child, parent)
        child._is_subagent = True
        child._preserve_first_profile = True
        child._agent_session_id = info.session_id if info else ""
        child._agent_title = info.label if info else "subagent"
        child._activation = info.activation if info else 0
        child.subagent_depth = info.depth if info else self.harness.subagent_depth + 1
        child._permission_ceiling = ceiling
        child.inherit_tool_permissions(parent, continuing=continuing)
        if info:
            child.session_id = info._storage_id
            info.mode = ceiling
        child.tasks = SubagentManager(child, self.root)
        child._parent_instructions, child._inherited_profile_instructions = parent.inherited_run_instructions(
            continuing=continuing
        )
        child.instructions = dict(parent.instructions)
        child.agent.tool_registry.clear()
        child.tools.builtins.pop("to_agent", None)
        if child.mode == "reviewer":
            child.tools.builtins = {
                name: tool for name, tool in child.tools.builtins.items() if name in _READ_ONLY_TOOLS
            }
        for name, tool in child.tools.builtins.items():
            child.tools.register_builtin(tool, name=name)
        child.tasks.register()
        child.refresh_instructions()
        return child

    async def _run_child(
        self,
        info: TaskInfo,
        prompt: str,
        *,
        message: RunInput | None = None,
        notifications: tuple[TaskCompleted | TaskMessage, ...] = (),
    ) -> None:
        child: Harness | None = None
        message = message or self._initial_inputs.pop(info.session_id, None)
        started = time.monotonic()
        logger.info(
            "Subagent execution started: task=%s profile=%s session=%s child_session=%s activation=%d",
            info.session_id,
            info.profile,
            info.root_session_id,
            info._storage_id,
            info.activation,
        )
        try:
            if info.activation:
                await self.root.harness.emit(self._started(info, prompt))
            child = self._create_child(info.profile, info, continuing=bool(info.activation))
            self._children[info.session_id] = child
            await _await_cleanup(
                asyncio.create_task(child.initialize(), name=f"ngn-subagent-initialize-{info.session_id}")
            )
            child.refresh_instructions()
            info.mode = child.mode
            info.provider, info.provider_kind, info.model = (
                child.config.provider,
                child.config.provider_profile().kind,
                child.agent.provider.model,
            )
            info.reasoning_effort = child.reasoning_effort()
            info.compaction = compaction_snapshot(child)
            if child._remote_profile.configured:
                info.provider, info.provider_kind, info.model = "a2a", "a2a", ""
                info.reasoning_effort, info.compaction = "", {}
            child.agent.plugins.append(ChildActivity(self, info, child))
            done: DoneEvent | None = None

            async def read_input() -> RunInput | None:
                pending = self._inputs.get(info.session_id)
                return pending.popleft() if pending else None

            consumed_turn = ""

            def observe_turn(turn: RunTurn) -> None:
                nonlocal consumed_turn
                consumed_turn = turn.id

            with child.followups.source(read_input), child.followups.observe(observe_turn):
                async with aclosing(
                    child._run(prompt, trigger=info.trigger, notifications=notifications, initial_input=message)
                ) as events:
                    async for event in events:
                        await self._emit_activity(info, public_payload(event), turn_id=consumed_turn)
                        if isinstance(event, TextDoneEvent) and child.tasks._running():
                            await self._emit_activity(
                                info, {"type": "status", "phase": "waiting", "detail": "Waiting for delegated work"}
                            )
                        if isinstance(event, ErrorEvent) and not event.recoverable:
                            remote_error = remote_error_message(event) if child._remote_profile.configured else ""
                            info.error = subagent_error_message(event)
                            if remote_error and event.code == "A2A_TASK_CANCELLED":
                                info.status = "cancelled"
                            logger.warning(
                                "Subagent failed: task=%s profile=%s session=%s child_session=%s activation=%d "
                                "remote=%s code=%s diagnostic=%s",
                                info.session_id,
                                info.profile,
                                info.root_session_id,
                                info._storage_id,
                                info.activation,
                                child._remote_profile.configured,
                                safe_error_code(event),
                                transport_diagnostic(event.extra) or "unavailable",
                            )
                        elif isinstance(event, DoneEvent):
                            done = event
            if done is None and not info.error:
                info.error = "Subagent ended without a completed response."
            elif done is not None and not info.error:
                info.result = done.final_text[:MAX_RESULT]
                if len(done.final_text) > MAX_RESULT:
                    info.result = info.result[: MAX_RESULT - 24] + "\n[Result truncated]"
        except asyncio.CancelledError:
            info.status = "cancelled"
            info.error = "Subagent cancelled with its parent run; it will not be replayed."
            raise
        except Exception as error:
            logger.warning(
                "Subagent execution failed: task=%s profile=%s session=%s child_session=%s activation=%d "
                "remote=%d elapsed_ms=%.1f error_type=%s",
                info.session_id,
                info.profile,
                info.root_session_id,
                info._storage_id,
                info.activation,
                int(child is not None and child._remote_profile.configured),
                (time.monotonic() - started) * 1000,
                type(error).__name__,
            )
            # Provider/extension exceptions may contain credentials or raw HTTP bodies.
            # An owned operation can finish with an error while this worker is
            # already cancelling. Preserve the stop request at this boundary;
            # shared owned-operation joins still expose their original errors.
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                info.status = "cancelled"
                info.error = "Subagent cancelled; it will not be replayed. Subagent setup or execution failed."
                raise asyncio.CancelledError from None
            info.status = "failed"
            info.error = (
                "Subagent operation timed out during setup or execution; no successful result is available."
                if isinstance(error, TimeoutError)
                else "Subagent failed during setup or execution; no successful result is available."
            )
        finally:
            info.phase = "stopping"
            try:
                if child is not None:
                    await _await_cleanup(
                        asyncio.create_task(child.close(), name=f"ngn-subagent-close-{info.session_id}")
                    )
            except asyncio.CancelledError:
                if info.status != "cancelled":
                    info.status = "cancelled"
                    info.error = "Subagent cancelled with its parent run; it will not be replayed."
                raise
            except Exception as cleanup_error:
                logger.warning(
                    "Subagent cleanup failed: task=%s profile=%s error_type=%s",
                    info.session_id,
                    info.profile,
                    type(cleanup_error).__name__,
                )
                task = asyncio.current_task()
                if info.status == "cancelled" or (task is not None and task.cancelling()):
                    info.status = "cancelled"
                    info.error = "Subagent cancelled; it will not be replayed. Subagent resource cleanup failed."
                    raise asyncio.CancelledError from None
                info.status = "failed"
                info.error = (
                    info.error + " Subagent resource cleanup also failed."
                    if info.error
                    else "Subagent resource cleanup failed."
                )
            finally:
                if info.status == "running":
                    info.status = "failed" if info.error else "completed"
                info.phase = info.status
                info.updated_at = info.completed_at = time.time()
                logger.info(
                    "Subagent execution finished: task=%s profile=%s session=%s child_session=%s activation=%d "
                    "status=%s elapsed_ms=%.1f",
                    info.session_id,
                    info.profile,
                    info.root_session_id,
                    info._storage_id,
                    info.activation,
                    info.status,
                    (time.monotonic() - started) * 1000,
                )
                self._inputs.pop(info.session_id, None)
                self._admitted.pop(info.session_id, None)
                parent = self._children.get(info.parent_session_id) if info.parent_session_id else self.root.harness
                info.parent_response_id = (
                    parent.agent._active_generation_id(parent.session_id)
                    if parent is not None and not parent._closed
                    else ""
                )
                info.parent_response_session_id = (
                    (info.parent_session_id or info.root_session_id) if info.parent_response_id else ""
                )
                self._publish(
                    info,
                    TaskCompleted(
                        info.session_id,
                        info.title,
                        info.result,
                        info.error,
                        info.parent_session_id,
                        info.root_session_id,
                        info.depth,
                        info.profile,
                        info.followups,
                        info.status,
                        info.activation,
                        info.trigger,
                        info.provider,
                        info.provider_kind,
                        info.model,
                        info.reasoning_effort,
                        parent_response_id=info.parent_response_id,
                        parent_response_session_id=info.parent_response_session_id,
                        completed_at=info.completed_at,
                    ),
                )

    def _publish(self, info: TaskInfo, event: TaskCompleted | TaskMessage) -> None:
        # Observability is root-wide; model data belongs only to the immediate parent.
        # Keep cleanup nonblocking, including when an inactive parent needs recreation.
        if not self.root._active or self.root.harness._closed:
            return
        self.root._observed.append(event)
        self.root._changed.set()
        parent_id = info.parent_session_id
        parent_info = self._infos.get(parent_id)
        if info.root_session_id != self.root.harness.session_id or (
            (parent_id and (parent_info is None or parent_info._storage_id != info._parent_storage_id))
            or (not parent_id and (info._parent_storage_id != info.root_session_id or info.depth != 1))
        ):
            self.root._observed.append(
                ErrorEvent(message="Immediate parent is unavailable; descendant result was not forwarded to Main.")
            )
            return
        if not parent_id:
            self.root._ready.append(event)
        else:
            parent = self._children.get(parent_id)
            worker = self._workers.get(parent_id)
            if parent_info is not None and (
                parent_info.status == "cancelled"
                or (worker is not None and worker.cancelled())
                or (
                    worker is not None
                    and not worker.done()
                    and parent is not None
                    and (parent._closed or parent.tasks._shutdown is not None)
                    and parent._activation == parent_info.activation
                )
            ):
                self.root._observed.append(
                    TaskDeliveryWarning(
                        session_id=info.session_id,
                        title=info.title,
                        parent_session_id=info.parent_session_id,
                        root_session_id=info.root_session_id,
                        depth=info.depth,
                        activation=info.activation,
                        followup=info.followups,
                    )
                )
                return
            if parent is not None and parent.tasks._active and not parent._closed:
                parent.tasks._ready.append(event)
                parent.tasks._changed.set()
            else:
                self.root._pending.setdefault(parent_id, []).append(event)

    async def wakeup_notification(self, prompt: str) -> str:
        """Deliver a self-wake at the recipient's next complete run boundary."""
        task_id, name = self.harness._agent_session_id, self.harness._agent_title or "Main"
        await self.root.harness.emit(
            TaskNotification(
                uuid.uuid4().hex,
                task_id,
                task_id,
                name,
                name,
                "wakeup",
                prompt,
                self.root._session_id,
                source_activation=self.harness._activation,
            )
        )
        return format_wakeup(prompt)

    async def flush_observed(self) -> None:
        """Emit already queued root lifecycle records without resuming a model.

        Child managers do not own this root-wide observer queue. In particular,
        a terminal child failure must leave sibling outcomes for the root.
        This does not wait for workers or consume their parent-routed inboxes.
        """
        if self is self.root:
            self.drain_activity()
            while self._observed:
                await self.harness.emit(replace(self._observed.popleft()))

    async def notification(self, *, wait_for_tasks: bool = False) -> str | None:
        """Drain outcomes only at an outer Agent.run boundary, batching ready jobs."""
        if self.harness.session_id != self._session_id:
            raise RuntimeError("Subagent results cannot be delivered to another session")
        self.notification_sources = ()
        while True:
            if self is self.root:
                await self.flush_observed()
                for parent_id in tuple(self._pending):
                    parent = self._children.get(parent_id)
                    worker = self._workers.get(parent_id)
                    if parent is not None and parent.tasks._active and not parent._closed:
                        parent.tasks._ready.extend(self._pending.pop(parent_id))
                        parent.tasks._changed.set()
                        continue
                    if (worker is not None and not worker.done()) or (parent is not None and parent.tasks._running()):
                        continue
                    pending = tuple(self._pending.pop(parent_id))
                    try:
                        await self.continue_task(
                            parent_id,
                            "Consider your immediate child's background results.",
                            trigger="notification",
                            notifications=pending,
                        )
                    except Exception:
                        await self.harness.emit(
                            ErrorEvent(
                                message="Immediate parent could not be reactivated: unavailable, cancelled, disabled, or quota exhausted. "
                                "Descendant result was not forwarded to Main."
                            )
                        )
            if self.harness.followups.pending:
                return None
            if self._ready and not (wait_for_tasks and self._running()):
                break
            if not self._running():
                return None
            self._changed.clear()
            await self._changed.wait()
        batch = format_batch(tuple(self._ready))
        ready = [self._ready.popleft() for _ in range(batch.count)]
        self.notification_sources = tuple(event.session_id for event in ready)
        for event in ready:
            if isinstance(event, TaskCompleted):
                await self.root.harness.emit(
                    TaskNotification(
                        uuid.uuid4().hex,
                        event.session_id,
                        self.harness._agent_session_id,
                        event.title,
                        self.harness._agent_title or "Main",
                        "completion",
                        event.error or event.result,
                        self.root._session_id,
                        source_activation=event.activation,
                    )
                )
        return batch.text

    async def end(self) -> None:
        """Cancel and await this entire subtree, without UI backpressure waits."""
        self._active = False
        self._stopped.set()
        for admission in self._start_admissions:
            if self._owns_start_admission(admission):
                for generation_stop in admission.stops:
                    # Root and live ancestor events are shared with siblings;
                    # only this shutdown's own scope is revoked here.
                    if generation_stop is admission.owner._stopped:
                        generation_stop.set()
        if self._shutdown is None:

            async def stop() -> None:
                workers = [worker for task_id, worker in self._workers.items() if self._belongs(self._infos[task_id])]
                for worker in workers:
                    if not worker.done():
                        worker.cancel()
                if workers:
                    await asyncio.gather(*workers, return_exceptions=True)
                for info in self._infos.values():
                    if self._belongs(info) and info.status == "running":
                        info.status = "cancelled"
                        info.error = "Subagent cancelled before completion; it will not be replayed."
                self._ready.clear()
                self._observed.clear()
                self._pending.clear()
                if self is self.root:
                    self._activity_gaps.clear()
                self._changed.set()

            self._shutdown = asyncio.create_task(stop(), name="ngn-subagent-cleanup")
        await _await_cleanup(self._shutdown)

    async def release_session(self, session_id: str) -> builtins.list[TaskInfo]:
        """Forget a stopped root's descendants without closing the shared harness.

        The host must stop the root run and exclude new admissions first. Child
        workers own their resource cleanup, so join them before removing any
        shared registry entries. Saved histories remain the host's responsibility.
        Return detached terminal metadata for the host's history journal. Other
        sessions retained by this manager are unaffected.
        """
        if self is not self.root:
            raise PermissionError("Only the root harness can release a session's tasks")
        if (self._active and self._session_id == session_id) or (
            self.harness._busy == "run" and self.harness.session_id == session_id
        ):
            raise RuntimeError("Stop the root run before releasing its descendant tasks")

        async def release() -> builtins.list[TaskInfo]:
            identifiers = {info.session_id for info in self._infos.values() if info.root_session_id == session_id}
            workers = [worker for task_id, worker in self._workers.items() if task_id in identifiers]
            for worker in workers:
                if not worker.done():
                    worker.cancel()
            if workers:
                await asyncio.gather(*workers, return_exceptions=True)
            if self._session_id == session_id and self._shutdown is not None:
                await _await_cleanup(self._shutdown)
            released = []
            for task_id in identifiers:
                info = self._infos[task_id]
                if info.status == "running":
                    info.status = "cancelled"
                    info.error = "Session removed before completion; this task will not be replayed."
                    info.phase = info.status
                    info.updated_at = info.completed_at = time.time()
                released.append(copy.deepcopy(info))
            # Child managers share the flat registry, but each owns its inbox.
            # No await after this point: observers cannot see half-removed trees.
            managers = [self, *(child.tasks for child in self._children.values())]
            for manager in managers:
                if manager is not self and manager.harness._agent_session_id in identifiers:
                    manager._active = False
                    manager._ready.clear()
                    manager._observed.clear()
                    manager._pending.clear()
                elif manager is self and self._session_id == session_id:
                    manager._ready.clear()
                    manager._observed.clear()
                    manager._pending.clear()
                else:
                    manager._ready = deque(event for event in manager._ready if event.session_id not in identifiers)
                    manager._observed = deque(
                        event
                        for event in manager._observed
                        if not isinstance(event, TaskCompleted | TaskMessage | TaskDeliveryWarning)
                        or event.session_id not in identifiers
                    )
                    manager._pending = {
                        parent: [event for event in events if event.session_id not in identifiers]
                        for parent, events in manager._pending.items()
                        if parent not in identifiers
                    }
                manager.notification_sources = tuple(
                    task_id for task_id in manager.notification_sources if task_id not in identifiers
                )
                manager._changed.set()
            for task_id in identifiers:
                self._workers.pop(task_id, None)
                self._children.pop(task_id, None)
                self._profile_snapshots.pop(task_id, None)
                self._model_snapshots.pop(task_id, None)
                self._options.pop(task_id, None)
                self._next_options.pop(task_id, None)
                self._next_revisions.pop(task_id, None)
                self._provider_overrides.pop(task_id, None)
                self._next_providers.pop(task_id, None)
                self._inputs.pop(task_id, None)
                self._admitted.pop(task_id, None)
                self._initial_inputs.pop(task_id, None)
                self._evidence.pop(task_id, None)
                self._infos.pop(task_id, None)
            self._activity_gaps.difference_update(identifiers)
            return released

        return await _await_cleanup(asyncio.create_task(release(), name="ngn-subagent-session-release"))
