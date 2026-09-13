"""Lifecycle-owned connectors, normal executor tools, and the serialized durable worker."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from threading import Lock
from typing import TYPE_CHECKING

from fastapi import HTTPException

from nagents.channels.runtime import ChannelRuntime
from nagents.channels.runtime import _Instructions
from nagents.channels.runtime import _bind
from nagents.channels.runtime import _envelope
from nagents.channels.runtime import _failure
from nagents.channels.types import ChannelError
from nagents.channels.types import ChannelSend
from nagents.channels.types import ChannelValue

from .catalog import ChannelCatalog
from .channel_activity import ChannelActivities
from .channel_privacy import CredentialGuard
from .channel_privacy import CredentialProtectionError
from .routing import RoutingStore
from .settings import _join

if TYPE_CHECKING:
    from pathlib import Path

    from nagents.channels.types import Channel
    from nagents.channels.types import ChannelMessage
    from nagents.extensions import ModelRequest
    from nagents.extensions import RunContext
    from nagents.types import ToolDefinition

    from .catalog import ConnectionInput
    from .routing import Work
    from .service import WebState

_HOSTS: set[Path] = set()
_HOST_LOCK = Lock()


class _ProtectedInstructions(_Instructions):
    def __init__(self, protection: CredentialGuard) -> None:
        super().__init__("[]")
        self.protection = protection
        self.catalog: list[dict[str, ChannelValue]] = []

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
        self.text = _Instructions(json.dumps(self.catalog)).text
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
        self.instructions = _ProtectedInstructions(self.catalog.protection)

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

    def update_tools(self) -> None:
        # Reuse core catalog/schema/dispatch validation, without Agent.listen's
        # task-ownership guard: Harness owns a separate child producer task.
        # Clear the previously published view before constructing/checking a new
        # one, so a failed refresh cannot leave stale sensitive instructions.
        self.runtime = None
        self.instructions.update([])
        runtime = (
            ChannelRuntime(self.state.harness.agent, tuple(self.channels.values()), self.state.selected_session_id)
            if self.channels
            else None
        )
        catalog = [json.loads(binding.catalog) for binding in runtime._bindings] if runtime is not None else []
        self.catalog.check_public(catalog)
        self.instructions.update(catalog)
        if runtime is not None:
            runtime._active = True
        self.runtime = runtime

    async def channel_list(self) -> list[dict[str, ChannelValue]]:
        """Discover connected channels and their action schemas."""
        try:
            result = await self.runtime._channel_list() if self.runtime is not None else []
            self.catalog.protection.check(result)
            return result
        except CredentialProtectionError:
            raise self.failure(ChannelError("Channel catalog withheld by credential protection"), "list") from None
        except Exception as error:
            raise self.failure(error, "list") from None

    async def channel_send(
        self, channel: str, destination: str, text: str, thread_id: str = "", reply_to: str = ""
    ) -> dict[str, ChannelValue]:
        """Send once to an explicit channel and destination. Never use an ingress ID as reply_to."""
        try:
            if self.runtime is None:
                raise ChannelError("No connected channels")
            result = await self.runtime._channel_send(channel, destination, text, thread_id, reply_to)
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
            result = await self.runtime._channel_action(channel, action, arguments)
            self.check_result(result)
            return result
        except Exception as error:
            raise self.failure(error, "action") from None

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

    async def acknowledgement(self, work: Work) -> None:
        channel = self.channels.get(work.channel)
        if channel is None:
            raise ChannelError("Command source is unavailable")
        async with asyncio.timeout(15):
            await channel.send(ChannelSend(work.conversation_id, work.acknowledgement, work.thread_id, work.reply_to))

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
            pending = await self.store.has_pending(
                web_only=not self.catalog.allow_plugins, available_channels=tuple(self.channels)
            )
            if pending and not self.closed and self.state.active is None and not self.state.mutating:
                with self.state.idle():
                    work = await self.claim()
                    if work is not None:
                        # Shutdown may have started while SQLite was claiming.
                        # There is no await between this guard and run ownership
                        # (or acknowledgement dispatch); unstarted work survives.
                        if self.closed:
                            await self.store.release_work(work)
                            return
                        status = "failed"
                        try:
                            if work.acknowledgement:
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
                await asyncio.wait_for(self.changed.wait(), 0.25)

    async def poll(self) -> None:
        previous: dict[str, str] = {}
        while not self.closed:
            try:
                sessions = await self.state.harness.list_sessions()
                catalog = json.dumps([session.__dict__ for session in sessions], sort_keys=True)
                if previous.get("") != catalog:
                    previous[""] = catalog
                    self.state.bus.publish({"type": "sessions", "sessions": json.loads(catalog)})
                for session_id in {sub.session_id for sub in self.state.bus.subscribers if sub.ready or sub.hydrating}:
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
        self.changed.set()
        # Stop the producer before joining its owner: execute_work deliberately
        # shields the model task from owner cancellation. Pending claims finish
        # gracefully and observe closed before execution, returning to queued.
        if self.state.active is not None:
            await self.state.stop(self.state.active)
        for task in self.tasks[1:]:
            task.cancel()
        # The worker's only other external operation is a bounded command send.
        # Join it before closing any connector that it may still be using.
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
        plugins[:] = [plugin for plugin in plugins if plugin is not self.instructions]
        self.runtime = None
