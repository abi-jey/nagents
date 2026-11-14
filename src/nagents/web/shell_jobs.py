"""Private session-owned shell controls and bounded, ID-only log downloads."""

from __future__ import annotations

import asyncio
import os
import re
import stat
from typing import TYPE_CHECKING
from typing import Annotated

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Path as PathParameter
from pydantic import BaseModel
from pydantic import ConfigDict
from starlette.responses import StreamingResponse

from .http.routing import RoutingStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from collections.abc import Callable

    from nagents.harness import Harness

    from .execution.service import WebState

SessionId = Annotated[str, PathParameter(pattern=r"^ngn-[A-Za-z0-9-]{1,76}$")]
JobId = Annotated[str, PathParameter(pattern=r"^[A-Za-z0-9_-]{1,128}$")]
DOWNLOAD_LIMIT = 64 * 1024 * 1024


class StopInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def open_output(harness: Harness, job_id: str, output_path: str) -> tuple[int, int]:
    """Open only an allocated private log; pin every mutable path component."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id):
        raise HTTPException(404, "Shell output unavailable.")
    root = harness.agent.session.db_path.parent / "shell-jobs"
    if output_path != str(root / job_id / "output.log"):
        raise HTTPException(404, "Shell output unavailable.")
    descriptors: list[int] = []
    try:
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        descriptors.append(directory)
        directory = os.open(job_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
        descriptors.append(directory)
        for descriptor in descriptors:
            info = os.fstat(descriptor)
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise OSError("Shell output directory is not private")
        descriptor = os.open("output.log", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        descriptors.append(descriptor)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise OSError("Shell output is not a private regular file")
        if info.st_size > DOWNLOAD_LIMIT:
            raise HTTPException(413, "Shell output exceeds the download budget.")
        descriptors.pop()
        return descriptor, info.st_size
    except OSError as error:
        raise HTTPException(404, "Shell output unavailable.") from error
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def register(app: FastAPI, state: Callable[[], WebState]) -> None:
    async def owner(session_id: str) -> Harness:
        host = state()
        await host.channels.store._transaction(lambda db: RoutingStore.root(db, session_id))
        return host.harness_for(session_id)

    @app.get("/api/sessions/{session_id}/shell-jobs")
    async def jobs(session_id: SessionId) -> dict[str, object]:
        harness = await owner(session_id)
        return {"jobs": await harness.shell_jobs.list(session_id=session_id)}

    @app.get("/api/sessions/{session_id}/shell-jobs/{job_id}")
    async def detail(session_id: SessionId, job_id: JobId) -> dict[str, object]:
        harness = await owner(session_id)
        try:
            return dict(await harness.shell_jobs.get(job_id, session_id=session_id))
        except (ValueError, KeyError) as error:
            raise HTTPException(404, "Shell job not found in this session.") from error

    @app.post("/api/sessions/{session_id}/shell-jobs/{job_id}/stop")
    async def stop(session_id: SessionId, job_id: JobId, body: StopInput) -> dict[str, object]:
        harness = await owner(session_id)
        try:
            return dict(await harness.shell_jobs.stop(job_id, session_id=session_id))
        except (ValueError, KeyError) as error:
            raise HTTPException(404, "Shell job not found in this session.") from error

    @app.get("/api/sessions/{session_id}/shell-jobs/{job_id}/output")
    async def output(session_id: SessionId, job_id: JobId) -> StreamingResponse:
        harness = await owner(session_id)
        try:
            job = await harness.shell_jobs.get(job_id, session_id=session_id)
        except (ValueError, KeyError) as error:
            raise HTTPException(404, "Shell job not found in this session.") from error
        path = job.get("output_path")
        if not isinstance(path, str):
            raise HTTPException(404, "Shell output unavailable.")
        descriptor, size = open_output(harness, job_id, path)

        async def chunks() -> AsyncIterator[bytes]:
            remaining = size
            try:
                while remaining:
                    chunk = await asyncio.to_thread(os.read, descriptor, min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk
            finally:
                os.close(descriptor)

        return StreamingResponse(
            chunks(),
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="shell-{job_id}.log"',
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )
