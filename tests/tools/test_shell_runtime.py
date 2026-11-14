"""Reusable shell lifecycle and storage regressions."""

from __future__ import annotations

import asyncio
import os
import stat
from typing import TYPE_CHECKING

import pytest

from nagents.tools.shell import ShellController
from nagents.tools.shell import ShellJob
from nagents.tools.shell import ShellOwner
from nagents.tools.shell import ShellPolicy

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(os.name != "posix", reason="POSIX")]


def owner(path: Path, session: str = "s") -> ShellOwner:
    return ShellOwner(path, session, "run")


async def finished(controller: ShellController, job: ShellJob) -> ShellJob:
    async with asyncio.timeout(5):
        while not controller.get(job.job_id).terminal:
            await asyncio.sleep(0.001)
    return await controller.stop(job.job_id)


async def test_foreground(tmp_path: Path) -> None:
    controller = ShellController(ShellPolicy(max_output=5))
    chunks: list[str] = []

    async def output(text: str) -> None:
        chunks.append(text)

    try:
        with pytest.raises(PermissionError):
            await controller.execute("true", owner=owner(tmp_path), background=True)
        job = await controller.execute("printf abcdefgh", owner=owner(tmp_path), on_output=output)
        assert job.output == "abcde" == "".join(chunks)
        assert job.truncated and job.exit_code == 0
    finally:
        await controller.close()


async def test_full_log_and_tail(tmp_path: Path) -> None:
    controller = ShellController(ShellPolicy(allow_background=True, tail_bytes=40, tail_lines=2))
    try:
        job = await controller.execute(
            "i=0; while [ $i -lt 300 ]; do echo $i; i=$((i+1)); done", owner=owner(tmp_path), background=True
        )
        job = await finished(controller, job)
        assert job.output == "298\n299\n"
        assert job.output_path is not None
        assert len(job.output_path.read_bytes().splitlines()) == 300
        assert stat.S_IMODE(job.output_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(job.output_path.parent.stat().st_mode) == 0o700
        giant = await controller.execute(
            "head -c 50000 /dev/zero | tr '\\000' x", owner=owner(tmp_path), background=True
        )
        giant = await finished(controller, giant)
        assert giant.output == "x" * 40 and giant.bytes_written == 50000
    finally:
        await controller.close()
    assert not controller.get(job.job_id).output_available


async def test_stop_owner_timeout_limit(tmp_path: Path) -> None:
    controller = ShellController(ShellPolicy(allow_background=True, max_log_bytes=30))
    try:
        first = await controller.execute("sleep 20", owner=owner(tmp_path), background=True)
        second = await controller.execute("sleep 20", owner=owner(tmp_path, "other"), background=True)
        await controller.cancel_owner(owner(tmp_path), "interrupted")
        assert (await controller.stop(first.job_id, "changed")).reason == "interrupted"
        assert not controller.get(second.job_id).terminal
        timeout = await controller.execute("sleep 20", owner=owner(tmp_path), timeout=0.02)
        assert timeout.timed_out
        limit = await controller.execute("head -c 50000 /dev/zero", owner=owner(tmp_path), background=True)
        limit = await finished(controller, limit)
        assert limit.state == "output_limit" and limit.bytes_written == 30 and not limit.output_complete
    finally:
        await controller.close()
    assert controller.get(second.job_id).reason == "host_shutdown"


async def test_subscriber_is_nonblocking(tmp_path: Path) -> None:
    controller = ShellController(ShellPolicy(allow_background=True, subscriber_queue_size=1))
    blocked = asyncio.Event()

    async def callback(event: object) -> None:
        await blocked.wait()

    controller.subscribe(callback)
    try:
        job = await controller.execute("head -c 200000 /dev/zero", owner=owner(tmp_path), background=True)
        assert (await finished(controller, job)).bytes_written == 200000
    finally:
        await controller.close()


async def test_cancel_spawn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = asyncio.create_subprocess_exec
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    errors: list[dict[str, object]] = []
    loop.set_exception_handler(lambda event_loop, context: errors.append(context))
    spawned = asyncio.Event()
    release = asyncio.Event()
    processes: list[asyncio.subprocess.Process] = []

    async def delayed(*args: str, **kwargs: object) -> asyncio.subprocess.Process:
        process = await real(*args, **kwargs)  # type: ignore[arg-type]
        processes.append(process)
        spawned.set()
        await release.wait()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed)
    controller = ShellController(ShellPolicy(allow_background=True))
    task = asyncio.create_task(controller.execute("sleep 20", owner=owner(tmp_path), background=True))
    await asyncio.wait_for(spawned.wait(), 5)
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert processes[0].returncode is not None
    await controller.close()
    await asyncio.sleep(0)
    loop.set_exception_handler(previous_handler)
    assert not errors


async def test_tail_controls_unicode_missing(tmp_path: Path) -> None:
    from nagents.tools.shell.storage import read_tail

    path = tmp_path / "output"
    path.write_bytes(b"\xff\x1b[31m\x00" + b"\xc3\xa9" * 30)
    text, available = read_tail(path, lines=2, limit=31)
    assert available and len(text.encode()) <= 31
    assert "\x1b" not in text and "\x00" not in text
    path.unlink()
    assert read_tail(path, lines=2, limit=31) == ("", False)


async def test_terminal_hook_retry(tmp_path: Path) -> None:
    attempts = 0

    async def persist(job: ShellJob) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("private error detail")

    controller = ShellController(ShellPolicy(allow_background=True), on_terminal=persist)
    try:
        job = await controller.execute("true", owner=owner(tmp_path), background=True)
        await finished(controller, job)
        assert controller.terminal_error(job.job_id) == "OSError"
        await controller.retry_terminal(job.job_id)
        assert attempts == 2 and not controller.terminal_error(job.job_id)
    finally:
        await controller.close()


async def test_quota_root_exclusive_and_existing_logs(tmp_path: Path) -> None:
    from nagents.tools.shell import ShellStorage

    root = tmp_path / "logs"
    policy = ShellPolicy(max_total_log_bytes=10)
    storage = ShellStorage(root, persistent=True)
    controller = ShellController(policy, storage)
    with pytest.raises(RuntimeError, match="active controller"):
        ShellController(policy, storage)
    await controller.execute("printf 12345678", owner=owner(tmp_path))
    await controller.close()
    controller = ShellController(policy, storage)
    try:
        job = await controller.execute("printf 12345678", owner=owner(tmp_path))
        assert job.state == "output_limit" and job.bytes_written == 2
    finally:
        await controller.close()


async def test_storage_failure_and_total_quota(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    controller = ShellController(ShellPolicy(allow_background=True, max_total_log_bytes=10))
    try:
        assert (await controller.execute("printf 12345678", owner=owner(tmp_path))).output_complete
        second = await controller.execute("printf 12345678", owner=owner(tmp_path))
        assert second.state == "output_limit" and second.bytes_written == 2
    finally:
        await controller.close()
    controller = ShellController()

    def fail(fd: int, data: bytes) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(controller, "_write", fail)
    try:
        job = await controller.execute("printf hello", owner=owner(tmp_path))
        assert job.state == "storage_failure" and not job.output_complete
    finally:
        await controller.close()
