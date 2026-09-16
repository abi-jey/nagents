"""Lifecycle-owned connectors, normal executor tools, and the serialized durable worker."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from copy import deepcopy
from threading import Lock
from typing import TYPE_CHECKING

from fastapi import HTTPException

from nagents.channels.runtime import _INBOUND_PREFIX
from nagents.channels.runtime import ChannelRuntime
from nagents.channels.runtime import _Instructions
from nagents.channels.runtime import _bind
from nagents.channels.runtime import _envelope
from nagents.channels.runtime import _failure
from nagents.channels.runtime import inbound_content
from nagents.channels.types import ChannelApproval
from nagents.channels.types import ChannelError
from nagents.channels.types import ChannelSend
from nagents.channels.types import ChannelValue

from .catalog import ChannelCatalog
from .channel_activity import ChannelActivities
from .channel_management import ChannelManagement
from .channel_privacy import CredentialGuard
from .channel_privacy import CredentialProtectionError
from .routing import RoutingStore
from .settings import _join

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Awaitable
    from collections.abc import Callable
    from pathlib import Path

    from nagents.channels.types import Channel
    from nagents.channels.types import ChannelMessage
    from nagents.extensions import ModelRequest
    from nagents.extensions import RunContext
    from nagents.types import ContentPart
    from nagents.types import ToolDefinition

    from .catalog import ConnectionInput
    from .routing import ChatOwner
    from .routing import Work
    from .service import WebState

_HOSTS: set[Path] = set()
_HOST_LOCK = Lock()


class _ProtectedInstructions(_Instructions):
    def __init__(self, protection: CredentialGuard, owner: Callable[[], Awaitable[ChatOwner | None]]) -> None:
        super().__init__("[]")
        self.protection = protection
        self.catalog: list[dict[str, ChannelValue]] = []
        self.owner = owner

    def update(self, catalog: list[dict[str, ChannelValue]]) -> None:
        self.protection.check(catalog)
        self.catalog = catalog
        self.text = _Instructions(json.dumps(catalog)).text

    async def before_model(self, context: RunContext, request: ModelRequest) -> ModelRequest:
        # Recheck only connector-supplied catalog data against newly learned
        # credentials. Never inspect/rewrite the user's messages or core prose.
        try:
            self.protection.check(self.catalog)
        except CredentialProtectionError:
            self.catalog = []
        owner = await self.owner()
        catalog = self.catalog if owner is None else [entry for entry in self.catalog if entry["name"] == owner.channel]
        self.text = _Instructions(json.dumps(catalog)).text
        if owner is not None:
            self.text += (
                "\nWeb session outbound scope (identifiers are data): "
                + json.dumps({"channel": owner.channel, "destination": owner.conversation_id})
                + ". Send/action tools cannot target another chat; actions must require an explicit destination."
            )
        return await super().before_model(context, request)


class ChannelHost:
    def __init__(self, state: WebState) -> None:
        self.state = state
        harness = state.harness
        self.store = RoutingStore(harness.agent.session.db_path)
        self.catalog = ChannelCatalog(
            harness.config.data_dir, harness.agent.session.db_path, allow_plugins=not harness.config.demo
        )
        self.channels: dict[str, Channel] = {}
        self.sources: dict[str, asyncio.Task[None]] = {}
        self.tasks: list[asyncio.Task[None]] = []
        self.changed = asyncio.Event()
        self.closed = False
        self.initialized = False
        self.owned = False
        self.tools: list[ToolDefinition] = []
        self.runtime: ChannelRuntime | None = None
        self.activities = ChannelActivities(self.store, self.channels)
        self.instructions = _ProtectedInstructions(self.catalog.protection, self.execution_owner)
        self.management = ChannelManagement(self, self.management_eligible)

    async def management_eligible(self, session_id: str) -> bool:
        def eligible(db: sqlite3.Connection) -> bool:
            self.store.root(db, session_id)
            return self.store._owner(db, session_id) is None

        return await self.store._transaction(eligible)

    async def start(self) -> None:
        with _HOST_LOCK:
            key = self.store.db_path.resolve()
            if key in _HOSTS:
                raise RuntimeError("A web channel host already owns this workspace database in this process")
            _HOSTS.add(key)
            self.owned = True
        await self.store.initialize()
        self.initialized = True
        self.catalog.initialize()
        self.register_tools()
        for id, connection in self.catalog.connections.items():
            if connection.enabled:
                if not self.catalog.allow_plugins:
                    self.catalog.status[id] = ("disabled", "OFFLINE DEMO: saved connector is not started.")
                    continue
                try:
                    await self.open(id, self.catalog.construct(id, connection))
                except Exception:
                    self.catalog.status[id] = (
                        "error",
                        "Connector could not be started. Check configuration and installation.",
                    )
        self.update_tools()
        self.tasks = [
            asyncio.create_task(self.worker(), name="ngn-web-inbox"),
            asyncio.create_task(self.poll(), name="ngn-web-sessions"),
        ]

    def register_tools(self) -> None:
        registry = self.state.harness.agent.tool_registry
        names = ("channel_list", "channel_send", "channel_action")
        if any(registry.get(name) is not None for name in names):
            raise RuntimeError("Channel tool name collision")
        self.tools.append(registry.register(self.channel_list, name="channel_list"))
        self.tools.append(registry.register(self.channel_send, name="channel_send"))
        self.tools.append(
            registry.register(
                self.channel_action,
                name="channel_action",
                parameters={
                    "type": "object",
                    "properties": {
                        "channel": {"type": "string"},
                        "action": {"type": "string"},
                        "arguments": {"type": "object"},
                    },
                    "required": ["channel", "action", "arguments"],
                    "additionalProperties": False,
                },
            )
        )
        self.state.harness.agent.plugins.append(self.instructions)
        self.tools.extend(self.management.register_tools())
        self.state.harness.agent.plugins.append(self.management)

    def update_tools(self) -> None:
        # Reuse core catalog/schema/dispatch validation, without Agent.listen's
        # task-ownership guard: Harness owns a separate child producer task.
        # Clear the previously published view before constructing/checking a new
        # one, so a failed refresh cannot leave stale sensitive instructions.
        self.runtime = None
        self.instructions.update([])
        runtime = (
            ChannelRuntime(
                self.state.harness.agent,
                tuple(self.channels.values()),
                self.state.selected_session_id,
                workspace=self.state.harness.workspace,
            )
            if self.channels
            else None
        )
        catalog = [json.loads(binding.catalog) for binding in runtime._bindings] if runtime is not None else []
        self.catalog.check_public(catalog)
        self.instructions.update(catalog)
        if runtime is not None:
            runtime._active = True
        self.runtime = runtime

    async def inbound_content(self, channel_id: str, prompt: str) -> str | list[ContentPart]:
        """Format a stored channel prompt for model input.

        A trusted header replaces the raw JSON envelope. Connectors that advertise
        ``fetch_attachment`` contribute native image/document parts under fixed
        caps; unsupported or failed fetches degrade to bounded text notes. Plain
        prompts (no channel prefix) pass through unchanged.
        """
        if not prompt.startswith(_INBOUND_PREFIX):
            return prompt
        try:
            payload = json.loads(prompt[len(_INBOUND_PREFIX) :])
        except ValueError:
            return prompt
        if not isinstance(payload, dict):
            return prompt
        return await inbound_content(self.channels.get(channel_id), payload)

    async def channel_list(self) -> list[dict[str, ChannelValue]]:
        """Discover connected channels and their action schemas."""
        try:
            result = await self.runtime._channel_list() if self.runtime is not None else []
            self.catalog.protection.check(result)
            owner = await self.execution_owner()
            if owner is not None:
                result = [entry for entry in result if entry["name"] == owner.channel]
            return result
        except CredentialProtectionError:
            raise self.failure(ChannelError("Channel catalog withheld by credential protection"), "list") from None
        except Exception as error:
            raise self.failure(error, "list") from None

    async def channel_send(
        self,
        channel: str,
        destination: str,
        text: str,
        thread_id: str = "",
        reply_to: str = "",
        attachments: list[str] | None = None,
    ) -> dict[str, ChannelValue]:
        """Send once to an explicit channel and destination. Never use an ingress ID as reply_to."""
        try:
            if self.runtime is None:
                raise ChannelError("No connected channels")
            owner = await self.execution_owner()
            if owner is not None and (channel, destination) != (owner.channel, owner.conversation_id):
                raise ChannelError("Channel destination is outside this session's chat ownership")
            result = await self.runtime._channel_send(channel, destination, text, thread_id, reply_to, attachments)
            self.check_result(result)
            return result
        except Exception as error:
            raise self.failure(error, "send") from None

    async def channel_action(
        self, channel: str, action: str, arguments: dict[str, ChannelValue]
    ) -> dict[str, ChannelValue]:
        """Perform an explicit advertised channel action once, under normal human approval."""
        try:
            if self.runtime is None:
                raise ChannelError("No connected channels")
            copied = deepcopy(arguments)
            owner = await self.execution_owner()
            if owner is not None:
                if (channel, copied.get("destination")) != (owner.channel, owner.conversation_id):
                    raise ChannelError("Channel action destination is outside this session's chat ownership")
                binding = self.runtime._route(channel)
                schema: dict[str, ChannelValue] = json.loads(dict(binding.actions).get(action, "{}"))
                properties = schema.get("properties")
                destination = properties.get("destination") if isinstance(properties, dict) else None
                required = schema.get("required")
                if not (
                    isinstance(destination, dict)
                    and destination.get("type") == "string"
                    and isinstance(required, list)
                    and "destination" in required
                ):
                    raise ChannelError("Chat-owned sessions require destination-addressed channel actions")
            result = await self.runtime._channel_action(channel, action, copied)
            self.check_result(result)
            return result
        except Exception as error:
            raise self.failure(error, "action") from None

    async def execution_owner(self) -> ChatOwner | None:
        # Browser selection can change during a producer. Outbound authorization
        # belongs to its active root, including browser-origin follow-ups and
        # descendants. Direct trusted Python calls use the Harness execution root.
        active = self.state.active
        session_id = active.session_id if active is not None else self.state.harness.session_id
        owner = await self.store.owner(session_id)
        current = self.state.active
        current_id = current.session_id if current is not None else self.state.harness.session_id
        if current is not active or current_id != session_id:
            raise ChannelError("Execution identity changed before channel dispatch")
        if owner is not None and owner.conflicted:
            raise ChannelError("Session ownership is conflicted; channel dispatch is disabled")
        return owner

    @staticmethod
    def failure(error: Exception, operation: str) -> ChannelError:
        # Keep the core dispatch flags, but never trust a connector's exception
        # text to be free of private host-injected credentials.
        safe = ChannelError(
            f"Channel {operation} failed. No automatic retry.",
            retry_after=error.retry_after if isinstance(error, ChannelError) else 0,
            outcome_unknown=error.outcome_unknown if isinstance(error, ChannelError) else True,
        )
        return _failure(safe, operation)

    def check_result(self, result: dict[str, ChannelValue]) -> None:
        try:
            self.catalog.protection.check(result)
        except CredentialProtectionError:
            # Dispatch already happened. Withholding an unsafe success payload
            # must not imply it is safe to retry the remote operation.
            raise ChannelError("Channel result withheld by credential protection", outcome_unknown=True) from None

    async def open(self, id: str, channel: Channel) -> None:
        self.catalog.require_live()
        _bind(channel)
        try:
            async with asyncio.timeout(15):
                await channel.open()
            # open() may change descriptions/actions after factory validation.
            self.catalog.check_public(json.loads(_bind(channel).catalog))
        except BaseException:
            with suppress(Exception):
                async with asyncio.timeout(5):
                    await channel.close()
            raise
        self.channels[id] = channel
        self.catalog.status[id] = ("running", "")
        self.sources[id] = asyncio.create_task(self.source(id, channel), name=f"ngn-channel:{id}")

    async def source(self, id: str, channel: Channel) -> None:
        async def receive(message: ChannelMessage) -> None:
            try:
                approval = channel.approval(message)
            except Exception:
                approval = None  # Optional hook; a broken connector degrades to normal handling.
            if approval is not None:
                # A claimed approval interaction is never model input, even when it
                # no longer correlates with a live pending decision.
                await self.resolve_approval(id, approval)
                self.changed.set()
                await self.activities.refresh()
                return
            envelope = _envelope(id, message)
            command = channel.command(message)
            while not self.closed:
                try:
                    await self.store.receive(id, envelope, self.catalog.connections[id].main_session_id, command)
                    self.changed.set()
                    await self.activities.refresh()
                    return
                except HTTPException as error:
                    if error.status_code != 429:
                        raise ChannelError("Channel admission failed") from None
                    await asyncio.sleep(0.1)
            raise ChannelError("Channel host is stopping")

        try:
            await channel.listen(receive)
            self.catalog.status[id] = ("idle", "")
        except asyncio.CancelledError:
            raise
        except Exception:
            self.catalog.status[id] = (
                "error",
                "Connector stopped. Check configuration and reconnect at an idle boundary.",
            )

    async def resolve_approval(self, id: str, decision: ChannelApproval) -> bool:
        """Apply one in-chat approval decision to the live pending, or ignore it.

        The decision is honored only when it exactly matches this session's
        permanently owning chat, an enabled connector that opted into in-chat
        approvals, and the run's single live pending approval. Anything else is
        dropped without side effects.
        """
        state = self.state
        run = state.active
        if (
            run is None
            or run.finished
            or run.task.done()
            or run.task.cancelling()
            or not decision.conversation_id
            or not decision.call_id
            or decision.session_id != run.session_id
            or decision.run_id != run.id
        ):
            return False
        owner = await self.store.owner(run.session_id)
        connection = self.catalog.connections.get(id)
        channel = self.channels.get(id)
        source = self.sources.get(id)
        pending = run.pending
        if (
            owner is None
            or owner.conflicted
            or owner.channel != id
            or owner.conversation_id != decision.conversation_id
            or connection is None
            or connection.enabled is not True
            or connection.chat_approvals is not True
            or self.catalog.connections.get(id) is not connection
            or not self.catalog.allow_plugins
            or self.state.harness.config.demo
            or channel is None
            or self.channels.get(id) is not channel
            or "approvals" not in channel.capabilities
            or source is None
            or self.sources.get(id) is not source
            or source.done()
            or source.cancelling()
        ):
            return False
        if (
            state.active is not run
            or run.pending is not pending
            or pending is None
            or pending.answer.done()
            or pending.call_id != decision.call_id
            or pending.record.get("run_id") != run.id
        ):
            return False
        pending.answer.set_result(decision.allow)
        return True

    async def in_chat_approvals(self, session_id: str) -> bool:
        """True when this session's owning chat may decide approvals without a browser."""
        try:
            owner = await self.store.owner(session_id)
        except Exception:
            return False
        if owner is None or owner.conflicted or not owner.channel or not owner.conversation_id:
            return False
        channel = self.channels.get(owner.channel)
        source = self.sources.get(owner.channel)
        connection = self.catalog.connections.get(owner.channel)
        return bool(
            connection is not None
            and connection.enabled is True
            and connection.chat_approvals is True
            and not self.closed
            and self.catalog.allow_plugins
            and not self.state.harness.config.demo
            and channel is not None
            and self.channels.get(owner.channel) is channel
            and "approvals" in channel.capabilities
            and source is not None
            and self.sources.get(owner.channel) is source
            and not source.done()
            and not source.cancelling()
        )

    async def stop_source(self, id: str) -> None:
        source = self.sources.pop(id, None)
        if source is not None:
            source.cancel()
            with suppress(asyncio.CancelledError):
                await _join(source)
        channel = self.channels.pop(id, None)
        if channel is not None:
            try:
                async with asyncio.timeout(5):
                    await channel.close()
            except Exception:
                self.catalog.status[id] = (
                    "error",
                    "Connector cleanup failed. Restart the server before enabling it again.",
                )

    async def save(self, id: str, body: ConnectionInput) -> dict[str, object]:
        self.catalog.require_live()
        roots = {session.id for session in await self.state.harness.list_sessions()}
        old = self.catalog.connections.get(id)
        main = body.main_session_id or (old.main_session_id if old else self.state.selected_session_id)
        if main not in roots:
            raise HTTPException(404, "Main session not found in this workspace.")
        connection = self.catalog.prepare(id, body, main)
        if self.runtime is not None:
            self.catalog.check_public(
                [json.loads(binding.catalog) for binding in self.runtime._bindings], additional=connection.secrets
            )
        channel = self.catalog.construct(id, connection) if connection.enabled else None
        if channel is not None:
            _bind(channel)
        # Commit configuration before changing live connectors. A failed open is
        # a persisted error state, not a silent rollback or lost accepted inbox.
        self.catalog.save({**self.catalog.connections, id: connection})
        await self.stop_source(id)
        if channel is not None:
            try:
                await self.open(id, channel)
            except Exception:
                self.catalog.status[id] = (
                    "error",
                    "Connector could not be started. Check configuration and installation.",
                )
        else:
            self.catalog.status[id] = ("disabled", "")
        self.update_tools()
        return await self.snapshot()

    async def delete(self, id: str, revision: str) -> dict[str, object]:
        self.catalog.require_live()
        self.catalog.check_revision(revision)
        if id not in self.catalog.connections:
            raise HTTPException(404, "Channel connection not found.")
        self.catalog.save({key: value for key, value in self.catalog.connections.items() if key != id})
        await self.stop_source(id)
        self.update_tools()
        return await self.snapshot()

    async def snapshot(self) -> dict[str, object]:
        return self.catalog.snapshot(await self.store.bindings())

    async def activity(self, session_id: str, active: bool, source: dict[str, str] | None = None) -> None:
        await self.activities.set(session_id, active, source or {})

    async def reply(self, work: Work, text: str) -> None:
        channel = self.channels.get(work.channel)
        if channel is None:
            raise ChannelError("Command source is unavailable")
        async with asyncio.timeout(15):
            await channel.send(ChannelSend(work.conversation_id, text, work.thread_id, work.reply_to))

    async def acknowledgement(self, work: Work) -> None:
        await self.reply(work, await self.store.acknowledgement(work))

    async def claim(self) -> Work | None:
        # Cancellation of RoutingStore.claim_work itself joins its transaction,
        # but discards the returned Work before raising. Own a shielded claim so
        # even cancellation racing commit can release the exact unstarted row.
        task = asyncio.create_task(
            self.store.claim_work(web_only=not self.catalog.allow_plugins, available_channels=tuple(self.channels))
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:

            async def release() -> None:
                work = await task
                if work is not None:
                    await self.store.release_work(work)

            await _join(asyncio.create_task(release()))
            raise

    async def worker(self) -> None:
        while not self.closed:
            self.changed.clear()
            if self.management.ready and self.state.active is None and not self.state.mutating:
                with self.state.idle():
                    await self.management.flush()
                continue
            pending = await self.store.has_pending(
                web_only=not self.catalog.allow_plugins, available_channels=tuple(self.channels)
            )
            if pending and not self.closed and self.state.active is None and not self.state.mutating:
                with self.state.idle():
                    work = await self.claim()
                    if work is not None:
                        # Shutdown may have started while SQLite was claiming.
                        # Model work rechecks after its async owner validation in
                        # execute_work; accepted acknowledgements remain bounded.
                        if self.closed:
                            await self.store.release_work(work)
                            return
                        status = "failed"
                        try:
                            if work.command == "compact":
                                status = await self.state.compact_work(work)
                            elif work.acknowledgement:
                                await self.acknowledgement(work)
                                status = "completed"
                            else:
                                status = await self.state.execute_work(work)
                        except asyncio.CancelledError:
                            status = "interrupted"
                            raise
                        except Exception:
                            pass
                        finally:
                            await self.store.finish_work(work, status)
                        continue
            with suppress(TimeoutError):
                # Python 3.11 wait_for can swallow owner cancellation when its
                # Event.wait child finishes concurrently. Wait in this task so
                # cancellation always escapes, retaining the same idle deadline.
                async with asyncio.timeout(0.25):
                    await self.changed.wait()

    async def poll(self) -> None:
        previous: dict[str, str] = {}
        while not self.closed:
            try:
                sessions = await self.state.list_sessions()
                catalog = json.dumps([session.__dict__ for session in sessions], sort_keys=True)
                if previous.get("") != catalog:
                    previous[""] = catalog
                    self.state.bus.publish({"type": "sessions", "sessions": json.loads(catalog)})
                watching = {sub.session_id for sub in self.state.bus.subscribers if sub.ready or sub.hydrating}
                for session_id in set(previous) - watching - {""}:
                    del previous[session_id]
                for session_id in watching:
                    try:
                        snapshot = await self.state.snapshot(session_id)
                    except HTTPException as error:
                        if error.status_code in {403, 404}:
                            previous.pop(session_id, None)
                            self.state.bus.invalidate_session(session_id)
                        continue
                    except Exception:
                        # One bad root must not suppress another root's updates.
                        # Transient failures retry without revoking subscriptions.
                        continue
                    history = json.dumps(snapshot["history"], sort_keys=True)
                    if previous.get(session_id) != history:
                        previous[session_id] = history
                        self.state.bus.publish({"type": "snapshot", "session_id": session_id, "snapshot": snapshot})
            except Exception:
                # DB contention can be retried; plugin imports/network are never polled here.
                pass
            await asyncio.sleep(1)

    async def close(self) -> None:
        self.closed = True
        self.management.shutdown()
        self.changed.set()
        try:
            await _join(asyncio.create_task(self._close()))
        finally:
            if self.owned:
                with _HOST_LOCK:
                    _HOSTS.discard(self.store.db_path.resolve())
                    self.owned = False

    async def _close(self) -> None:
        self.closed = True
        self.management.shutdown()
        self.changed.set()
        # Stop the producer before joining its owner: execute_work deliberately
        # shields the model task from owner cancellation. Pending claims finish
        # gracefully and observe closed before execution, returning to queued.
        if self.state.active is not None:
            await self.state.stop(self.state.active)
        for task in self.tasks[1:]:
            task.cancel()
        # Join a bounded command send or accepted configuration apply before
        # closing any connector that the worker may still be using.
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.activities.close()
        for id in tuple(self.sources):
            await self.stop_source(id)
        if self.initialized:
            await self.store.interrupt_work()
        registry = self.state.harness.agent.tool_registry
        for tool in self.tools:
            if registry.get(tool.name) is tool:
                registry.unregister(tool.name)
        plugins = self.state.harness.agent.plugins
        plugins[:] = [plugin for plugin in plugins if plugin is not self.instructions and plugin is not self.management]
        self.runtime = None
