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

from nagents.harness.session_titles import generate_title
from nagents.session.forks import SessionForkError
from nagents.session.forks import fork_in
from nagents.session.forks import rename_in
from nagents.session.forks import title_revision
from nagents.session.forks import title_text

from .._async import finish_on_cancel
from ..routing import RoutingStore

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable

    from ..execution.service import WebState
    from ..live.runtime import LiveService

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
    title: str = Field(default="", max_length=80)

    @field_validator("title")
    @classmethod
    def valid_title(cls, value: str) -> str:
        return title_text(value, optional=True)


def quiescent(state: WebState, root: str) -> None:
    if state.run_for(root) is not None or root in state.channels.work_tasks or root in state.channels.work_cleanup:
        raise HTTPException(409, "Finish or cancel this chat's active run before forking it.")
    harness = state.harness_for(root)
    if any(
        info.root_session_id == root
        and (
            info.status not in {"completed", "failed", "cancelled"}
            or (info.session_id in harness.tasks._workers and not harness.tasks._workers[info.session_id].done())
        )
        for info in harness.tasks._infos.values()
    ):
        raise HTTPException(409, "Finish descendant work in this chat before forking it.")
    if any(item.root_session_id == root for item in state.wakeups.pending.values()):
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
            title = body.title
            previous = await host.channels.store._transaction(
                lambda db: (RoutingStore.root(db, session_id), title_revision(db, session_id))[1]
            )
            if not title:
                try:
                    async with (
                        host.executions.auxiliary_provider(),
                        host.session_agents.view(session_id) as harness,
                    ):
                        title = await generate_title(harness, session_id)
                except (ValueError, NotImplementedError) as error:
                    raise HTTPException(422, str(error)) from None
                except TimeoutError:
                    raise HTTPException(504, "Title generation timed out. Retry /rename or supply a title.") from None

            async def persist() -> dict[str, object]:
                with host.idle(allow_running=True):

                    def save(db: sqlite3.Connection) -> None:
                        RoutingStore.root(db, session_id)
                        if not body.title and title_revision(db, session_id) != previous:
                            raise HTTPException(
                                409, "This chat was renamed while its title was generating. Retry /rename."
                            )
                        rename_in(db, session_id, title)

                    await host.channels.store._transaction(save)
                    host.session_revision += 1
                    result = await host.snapshot()
                    host.bus.publish({"type": "sessions", "sessions": result["sessions"]})
                    return result

            return await finish_on_cancel(persist())

        return await change()
