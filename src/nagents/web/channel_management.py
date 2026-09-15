"""Approved, process-local connection changes, committed by the host's idle worker.

ChannelHost owns this plugin and its normally approved tools. Eligibility requires
an existing root with no permanent chat owner. WebState.finish records the joined
producer's outcome; the serialized worker applies ready requests at idle before
claiming another turn. Shutdown drops pending input and joins any accepted save
before closing connectors and removing the plugin.

QUEUED is not saved: one bounded request lives only in this process until its exact
originating run completes successfully. Cancellation/failure/shutdown discard it;
restart never replays it. An idle apply is an ordinary accepted ChannelHost.save:
join an in-progress flush on shutdown rather than cancelling it after persistence.
Tool arguments/user chat remain in the normal approval and transcript surfaces;
CredentialGuard protects connector/catalog/results, not user-authored transcripts.
"""

from __future__ import annotations

import secrets
from collections import deque
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING
from typing import Literal
from typing import cast

from fastapi import HTTPException

from nagents.channels.runtime import _identifier
from nagents.channels.runtime import _object_copy
from nagents.channels.types import ChannelError
from nagents.extensions import AgentPlugin

from .catalog import ConnectionInput

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable

    from nagents.channels.types import ChannelValue
    from nagents.extensions import ModelRequest
    from nagents.extensions import RunContext
    from nagents.types import JsonSchema
    from nagents.types import ToolDefinition

    from .channel_host import ChannelHost
    from .service import Run

NAMES = ("channel_configuration", "channel_configure")
MAX_RESULTS = 32
_CONFIGURATION_FIELDS = {
    "revision",
    "plugin",
    "enabled",
    "auto_reply",
    "chat_approvals",
    "config",
    "secrets",
    "main_session_id",
}
_CONNECTOR_STATES = {"running", "idle", "disabled", "error", "unavailable"}


@dataclass(repr=False)
class _Queued:
    request_id: str
    connection_id: str
    run: Run
    body: ConnectionInput = field(repr=False)
    ready: bool = False


