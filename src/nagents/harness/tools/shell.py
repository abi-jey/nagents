"""Harness-owned shell execution, durable notices, and host wake admission."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import uuid
from contextlib import asynccontextmanager
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

from nagents._async import join_owned
from nagents.observation import scope as observation_scope
from nagents.session.shells import ShellRecord
from nagents.session.shells import ShellRecordOwner
from nagents.session.shells import ShellStore
from nagents.session.shells import ShellTerminal
from nagents.tools.shell import ShellController
from nagents.tools.shell import ShellInstance
from nagents.tools.shell import ShellOwner
from nagents.tools.shell import ShellPolicy
from nagents.tools.shell import ShellStorage
from nagents.tools.shell import ShellSubscription

from ..types import ToolOutput

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from collections.abc import Awaitable
    from collections.abc import Callable

    from nagents.types import JsonValue

    from ..runtime.core import Harness
    from ..runtime.followups import RunInput


class _SharedShell:
    def __init__(self, policy: ShellPolicy, root: Path) -> None:
        self.bridges: set[Shells] = set()
        self.root = root
        self.lease_file = root / ".harness-hosts.json"
        self.stale_hosts: tuple[str, ...] = ()
        self.owners: dict[str, Shells] = {}
        self.controller = ShellController(
            policy,
            ShellStorage(root, persistent=True),
            on_started=self.started,
            on_terminal=self.terminal,
        )
        if self.lease_file.exists():
            fd = os.open(self.lease_file, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "r") as handle:
                saved = json.load(handle)
            if isinstance(saved, list) and all(isinstance(host, str) for host in saved):
                self.stale_hosts = tuple(saved)

    def save_hosts(self) -> None:
        path = self.root / f".harness-hosts-{uuid.uuid4().hex}.tmp"
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(sorted({*self.stale_hosts, *(bridge.host_id for bridge in self.bridges)}), handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(path, self.lease_file)

    async def started(self, shell: ShellInstance) -> None:
        await self.owners[shell.owner.run_id]._started(shell)

    async def terminal(self, shell: ShellInstance) -> None:
        await self.owners[shell.owner.run_id]._terminal(shell)


_shared: dict[tuple[asyncio.AbstractEventLoop, Path], _SharedShell] = {}


class Shells:
    """One host's process lifetime, independent of a foreground Agent iterator."""

    def __init__(self, harness: Harness) -> None:
        self.harness = harness
        self.host_id = uuid.uuid4().hex
        self.run_id = ""
        self.on_work_pending: Callable[[str], Awaitable[None]] | None = None
        self._store: ShellStore | None = None
        self._controller: ShellController | None = None
        self._automatic_turns = 0
        self._changed = asyncio.Event()
        self._shared: _SharedShell | None = None
        self._stores: dict[str, ShellStore] = {}
        self._retrying: set[str] = set()
        self._terminal_retries: dict[str, asyncio.Task[None]] = {}
        self._closing = False

    @property
    def store(self) -> ShellStore:
        if self._store is None or self._store.session is not self.harness.agent.session:
            self._store = ShellStore(self.harness.agent.session)
        return self._store

    @asynccontextmanager
    async def notice_admission(self, *, enabled: bool) -> AsyncIterator[None]:
        if not enabled:
            yield
            return
        # Snapshot authority before claiming: cancellation can arrive after the
        # SQLite commit but before claim_wake returns its message identifier.
        session_id, store, host_id = self.harness.session_id, self.store, self.host_id
        try:
            yield
        except BaseException:

            async def reconcile() -> None:
                for record in await store.list_shells(session_id):
                    if record.owner.host_id == host_id and record.wake_state == "claimed":
                        await store.finish_wake(session_id, record.notice_message_id, uncertain=True)

            await join_owned(asyncio.create_task(reconcile()))
            raise

    def _policy(self) -> ShellPolicy:
        config = self.harness.config
        return ShellPolicy(
            allow_background=config.shell.allow_background and not self.harness._is_subagent,
            timeout=config.shell_timeout,
            max_output=config.max_output,
            max_log_bytes=config.shell.max_log_bytes,
            max_total_log_bytes=config.shell.max_total_log_bytes,
            max_concurrent=config.shell.max_concurrent,
            max_concurrent_per_owner=config.shell.max_concurrent_per_owner,
        )

    @property
    def controller(self) -> ShellController:
        if self._controller is None:
            key = (asyncio.get_running_loop(), (self.harness.agent.session.db_path.parent / "shells").resolve())
            shared = _shared.get(key)
            if shared is None:
                shared = _SharedShell(self._policy(), key[1])
                _shared[key] = shared
            shared.bridges.add(self)
            shared.save_hosts()
            self._shared = shared
            self._controller = shared.controller
        return self._controller

    def _session_store(self, session_id: str) -> ShellStore:
        return self._stores.get(session_id, self.store)

    async def _started(self, shell: ShellInstance) -> None:
        if not shell.background:
            return
        await self._session_store(shell.owner.session_id).register(
            ShellRecord(
                shell.shell_id,
                ShellRecordOwner(
                    str(shell.owner.workspace),
                    shell.owner.root_id,
                    shell.owner.session_id,
                    shell.owner.run_id,
                    shell.owner.tool_call_id,
                    self.host_id,
                ),
                shell.command,
                str(shell.output_path or ""),
                shell.retention,
            )
        )

    async def _retry_terminal(self, shell_id: str) -> None:
        assert self._controller is not None
        # Retry independently of run-scoped consumers and idle host callbacks.
        for delay in (0.01, 0.05, 0.2, 0.5, 1.0):
            await asyncio.sleep(delay)
            self._retrying.add(shell_id)
            try:
                await self._controller.retry_terminal(shell_id)
            except Exception:
                continue
            finally:
                self._retrying.discard(shell_id)
            return
        logging.getLogger(__name__).error("Shell terminal persistence retries exhausted: %s", shell_id)
        self._changed.set()

    async def _terminal(self, shell: ShellInstance) -> None:
        try:
            await self._persist_terminal(shell)
        except Exception:
            if shell.shell_id not in self._terminal_retries and not self._closing:
                self._terminal_retries[shell.shell_id] = asyncio.create_task(
                    self._retry_terminal(shell.shell_id), name=f"shell-persist-{shell.shell_id}"
                )
            raise

    async def _persist_terminal(self, shell: ShellInstance) -> None:
        if not shell.background:
            return
        await self._session_store(shell.owner.session_id).terminal(
            shell.owner.session_id,
            shell.shell_id,
            ShellTerminal(
                shell.state,
                shell.reason,
                shell.output,
                shell.exit_code if shell.exit_code is not None else -1,
                shell.timed_out,
                shell.truncated,
                shell.bytes_written,
                shell.output_complete,
            ),
            wake=shell.state != "cancelled" and not self.harness._closed,
        )
        if self.harness._worker is None:
            await self.process_notifications(shell.owner.session_id)
        self.harness.followups.changed()
        self._changed.set()
        if shell.state != "cancelled" and self.on_work_pending is not None and not self.harness._closed:
            try:
                await self.on_work_pending(shell.owner.session_id)
            except Exception:
                logging.getLogger(__name__).exception("Shell host wake callback failed after durable completion")

    async def execute(self, command: str, timeout: float = 0.0, background: bool = False) -> dict[str, JsonValue]:
        h = self.harness
        policy = self._policy()
        if background and not policy.allow_background:
            raise PermissionError("Background shell execution is disabled")
        h.tools.writable()
        seconds = timeout or policy.timeout
        if not math.isfinite(seconds) or not 0 < seconds <= policy.timeout:
            raise ValueError(
                f"timeout must be > 0 and <= {policy.timeout}, or 0 for the default. No command was started."
            )
        if not command.strip() or len(command) > 16384 or "\x00" in command:
            raise ValueError("command must be nonempty, without NUL, and <= 16384 characters")
        call_id = h.tools.call_id.get()
        await h.approve(
            "shell",
            {"command": command, "timeout": seconds, "background": background},
            "LOCAL SHELL IS NOT SANDBOXED: may access the network, credentials, and files outside the workspace.",
            f"cwd: {h.workspace}\ntimeout: {seconds}s\nbackground: {background}\n\n{command}",
            call_id,
        )
        h.tools.writable()
        if background and (not h.config.shell.allow_background or h._is_subagent):
            raise PermissionError("Background shell execution is disabled")
        owner = ShellOwner(
            h.workspace,
            h.session_id,
            self.run_id or uuid.uuid4().hex,
            call_id,
            h.config.agent,
            h.config.provider,
            h.config.model,
            h.tasks.root.harness.session_id,
        )

        invocation = observation_scope.get()
        generation, index = invocation.get("tool_generation_id"), invocation.get("tool_index")
        extra: dict[str, object] = {}
        if (
            invocation.get("tool_call_id") == call_id
            and isinstance(generation, str)
            and generation
            and type(index) is int
            and 0 <= index < 1024
        ):
            extra = {"generation_id": generation, "index": index}

        async def output(text: str) -> None:
            await h.emit(ToolOutput(call_id, "shell", text, extra=extra))

        controller = self.controller
        assert self._shared is not None
        await self._recover()
        self._shared.owners[owner.run_id] = self
        self._stores[owner.session_id] = self.store
        controller.policy = policy
        shell = await controller.execute(
            command, owner=owner, timeout=seconds, background=background, on_output=None if background else output
        )
        if not background:
            if shell.state in {"output_limit", "storage_failure"}:
                raise RuntimeError(f"Shell stopped: {shell.reason}; output is incomplete.")
            return {
                "output": shell.output,
                "exit_code": shell.exit_code,
                "timed_out": shell.timed_out,
                "truncated": shell.truncated,
            }
        return {
            "shell_id": shell.shell_id,
            "state": shell.state,
            "background": True,
            "output_path": str(shell.output_path or ""),
            "retention": shell.retention,
            "output_available": shell.output_available,
            "message": "You will receive a notification once the shell completes.",
        }

    def _snapshot(self, record: ShellRecord) -> dict[str, JsonValue]:
        terminal = json.loads(record.terminal_json) if record.terminal_json else {}
        live = (
            self._controller.get(record.shell_id)
            if self._controller is not None and record.owner.host_id == self.host_id
            else None
        )
        return {
            "shell_id": record.shell_id,
            "state": live.state if live is not None else record.state,
            "command": record.command,
            "output_path": record.output_path,
            "output": terminal.get("output", ""),
            "reason": terminal.get("reason", ""),
            "bytes_written": live.bytes_written if live is not None else terminal.get("bytes_written", 0),
            "output_complete": terminal.get("output_complete", False),
            "wake_blocked_reason": "automatic activation budget exhausted"
            if self._automatic_turns >= 16 and record.wake_state == "suppressed"
            else "",
            "background": True,
            "session_id": record.owner.session_id,
            "retention": record.retention,
            "output_available": bool(record.output_path) and Path(record.output_path).is_file(),
        }

    async def _recover(self) -> None:
        if self._shared is None:
            return
        # The exclusive controller lease proves only these prior lease hosts dead.
        for host_id in self._shared.stale_hosts:
            await self.store.reconcile_orphans(host_id)

    async def list(self, session_id: str = "") -> list[dict[str, JsonValue]]:
        await self.harness.initialize()
        _ = self.controller
        await self._recover()
        return [
            self._snapshot(record) for record in await self.store.list_shells(session_id or self.harness.session_id)
        ]

    async def get(self, shell_id: str, session_id: str = "") -> dict[str, JsonValue]:
        await self.harness.initialize()
        return self._snapshot(await self.store.get(session_id or self.harness.session_id, shell_id))

    async def stop(self, shell_id: str, session_id: str = "", reason: str = "user_stop") -> dict[str, JsonValue]:
        session = session_id or self.harness.session_id
        await self.harness.initialize()
        record = await self.store.get(session, shell_id)
        if record.owner.host_id != self.host_id and not record.terminal_json:
            raise ValueError("Shell belongs to another host")
        if await self.store.request_cancel(session, shell_id, reason):
            await self.controller.stop(shell_id, reason)
        await self.process_notifications(session)
        return self._snapshot(await self.store.get(session, shell_id))

    async def has_pending(self) -> bool:
        # Lifecycle inspection must not create a selected/new conversation.
        if self._controller is None:
            return False
        await self._recover()
        if self._automatic_turns >= 16:
            await self.store.suppress_wakes(self.harness.session_id)
        return any(
            not record.terminal_json or record.wake_state == "pending"
            for record in await self.store.list_shells(self.harness.session_id)
        )

    async def wake_pending(self) -> bool:
        if self._controller is None or not self._stores:
            return False
        return self._automatic_turns < 16 and any(
            record.wake_state == "pending" for record in await self.store.list_shells(self.harness.session_id)
        )

    async def wait_changed(self) -> None:
        await self._changed.wait()
        self._changed.clear()

    def subscribe(self) -> ShellSubscription:
        return self.controller.subscribe()

    async def process_notifications(self, session_id: str = "") -> None:
        session = session_id or self.harness.session_id
        if self._controller is not None and self._shared is not None:
            for shell in self._controller.list():
                if (
                    shell.background
                    and shell.owner.session_id == session
                    and shell.terminal
                    and self._shared.owners.get(shell.owner.run_id) is self
                    and shell.shell_id not in self._retrying
                    and self._controller.terminal_error(shell.shell_id)
                ):
                    self._retrying.add(shell.shell_id)
                    try:
                        await self._controller.retry_terminal(shell.shell_id)
                    finally:
                        self._retrying.discard(shell.shell_id)
        for record in await self._session_store(session).list_shells(session):
            if not record.receipt_committed:
                await self._session_store(session).mark_receipt_committed(session, record.owner.tool_call_id)
            if record.terminal_json and not record.notice_message_id:
                await self._session_store(session).claim_notice(session, record.shell_id)

    async def next_input(self) -> RunInput | None:
        from ..runtime.followups import RunInput

        if self._controller is None or not self._stores:
            return None
        await self._recover()
        await self.process_notifications()
        if self._automatic_turns >= 16:
            await self.store.suppress_wakes(self.harness.session_id)
            return None
        if self.harness._closed or self.harness._remote_profile.configured:
            return None
        message_id = await self.store.claim_wake(self.harness.session_id)
        if not message_id:
            return None
        self._automatic_turns += 1
        return RunInput("Shell notification", kind="notification", persisted_notice_id=message_id)

    async def cancel_owner(self, session_id: str, reason: str = "owner_cancelled") -> None:
        await self._session_store(session_id).suppress_wakes(session_id)
        # A boundary may claim a notice before its queued turn is dispatched.
        # Reconcile the journal rather than relying on the current RunInput.
        for record in await self._session_store(session_id).list_shells(session_id):
            if record.owner.host_id == self.host_id and record.wake_state == "claimed":
                await self._session_store(session_id).finish_wake(session_id, record.notice_message_id, uncertain=True)
        if self._controller is None:
            return
        for shell in self._controller.list():
            if (
                shell.background
                and shell.owner.session_id == session_id
                and not shell.terminal
                and self._shared is not None
                and self._shared.owners.get(shell.owner.run_id) is self
            ):
                await self._session_store(session_id).request_cancel(session_id, shell.shell_id, reason)
                await self._controller.stop(shell.shell_id, reason)
        await self.process_notifications(session_id)

    async def close(self) -> None:
        if self._controller is None or self._shared is None:
            return
        self._closing = True
        shared = self._shared
        sessions = {
            shell.owner.session_id
            for shell in self._controller.list()
            if shell.background and shared.owners.get(shell.owner.run_id) is self
        }
        try:
            for session in sessions:
                await self.cancel_owner(session, "host_shutdown")
            for task in self._terminal_retries.values():
                await asyncio.shield(task)
            for session in sessions:
                await self.process_notifications(session)
        finally:
            # Even journal cleanup failure must reap owned processes before
            # releasing callback routing and the bridge's strong references.
            await asyncio.gather(
                *(
                    self._controller.stop(shell.shell_id, "host_shutdown")
                    for shell in self._controller.list()
                    if shared.owners.get(shell.owner.run_id) is self and not shell.terminal
                ),
                return_exceptions=True,
            )
            for task in self._terminal_retries.values():
                if not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
            for run_id, bridge in tuple(shared.owners.items()):
                if bridge is self:
                    del shared.owners[run_id]
            self._terminal_retries.clear()
            shared.bridges.discard(self)
            shared.save_hosts()
            if not shared.bridges:
                try:
                    await shared.controller.close()
                finally:
                    for key, value in tuple(_shared.items()):
                        if value is shared:
                            del _shared[key]
