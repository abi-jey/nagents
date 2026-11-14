"""Explicitly owned, non-sandboxed POSIX shell processes."""

from __future__ import annotations

import asyncio
import codecs
import contextvars
import math
import os
import signal
import tempfile
import uuid
from contextlib import suppress
from dataclasses import dataclass
from dataclasses import replace
from datetime import UTC
from datetime import datetime
from pathlib import Path

from nagents._async import finish_on_cancel
from nagents._async import join_owned

from .storage import read_tail
from .types import ShellCallback
from .types import ShellEvent
from .types import ShellJob
from .types import ShellJobCallback
from .types import ShellOutputCallback
from .types import ShellOwner
from .types import ShellPolicy
from .types import ShellState
from .types import ShellStorage


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _same_owner(left: ShellOwner, right: ShellOwner) -> bool:
    return (left.workspace, left.root_id, left.session_id) == (right.workspace, right.root_id, right.session_id)


class ShellSubscription:
    """Lossy activity stream; reconcile terminal state using controller.list()."""

    def __init__(self, size: int, callback: ShellCallback | None = None) -> None:
        self.queue: asyncio.Queue[ShellEvent | None] = asyncio.Queue(size)
        self.closed = False
        self.task = asyncio.create_task(self._consume(callback), context=contextvars.Context()) if callback else None

    def publish(self, event: ShellEvent) -> None:
        if self.closed:
            return
        if self.queue.full():
            self.queue.get_nowait()
        self.queue.put_nowait(event)

    def __aiter__(self) -> ShellSubscription:
        return self

    async def __anext__(self) -> ShellEvent:
        event = await self.queue.get()
        if event is None:
            raise StopAsyncIteration
        return event

    async def _consume(self, callback: ShellCallback) -> None:
        async for event in self:
            try:
                await callback(event)
            except Exception:
                # Subscriber failures cannot strand a process or pipe.
                continue

    async def aclose(self) -> None:
        self.closed = True
        if self.queue.full():
            self.queue.get_nowait()
        self.queue.put_nowait(None)
        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task


@dataclass
class _Entry:
    job: ShellJob
    policy: ShellPolicy
    fd: int
    ready: asyncio.Future[None]
    task: asyncio.Task[None] | None = None
    process: asyncio.subprocess.Process | None = None
    stop_reason: str = ""
    terminal_error: Exception | None = None


_DEFAULT_POLICY = ShellPolicy()
_DEFAULT_STORAGE = ShellStorage()