class ChannelManagement(AgentPlugin):
    """Host-owned tools; eligibility must query permanent ownership, not bindings."""

    def __init__(self, host: ChannelHost, eligible: Callable[[str], Awaitable[bool]]) -> None:
        self.host = host
        self.eligible = eligible
        self.closed = False
        self._pending: dict[str, _Queued] = {}  # At most one globally, including ready work.
        self._last_run_id = ""
        self._results: deque[dict[str, ChannelValue]] = deque(maxlen=MAX_RESULTS)

    def register_tools(self) -> list[ToolDefinition]:
        """Return owned definitions for host cleanup; keep normal custom-tool approval."""
        registry = self.host.state.harness.agent.tool_registry
        if any(registry.get(name) is not None for name in NAMES):
            raise RuntimeError("Channel management tool name collision")
        return [
            registry.register(
                self.channel_configuration,
                parameters={
                    "type": "object",
                    "properties": {"operation": {"type": "string", "enum": ["discover", "status"]}},
                    "additionalProperties": False,
                },
            ),
            registry.register(
                self.channel_configure,
                parameters=cast(
                    "JsonSchema",
                    {
                        "type": "object",
                        "properties": {
                            "connection_id": {"type": "string"},
                            "configuration": {
                                "type": "object",
                                "properties": {
                                    "revision": {"type": "string"},
                                    "plugin": {"type": "string"},
                                    "enabled": {"type": "boolean"},
                                    "auto_reply": {
                                        "type": "boolean",
                                        "description": "Allow automatic communication within the durable owning chat, including owned-session scheduled work. Privileged tools retain approval. Omit to preserve the same connector's saved policy; defaults to false for a new connector.",
                                    },
                                    "chat_approvals": {
                                        "type": "boolean",
                                        "description": "Allow the durable owning chat to decide tool approvals from the channel without a live browser subscriber. Omit to preserve the same connector's saved policy; defaults to false for a new connector.",
                                    },
                                    "config": {"type": "object"},
                                    "secrets": {"type": "object"},
                                    "main_session_id": {"type": "string"},
                                },
                                "required": ["revision", "plugin", "enabled"],
                                "additionalProperties": False,
                            },
                        },
                        "required": ["connection_id", "configuration"],
                        "additionalProperties": False,
                    },
                ),
            ),
        ]

    def _check_active(self, run: Run) -> None:
        state = self.host.state
        if (
            self.closed
            or self.host.closed
            or state.active is not run
            or run.finished
            or run.background
            or run.source
            or run.outcome != "completed"
            or run.task.done()
            or run.task.cancelling()
            or state.harness.session_id != run.session_id
            or state.harness._is_subagent
        ):
            raise ChannelError("Channel management requires an active trusted unassigned web root.")

    async def _active(self) -> Run:
        run = self.host.state.active
        if run is None:
            raise ChannelError("Channel management requires an active trusted unassigned web root.")
        self._check_active(run)
        try:
            allowed = await self.eligible(run.session_id)
        except Exception:
            allowed = False
        self._check_active(run)  # Ownership lookup may race cancellation or shutdown.
        if not allowed:
            raise ChannelError("Channel management requires an active trusted unassigned web root.")
        return run

    async def before_model(self, context: RunContext, request: ModelRequest) -> ModelRequest:
        try:
            run = await self._active()
            allowed = context.agent is self.host.state.harness.agent and context.session_id == run.session_id
        except ChannelError:
            allowed = False
        if not allowed:
            request.tools = [tool for tool in request.tools if tool.name not in NAMES]
        return request

    async def channel_configuration(
        self, operation: Literal["discover", "status"] = "discover"
    ) -> dict[str, ChannelValue]:
        """Discover installed plugin schemas/private field names, host-controlled plugin_path for approved installation, and revision, or read configuration request status; trusted unassigned web roots only. Host discovery is authoritative: a subprocess may not include the host's plugin directory on sys.path. QUEUED is process-local and not saved; APPLIED includes the actual connector status."""
        await self._active()
        catalog = self.host.catalog
        try:
            if operation == "discover":
                catalog.discover(descriptors=True)
                # Publish the host's installation directory with private-config
                # filtering; the complete result below is credential-checked.
                snapshot = catalog.snapshot([])
                plugins = [
                    {"id": id, **plugin.public_data(), "secret_fields": sorted(plugin.secret_fields())}
                    for id, plugin in sorted(catalog.plugins.items())
                ]
                result = _object_copy(
                    {
                        "revision": snapshot["revision"],
                        "plugin_path": snapshot["plugin_path"],
                        "plugins": plugins,
                        "connections": snapshot["connections"],
                    }
                )
            elif operation == "status":
                result = _object_copy({"revision": catalog.revision, "requests": list(self._results)})
            else:
                raise ValueError
            catalog.protection.check(result)
            return result
        except Exception:
            raise ChannelError(
                "Channel configuration data is unavailable or withheld by credential protection."
            ) from None

    async def channel_configure(
        self, connection_id: str, configuration: dict[str, ChannelValue]
    ) -> dict[str, ChannelValue]:
        """Queue ONE approved connection configuration per active trusted web run. Supply connection_id and strict ConnectionInput JSON: revision, plugin, enabled, auto_reply, chat_approvals, config, secrets, main_session_id. auto_reply is an optional boolean for automatic owning-chat communication; chat_approvals is an optional boolean allowing the durable owning chat to decide approvals without a browser; omission preserves the same connector's saved policy. Put private fields in secrets (omitted values preserve, empty strings clear); config replaces public fields. Empty main_session_id preserves the existing main or uses the active run root. QUEUED is not saved or running: apply only at idle after this run succeeds, retaining revision checks. Failure/cancellation/restart discard it. Use channel_configuration status on a later turn."""
        run = await self._active()
        if self._pending or self._last_run_id == run.id:
            raise ChannelError("Only one channel configuration may be queued per run; finish this run first.")
        catalog = self.host.catalog
        try:
            _identifier(connection_id, "connection ID")
            values = _object_copy(configuration)
            if values.keys() - _CONFIGURATION_FIELDS:
                raise ValueError
            body = ConnectionInput.model_validate(values)
            # Remember even rejected/retired credentials before exposing any plugin data.
            catalog.protection.remember(body.secrets)
            old = catalog.connections.get(connection_id)
            body.main_session_id = body.main_session_id or (old.main_session_id if old else run.session_id)
            catalog.prepare(connection_id, body, body.main_session_id)
            # No factory, disk write, source replacement, or instruction update during a turn.
            item = _Queued(secrets.token_urlsafe(24), connection_id, run, body)
            result = self._record(item, "QUEUED", "AWAITING_SUCCESSFUL_RUN")
            catalog.protection.check(result)
        except Exception:
            raise ChannelError("Channel configuration rejected; rediscover and check the connection fields.") from None
        self._pending[run.id] = item
        self._last_run_id = run.id
        self._results.append(result)
        return dict(result)

    @staticmethod
    def _record(item: _Queued, status: str, reason: str, *, saved: bool = False) -> dict[str, ChannelValue]:
        return {
            "request_id": item.request_id,
            "connection_id": item.connection_id,
            "run_id": item.run.id,
            "session_id": item.run.session_id,
            "status": status,
            "reason": reason,
            "saved": saved,
            "process_local": True,
        }

    def _complete(self, item: _Queued, status: str, reason: str, *, saved: bool = False) -> None:
        result = self._record(item, status, reason, saved=saved)
        connector = self.host.catalog.status.get(item.connection_id, ("unavailable", ""))[0]
        result["connector_status"] = connector if connector in _CONNECTOR_STATES else "unavailable"
        self._results = deque(
            (record for record in self._results if record["request_id"] != item.request_id), maxlen=MAX_RESULTS
        )
        # Every public read is rechecked against newly learned credentials.
        self._results.append(result)

    @staticmethod
    def _successful(run: Run) -> bool:
        return (
            run.finished
            and run.task.done()
            and not run.task.cancelled()
            and not run.task.cancelling()
            and run.task.exception() is None
            and run.outcome == "completed"
        )

    def run_finished(self, run: Run) -> None:
        """After producer join and run.finished=True; never call from Agent.after_run."""
        item = self._pending.get(run.id)
        if item is None or item.run is not run:
            return
        if self.closed or self.host.closed or not self._successful(run):
            del self._pending[run.id]
            self._complete(item, "CANCELLED" if run.outcome == "cancelled" else "FAILED", "ORIGIN_NOT_SUCCESSFUL")
        else:
            item.ready = True
            self.host.changed.set()

    @property
    def ready(self) -> bool:
        return not self.closed and any(item.ready for item in self._pending.values())

    async def flush(self) -> None:
        """Worker-only, under state.idle(), before another run; never auto-retry a save."""
        if self.closed or self.host.closed:
            self.shutdown()
            return
        if self.host.state.active is not None or not self.host.state.mutating:
            raise RuntimeError("Channel configuration flush requires the worker's idle boundary.")
        if not self.ready:
            return
        item = next(iter(self._pending.values()))
        del self._pending[item.run.id]  # Consume before any await; interruption never replays it.
        catalog = self.host.catalog
        old = catalog.connections.get(item.connection_id)
        try:
            allowed = await self.eligible(item.run.session_id)
            if not self._successful(item.run) or not allowed:
                self._complete(item, "FAILED", "ORIGIN_NOT_ELIGIBLE")
                return
            if self.closed or self.host.closed:
                self._complete(item, "CANCELLED", "HOST_STOPPING")
                return
            # Keep the submitted revision. Host.save reuses prepare/construct/save
            # and validates roots; a concurrent UI edit is a stale failure.
            result = await self.host.save(item.connection_id, item.body)
            catalog.protection.check(result)
        except BaseException as error:
            saved = catalog.connections.get(item.connection_id) is not old
            stale = (
                not saved
                and isinstance(error, HTTPException)
                and error.status_code == 409
                and item.body.revision != catalog.revision
            )
            self._complete(item, "FAILED", "STALE_REVISION" if stale else "APPLY_FAILED", saved=saved)
            if not isinstance(error, Exception):
                raise
        else:
            self._complete(item, "APPLIED", "SAVED", saved=True)

    def shutdown(self) -> None:
        """Drop all not-yet-applied input; the host joins any already-running flush."""
        self.closed = True
        for item in self._pending.values():
            self._complete(item, "CANCELLED", "HOST_STOPPING")
        self._pending.clear()
        self.host.changed.set()
