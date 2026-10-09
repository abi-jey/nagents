"""Explicit root rename/fork actions, independent of execution ownership."""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Annotated

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi import Path as PathParameter
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator

from nagents.session.forks import SessionForkError
from nagents.session.forks import fork_in
from nagents.session.forks import rename_in
from nagents.session.forks import title_text

from ._async import finish_on_cancel
from .routing import RoutingStore

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

    from .live_runtime import LiveService
    from .service import WebState

RootId = Annotated[str, PathParameter(min_length=1, max_length=80, pattern=r"^ngn-[a-zA-Z0-9-]+$")]


class ForkInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    title: str = Field(default="", max_length=80)

    @field_validator("title")
    @classmethod
    def valid_title(cls, value: str) -> str:
        return title_text(value, optional=True)


class RenameInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    title: str = Field(min_length=1, max_length=80)

    @field_validator("title")
    @classmethod
    def valid_title(cls, value: str) -> str:
        return title_text(value)


def quiescent(state: WebState, root: str) -> None:
    if state.run_for(root) is not None or root in state.channels.work_tasks or root in state.channels.work_cleanup:
        raise HTTPException(409, "Finish or cancel this chat's active run before forking it.")
    harness = state.harness_for(root)
    if any(
        info.session_id == root
        and (
            info.status not in {"completed", "failed", "cancelled"}
            or (info.id in harness.tasks._workers and not harness.tasks._workers[info.id].done())
        )
        for info in harness.tasks._infos.values()
    ):
        raise HTTPException(409, "Finish descendant work in this chat before forking it.")
    if any(item.session_id == root for item in state.wakeups.pending.values()):
        raise HTTPException(409, "Finish or cancel this chat's scheduled work before forking it.")


def register_session_actions(app: FastAPI, state: Callable[[], WebState], live: Callable[[], LiveService]) -> None:
    @app.post("/api/sessions/{session_id}/fork")
    async def fork(session_id: RootId, body: ForkInput) -> dict[str, object]:
        async def change() -> dict[str, object]:
            host = state()
            with host.idle(allow_running=True):
                await host.channels.store._transaction(lambda db: RoutingStore.root(db, session_id))
                quiescent(host, session_id)
                if live().active_session_id:
                    await live().close(live().active_session_id)
                quiescent(host, session_id)
                try:
                    target = await host.channels.store._transaction(lambda db: fork_in(db, session_id, body.title))
                except SessionForkError as error:
                    raise HTTPException(409, str(error)) from None
                host.selected_session_id = target
                host.session_revision += 1
                if not host.harness._busy and not host.executions.in_use(host.harness):
                    host.harness.session_id = target
                    host.harness.tools.read_hashes.clear()
                result = await host.snapshot(target)
                host.bus.publish({"type": "sessions", "sessions": result["sessions"]})
                return result

        return await finish_on_cancel(change())

    @app.post("/api/sessions/{session_id}/rename")
    async def rename(session_id: RootId, body: RenameInput) -> dict[str, object]:
        async def change() -> dict[str, object]:
            host = state()
            with host.idle(allow_running=True):

                def save(db: sqlite3.Connection) -> None:
                    RoutingStore.root(db, session_id)
                    rename_in(db, session_id, body.title)

                await host.channels.store._transaction(save)
                host.session_revision += 1
                result = await host.snapshot()
                host.bus.publish({"type": "sessions", "sessions": result["sessions"]})
                return result

        return await finish_on_cancel(change())