class ShellController:
    def __init__(
        self,
        policy: ShellPolicy = _DEFAULT_POLICY,
        storage: ShellStorage = _DEFAULT_STORAGE,
        *,
        on_started: ShellJobCallback | None = None,
        on_terminal: ShellJobCallback | None = None,
    ) -> None:
        self.policy = policy
        self.storage = storage
        self.on_started = on_started
        self.on_terminal = on_terminal
        self._temporary = tempfile.TemporaryDirectory(prefix="nagents-shell-") if storage.root is None else None
        self._root = Path(self._temporary.name) if self._temporary else storage.root
        assert self._root is not None
        if self._root.is_symlink():
            raise ValueError("shell storage root must not be a symlink")
        self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self._root.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PermissionError("shell storage root must be private (0700) and owned by this user")
        if os.name != "posix":
            raise OSError("Shell process-group cleanup currently requires POSIX")
        import fcntl

        self._quota_fd = os.open(self._root / ".controller.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self._quota_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self._quota_fd)
            raise RuntimeError("shell storage root already has an active controller") from None
        self._entries: dict[str, _Entry] = {}
        self._subscriptions: list[ShellSubscription] = []
        self._closed = False
        self._bytes = sum(p.stat().st_size for p in self._root.glob("*/output.log") if not p.is_symlink())

    def subscribe(self, callback: ShellCallback | None = None) -> ShellSubscription:
        if self._closed:
            raise RuntimeError("shell controller is closed")
        subscription = ShellSubscription(self.policy.subscriber_queue_size, callback)
        self._subscriptions.append(subscription)
        return subscription

    def _publish(self, kind: str, entry: _Entry, text: str = "") -> None:
        event = ShellEvent(kind, entry.job, text)  # type: ignore[arg-type]
        for subscription in self._subscriptions:
            subscription.publish(event)

    def get(self, job_id: str) -> ShellJob:
        job = self._entries[job_id].job
        return replace(job, output_available=job.output_path is not None and job.output_path.is_file())

    def list(self, *, owner: ShellOwner | None = None) -> tuple[ShellJob, ...]:
        return tuple(
            self.get(key)
            for key, entry in self._entries.items()
            if owner is None or _same_owner(entry.job.owner, owner)
        )

    async def execute(
        self,
        command: str,
        *,
        owner: ShellOwner,
        timeout: float = 0.0,
        background: bool = False,
        on_output: ShellOutputCallback | None = None,
    ) -> ShellJob:
        if os.name != "posix":
            raise OSError("Shell process-group cleanup currently requires POSIX")
        if self._closed:
            raise RuntimeError("shell controller is closed")
        policy = self.policy
        if background and not policy.allow_background:
            raise PermissionError("background shell execution is disabled")
        if background and on_output is not None:
            raise ValueError("background output must use a nonblocking subscription")
        if not command.strip() or len(command) > 16384 or "\x00" in command:
            raise ValueError("command must be nonempty, without NUL, and <= 16384 characters")
        seconds = timeout or policy.timeout
        if not math.isfinite(seconds) or not 0 < seconds <= policy.timeout:
            raise ValueError(f"timeout must be > 0 and <= {policy.timeout}, or 0 for the default")
        active = [entry for entry in self._entries.values() if not entry.job.terminal]
        if (
            len(active) >= policy.max_concurrent
            or sum(_same_owner(e.job.owner, owner) for e in active) >= policy.max_concurrent_per_owner
        ):
            raise RuntimeError("shell concurrency limit reached")
        owner = replace(owner, workspace=owner.workspace.resolve())
        job_id = uuid.uuid4().hex
        assert self._root is not None
        directory = self._root / job_id
        directory.mkdir(mode=0o700)
        path = directory / "output.log"
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        job = ShellJob(
            job_id,
            owner,
            command,
            background,
            output_path=path,
            retention="persistent" if self.storage.persistent else "temporary",
            output_available=True,
            created_at=_now(),
        )
        entry = _Entry(job, policy, fd, asyncio.get_running_loop().create_future())
        self._entries[job_id] = entry
        # Cancellation can abandon the admission waiter, but not its exception.
        entry.ready.add_done_callback(lambda future: None if future.cancelled() else future.exception())
        entry.task = asyncio.create_task(self._worker(entry, seconds, on_output), context=contextvars.Context())
        try:
            await asyncio.shield(entry.ready)
            if background:
                return self.get(job_id)
            await asyncio.shield(entry.task)
            if entry.terminal_error is not None:
                raise entry.terminal_error
            return self.get(job_id)
        except asyncio.CancelledError:
            await finish_on_cancel(self.stop(job_id, reason="owner_cancelled"))
            raise

    async def _reap(self, entry: _Entry) -> None:
        process = entry.process
        if process is None:
            return
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        if process.stdout is not None:
            while await process.stdout.read(65536):
                pass
        await process.wait()

    async def _worker(self, entry: _Entry, seconds: float, on_output: ShellOutputCallback | None) -> None:
        state: ShellState = "completed"
        reason = "exited"
        preview = bytearray()
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            if self.on_started is not None:
                await self.on_started(entry.job)
            if entry.stop_reason:
                return
            entry.process = await asyncio.create_subprocess_exec(
                "/bin/sh",
                "-c",
                entry.job.command,
                cwd=entry.job.owner.workspace,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
            entry.job = replace(entry.job, state="running", started_at=_now())
            if entry.stop_reason:
                state, reason = "cancelled", entry.stop_reason
            else:
                self._publish("started", entry)
                entry.ready.set_result(None)
                assert entry.process.stdout is not None
                async with asyncio.timeout(seconds):
                    while chunk := await entry.process.stdout.read(65536):
                        if entry.stop_reason:
                            state, reason = "cancelled", entry.stop_reason
                            break
                        allowed = min(
                            len(chunk),
                            entry.policy.max_log_bytes - entry.job.bytes_written,
                            entry.policy.max_total_log_bytes - self._bytes,
                        )
                        accepted = chunk[: max(0, allowed)]
                        self._bytes += len(accepted)
                        await join_owned(asyncio.create_task(asyncio.to_thread(self._write, entry.fd, accepted)))
                        old_size = len(preview)
                        preview.extend(chunk[: max(0, entry.policy.max_output - old_size)])
                        entry.job = replace(
                            entry.job,
                            bytes_written=entry.job.bytes_written + len(accepted),
                            truncated=entry.job.truncated or len(chunk) > entry.policy.max_output - old_size,
                        )
                        text = decoder.decode(bytes(preview[old_size:]))
                        self._publish("output", entry, text)
                        if on_output is not None and text:
                            await on_output(text)
                        if len(accepted) < len(chunk):
                            state, reason = "output_limit", "output_limit"
                            break
                    if state == "completed":
                        await entry.process.wait()
                ending = decoder.decode(b"", final=True)
                if on_output is not None and ending:
                    await on_output(ending)
        except TimeoutError:
            state, reason = "timed_out", "timeout"
        except OSError as error:
            state, reason = (
                "storage_failure" if entry.process else "failed",
                "storage_failure" if entry.process else "spawn_failed",
            )
            if entry.process is None and not entry.ready.done():
                entry.ready.set_exception(error)
        except Exception as error:
            entry.terminal_error = error
            state, reason = "failed", "callback_failed"
            if not entry.ready.done():
                entry.ready.set_exception(error)
        finally:
            await self._reap(entry)
            os.close(entry.fd)
            if entry.stop_reason:
                state, reason = "cancelled", entry.stop_reason
            if state == "completed" and entry.process is not None and entry.process.returncode != 0:
                state, reason = "failed", "nonzero_exit"
            output = bytes(preview).decode("utf-8", "replace")
            available = True
            if entry.job.background and entry.job.output_path is not None:
                output, available = await asyncio.to_thread(
                    read_tail, entry.job.output_path, lines=entry.policy.tail_lines, limit=entry.policy.tail_bytes
                )
            if entry.stop_reason:
                state, reason = "cancelled", entry.stop_reason
            entry.job = replace(
                entry.job,
                state=state,
                reason=reason,
                output=output,
                exit_code=entry.process.returncode if entry.process else None,
                timed_out=state == "timed_out",
                output_available=available,
                output_complete=reason in ("exited", "nonzero_exit"),
                completed_at=_now(),
            )
            if not entry.ready.done():
                entry.ready.set_exception(RuntimeError(f"shell admission cancelled: {reason}"))
            try:
                if self.on_terminal is not None:
                    await self.on_terminal(entry.job)
            except Exception as error:
                entry.terminal_error = error
            self._publish("completed", entry)

    @staticmethod
    def _write(fd: int, data: bytes) -> None:
        view = memoryview(data)
        while view:
            count = os.write(fd, view)
            if not count:
                raise OSError("shell log write made no progress")
            view = view[count:]

    async def stop(self, job_id: str, reason: str = "stopped") -> ShellJob:
        entry = self._entries[job_id]
        if not entry.job.terminal and not entry.stop_reason:
            entry.stop_reason = reason
            if entry.process is not None:
                with suppress(ProcessLookupError):
                    os.killpg(entry.process.pid, signal.SIGKILL)
        if entry.task is not None:
            await join_owned(entry.task)
        return self.get(job_id)

    async def retry_terminal(self, job_id: str) -> ShellJob:
        """Retry an idempotent host persistence hook after a recorded failure."""
        entry = self._entries[job_id]
        if not entry.job.terminal:
            raise RuntimeError("shell job is not terminal")
        if entry.task is not None:
            await join_owned(entry.task)
        if entry.terminal_error is not None and self.on_terminal is not None:
            await self.on_terminal(entry.job)
            entry.terminal_error = None
            self._publish("completed", entry)
        return self.get(job_id)

    def terminal_error(self, job_id: str) -> str:
        """Expose hook failure type without leaking credentials in exception text."""
        error = self._entries[job_id].terminal_error
        return type(error).__name__ if error is not None else ""

    async def cancel_owner(self, owner: ShellOwner, reason: str = "owner_cancelled") -> tuple[ShellJob, ...]:
        jobs = self.list(owner=owner)
        return tuple(await asyncio.gather(*(self.stop(job.job_id, reason) for job in jobs if not job.terminal)))

    async def update_policy(self, policy: ShellPolicy) -> None:
        self.policy = policy
        if not policy.allow_background:
            await asyncio.gather(
                *(
                    self.stop(e.job.job_id, "policy_revoked")
                    for e in self._entries.values()
                    if e.job.background and not e.job.terminal
                )
            )

    async def close(self, reason: str = "host_shutdown") -> None:
        self._closed = True
        await finish_on_cancel(self._close(reason))

    async def _close(self, reason: str) -> None:
        await asyncio.gather(*(self.stop(key, reason) for key in self._entries))
        for subscription in self._subscriptions:
            await subscription.aclose()
        if self._quota_fd >= 0:
            os.close(self._quota_fd)
            self._quota_fd = -1
        if not self.storage.persistent:
            for entry in self._entries.values():
                if entry.job.output_path is not None:
                    entry.job.output_path.unlink(missing_ok=True)
                    with suppress(FileNotFoundError):
                        entry.job.output_path.parent.rmdir()
            if self._temporary is not None:
                self._temporary.cleanup()
