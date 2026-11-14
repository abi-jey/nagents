"""Harness-owned shell execution, durable notices, and host wake admission."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from nagents.observation import scope as observation_scope
from nagents.session.shell_jobs import ShellJobOwner
from nagents.session.shell_jobs import ShellJobRecord
from nagents.session.shell_jobs import ShellJobStore
from nagents.session.shell_jobs import ShellJobTerminal
from nagents.tools.shell import ShellController
from nagents.tools.shell import ShellJob
from nagents.tools.shell import ShellOwner
from nagents.tools.shell import ShellPolicy
from nagents.tools.shell import ShellStorage
from nagents.tools.shell import ShellSubscription

from ..types import ToolOutput

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable

    from nagents.types import JsonValue

    from ..runtime.core import Harness
    from ..runtime.followups import RunInput


class _SharedShell:
    def __init__(self, policy: ShellPolicy, root: Path) -> None:
        self.bridges: set[ShellJobs] = set()
        self.root = root
        self.lease_file = root / ".harness-hosts.json"
        self.stale_hosts: tuple[str, ...] = ()
        self.owners: dict[str, ShellJobs] = {}
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

    async def started(self, job: ShellJob) -> None:
        await self.owners[job.owner.run_id]._started(job)

    async def terminal(self, job: ShellJob) -> None:
        await self.owners[job.owner.run_id]._terminal(job)


_shared: dict[tuple[asyncio.AbstractEventLoop, Path], _SharedShell] = {}


class ShellJobs:
    """One host's process lifetime, independent of a foreground Agent iterator."""

    def __init__(self, harness: Harness) -> None:
        self.harness = harness
        self.host_id = uuid.uuid4().hex
        self.run_id = ""
        self.on_work_pending: Callable[[str], Awaitable[None]] | None = None
        self._store: ShellJobStore | None = None
        self._controller: ShellController | None = None
        self._automatic_turns = 0
        self._changed = asyncio.Event()
        self._shared: _SharedShell | None = None
        self._stores: dict[str, ShellJobStore] = {}
        self._retrying: set[str] = set()

    @property
    def store(self) -> ShellJobStore:
        if self._store is None or self._store.session is not self.harness.agent.session:
            self._store = ShellJobStore(self.harness.agent.session)
        return self._store

    def _policy(self) -> ShellPolicy:
        config = self.harness.config
        return ShellPolicy(
            allow_background=config.allow_background_shell,
            timeout=config.shell_timeout,
            max_output=config.max_output,
            max_log_bytes=config.shell_max_log_bytes,
            max_total_log_bytes=config.shell_max_total_log_bytes,
            max_concurrent=config.shell_max_concurrent,
            max_concurrent_per_owner=config.shell_max_concurrent_per_owner,
        )

    @property
    def controller(self) -> ShellController:
        if self._controller is None:
            key = (asyncio.get_running_loop(), (self.harness.agent.session.db_path.parent / "shell-jobs").resolve())
            shared = _shared.get(key)
            if shared is None:
                shared = _SharedShell(self._policy(), key[1])
                _shared[key] = shared
            shared.bridges.add(self)
            shared.save_hosts()
            self._shared = shared
            self._controller = shared.controller
        return self._controller

    def _session_store(self, session_id: str) -> ShellJobStore:
        return self._stores.get(session_id, self.store)

    async def _started(self, job: ShellJob) -> None:
        if not job.background:
            return
        await self._session_store(job.owner.session_id).register(
            ShellJobRecord(
                job.job_id,
                ShellJobOwner(
                    str(job.owner.workspace),
                    job.owner.root_id,
                    job.owner.session_id,
                    job.owner.run_id,
                    job.owner.tool_call_id,
                    self.host_id,
                ),
                job.command,
                str(job.output_path or ""),
                job.retention,
            )
        )

    async def _terminal(self, job: ShellJob) -> None:
        if not job.background:
            return
        await self._session_store(job.owner.session_id).terminal(
            job.owner.session_id,
            job.job_id,
            ShellJobTerminal(
                job.state,
                job.reason,
                job.output,
                job.exit_code if job.exit_code is not None else -1,
                job.timed_out,
                job.truncated,
                job.bytes_written,
                job.output_complete,
            ),
            wake=job.state != "cancelled" and not self.harness._closed,
        )
        if self.harness._worker is None:
            await self.process_notifications(job.owner.session_id)
        self.harness.followups.changed()
        self._changed.set()
        if job.state != "cancelled" and self.on_work_pending is not None and not self.harness._closed:
            try:
                await self.on_work_pending(job.owner.session_id)
            except Exception:
                logging.getLogger(__name__).exception("Shell host wake callback failed after durable completion")

    async def execute(self, command: str, timeout: float = 0.0, background: bool = False) -> dict[str, JsonValue]:
        h = self.harness
        h.tools.writable()
        policy = self._policy()
        if background and not policy.allow_background:
            raise PermissionError("Background shell execution is disabled")
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
        if background and not h.config.allow_background_shell:
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
        job = await controller.execute(
            command, owner=owner, timeout=seconds, background=background, on_output=None if background else output
        )
        if not background:
            return {
                "output": job.output,
                "exit_code": job.exit_code,
                "timed_out": job.timed_out,
                "truncated": job.truncated,
            }
        return {
            "job_id": job.job_id,
            "state": job.state,
            "background": True,
            "output_path": str(job.output_path or ""),
            "retention": job.retention,
            "output_available": job.output_available,
            "message": "You will receive a notification once the shell completes.",
        }

    def _snapshot(self, record: ShellJobRecord) -> dict[str, JsonValue]:
        terminal = json.loads(record.terminal_json) if record.terminal_json else {}
        live = (
            self._controller.get(record.job_id)
            if self._controller is not None and record.owner.host_id == self.host_id
            else None
        )
        return {
            "job_id": record.job_id,
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
        return [self._snapshot(record) for record in await self.store.list_jobs(session_id or self.harness.session_id)]

    async def get(self, job_id: str, session_id: str = "") -> dict[str, JsonValue]:
        await self.harness.initialize()
        return self._snapshot(await self.store.get(session_id or self.harness.session_id, job_id))

    async def stop(self, job_id: str, session_id: str = "", reason: str = "user_stop") -> dict[str, JsonValue]:
        session = session_id or self.harness.session_id
        await self.harness.initialize()
        record = await self.store.get(session, job_id)
        if record.owner.host_id != self.host_id and not record.terminal_json:
            raise ValueError("Shell job belongs to another host")
        if await self.store.request_cancel(session, job_id, reason):
            await self.controller.stop(job_id, reason)
        await self.process_notifications(session)
        return self._snapshot(await self.store.get(session, job_id))

    async def has_pending(self) -> bool:
        # Lifecycle inspection must not create a selected/new conversation.
        if self._controller is None:
            return False
        await self._recover()
        if self._automatic_turns >= 16:
            await self.store.suppress_wakes(self.harness.session_id)
        return any(
            not record.terminal_json or record.wake_state == "pending"
            for record in await self.store.list_jobs(self.harness.session_id)
        )

    async def wake_pending(self) -> bool:
        if self._controller is None or not self._stores:
            return False
        return self._automatic_turns < 16 and any(
            record.wake_state == "pending" for record in await self.store.list_jobs(self.harness.session_id)
        )

    async def wait_changed(self) -> None:
        await self._changed.wait()
        self._changed.clear()

    def subscribe(self) -> ShellSubscription:
        return self.controller.subscribe()

    async def process_notifications(self, session_id: str = "") -> None:
        session = session_id or self.harness.session_id
        if self._controller is not None and self._shared is not None:
            for job in self._controller.list():
                if (
                    job.background
                    and job.owner.session_id == session
                    and job.terminal
                    and self._shared.owners.get(job.owner.run_id) is self
                    and job.job_id not in self._retrying
                    and self._controller.terminal_error(job.job_id)
                ):
                    self._retrying.add(job.job_id)
                    try:
                        await self._controller.retry_terminal(job.job_id)
                    finally:
                        self._retrying.discard(job.job_id)
        for record in await self._session_store(session).list_jobs(session):
            if not record.receipt_committed:
                await self._session_store(session).mark_receipt_committed(session, record.owner.tool_call_id)
            if record.terminal_json and not record.notice_message_id:
                await self._session_store(session).claim_notice(session, record.job_id)

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
        return RunInput("Shell job notification", kind="notification", persisted_notice_id=message_id)

    async def cancel_owner(self, session_id: str, reason: str = "owner_cancelled") -> None:
        await self._session_store(session_id).suppress_wakes(session_id)
        # A boundary may claim a notice before its queued turn is dispatched.
        # Reconcile the journal rather than relying on the current RunInput.
        for record in await self._session_store(session_id).list_jobs(session_id):
            if record.owner.host_id == self.host_id and record.wake_state == "claimed":
                await self._session_store(session_id).finish_wake(session_id, record.notice_message_id, uncertain=True)
        if self._controller is None:
            return
        for job in self._controller.list():
            if (
                job.background
                and job.owner.session_id == session_id
                and not job.terminal
                and self._shared is not None
                and self._shared.owners.get(job.owner.run_id) is self
            ):
                await self._session_store(session_id).request_cancel(session_id, job.job_id, reason)
                await self._controller.stop(job.job_id, reason)
        await self.process_notifications(session_id)

    async def close(self) -> None:
        if self._controller is None or self._shared is None:
            return
        sessions = {
            job.owner.session_id
            for job in self._controller.list()
            if job.background and self._shared.owners.get(job.owner.run_id) is self
        }
        for session in sessions:
            await self.cancel_owner(session, "host_shutdown")
        shared = self._shared
        shared.bridges.discard(self)
        shared.save_hosts()
        if not shared.bridges:
            try:
                await shared.controller.close()
            finally:
                for key, value in tuple(_shared.items()):
                    if value is shared:
                        del _shared[key]
        for session in sessions:
            await self.process_notifications(session)
