"""Authenticated image downloads from this root's exact committed history."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING
from typing import Annotated
from urllib.parse import quote

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Path as PathParameter
from starlette.responses import Response

from nagents.harness.remote.artifacts import image_bytes
from nagents.types import ImageContent

from ._async import finish_on_cancel
from .routing import RoutingStore
from .session_actions import RootId  # noqa: TC001 - FastAPI resolves this alias at runtime.

if TYPE_CHECKING:
    from collections.abc import Callable

    from .execution.service import WebState


def register_artifacts(app: FastAPI, get: Callable[[], WebState]) -> None:
    @app.get("/api/sessions/{session_id}/artifacts/{history_id}/{part_index}")
    async def asset(
        session_id: RootId,
        history_id: Annotated[int, PathParameter(gt=0, le=2**63 - 1)],
        part_index: Annotated[int, PathParameter(ge=0, lt=256)],
    ) -> Response:
        state = get()
        await state.tasks.store.initialize()

        def read() -> tuple[bytes, str, str]:
            with closing(sqlite3.connect(state.history.db_path, timeout=5)) as db, db:
                db.execute("BEGIN")
                RoutingStore.execution_root(db, session_id)
                if db.execute("SELECT 1 FROM ngn_web_session_trash WHERE id = ?", (session_id,)).fetchone():
                    raise ValueError("Removed conversation")
                db.row_factory = sqlite3.Row
                row = db.execute(
                    "SELECT m.* FROM v2_messages m WHERE m.id = ? AND (m.session_id = ? OR EXISTS ("
                    "SELECT 1 FROM nagents_agent_sessions a WHERE a.storage_id = m.session_id AND a.root_session_id = ? "
                    "AND NOT EXISTS (SELECT 1 FROM harness_sessions h WHERE h.id = m.session_id)))",
                    (history_id, session_id, session_id),
                ).fetchone()
                if row is None:
                    raise ValueError("Unavailable artifact")
            # Authorization and the exact row are captured in one short read
            # snapshot. Parsing and decoding bytes must not retain its lock.
            message = state.history._row_to_message(row)
            if not isinstance(message.content, list) or part_index >= len(message.content):
                raise ValueError("Unavailable artifact part")
            part = message.content[part_index]
            if not isinstance(part, ImageContent) or not part.artifact_id:
                raise ValueError("Unavailable image artifact")
            return image_bytes(part), part.media_type, part.filename

        try:
            data, media_type, filename = await finish_on_cancel(asyncio.to_thread(read))
        except (ValueError, HTTPException):
            raise HTTPException(404, "Image artifact not found in this active conversation.") from None
        return Response(
            data,
            media_type=media_type,
            headers={
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, no-store",
                "Content-Disposition": f"inline; filename*=UTF-8''{quote(filename, safe='')}",
            },
        )
