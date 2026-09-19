"""Pinned designed agents behind the existing channel queue and ownership boundary."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextlib import closing
from contextlib import suppress
from typing import TYPE_CHECKING

from fastapi import HTTPException

from nagents.designer.runtime import DesignedHarness
from nagents.designer.schema import Design
from nagents.designer.schema import parse
from nagents.designer.schema import serialize
from nagents.designer.store import Recorder
from nagents.designer.store import TraceStore
from nagents.observation import observer

from .settings import _join

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import AsyncIterator

    from nagents.harness import Harness

    from .service import Run
    from .service import WebState


class DesignedChannels:
    def __init__(self, state: WebState) -> None:
        self.state = state
        self.traces = TraceStore(state.harness.agent.session.db_path.parent / "designer.db")

    async def initialize(self) -> None:
        def create(db: sqlite3.Connection) -> None:
            db.execute(
                "CREATE TABLE IF NOT EXISTS ngn_design_channels "
                "(connection TEXT PRIMARY KEY, design_id TEXT NOT NULL, agent TEXT NOT NULL, source TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS ngn_design_sessions "
                "(session_id TEXT PRIMARY KEY, agent TEXT NOT NULL, source TEXT NOT NULL)"
            )

        await self.state.channels.store._transaction(create)
        await self.traces.initialize()

    async def routes(self) -> list[dict[str, str]]:
        def read(db: sqlite3.Connection) -> list[dict[str, str]]:
            with closing(db.execute("SELECT connection, design_id, agent FROM ngn_design_channels")) as cursor:
                return [dict(zip(("connection", "design", "agent"), row, strict=True)) for row in cursor.fetchall()]

        return await self.state.channels.store._transaction(read)

    async def apply(self, design: Design) -> list[dict[str, str]]:
        host = self.state.channels
        if any(connection not in host.catalog.connections for connection in design.channels):
            raise HTTPException(422, "Configure the referenced channel connections first.")
        if await host.store.has_pending():
            raise HTTPException(409, "Finish queued messages before changing channel routes.")
        source = serialize(design.resolved(self.state.harness.workspace))

        def save(db: sqlite3.Connection) -> None:
            for connection in design.channels:
                with closing(
                    db.execute("SELECT design_id FROM ngn_design_channels WHERE connection = ?", (connection,))
                ) as cursor:
                    previous = cursor.fetchone()
                if previous and previous[0] != design.id:
                    raise HTTPException(409, f"Connection {connection} is already routed by design {previous[0]}.")
            db.execute("DELETE FROM ngn_design_channels WHERE design_id = ?", (design.id,))
            db.executemany(
                "INSERT INTO ngn_design_channels VALUES (?, ?, ?, ?)",
                [(connection, design.id, agent, source) for connection, agent in design.channels.items()],
            )

        await host.store._transaction(save)
        return await self.routes()

    async def pin(self, session_id: str) -> tuple[str, str]:
        def resolve(db: sqlite3.Connection) -> tuple[str, str]:
            store = self.state.channels.store
            store.root(db, session_id)
            with closing(
                db.execute("SELECT agent, source FROM ngn_design_sessions WHERE session_id = ?", (session_id,))
            ) as cursor:
                previous = cursor.fetchone()
            if previous:
                return str(previous[0]), str(previous[1])
            owner = store._owner(db, session_id)
            selected = ("", "")
            # Existing conversations keep their original runtime. /new creates a
            # fresh chat-owned root which can adopt the current channel route.
            with closing(db.execute("SELECT 1 FROM v2_messages WHERE session_id = ? LIMIT 1", (session_id,))) as cursor:
                populated = cursor.fetchone() is not None
            if owner is not None and not owner.conflicted and not populated:
                with closing(
                    db.execute("SELECT agent, source FROM ngn_design_channels WHERE connection = ?", (owner.channel,))
                ) as cursor:
                    route = cursor.fetchone()
                if route:
                    selected = str(route[0]), str(route[1])
            db.execute("INSERT INTO ngn_design_sessions VALUES (?, ?, ?)", (session_id, *selected))
            return selected

        return await self.state.channels.store._transaction(resolve)

    async def pinned(self, session_id: str) -> bool:
        def read(db: sqlite3.Connection) -> bool:
            with closing(
                db.execute("SELECT 1 FROM ngn_design_sessions WHERE session_id = ? AND source != ''", (session_id,))
            ) as cursor:
                return cursor.fetchone() is not None

        return await self.state.channels.store._transaction(read)

    @asynccontextmanager
    async def execution(self, run: Run) -> AsyncIterator[Harness]:
        agent, source = await self.pin(run.session_id)
        if not source:
            yield self.state.harness
            return
        recorder = Recorder()
        harness = DesignedHarness(self.state.harness.config, parse(source), agent, recorder)
        harness.session_id = run.session_id
        harness.agent.session = self.state.history
        harness.agent.plugins.append(self.state.history.identity)
        harness.approval_handler = self.state.approve
        host = self.state.channels
        installed = []
        for tool in host.tools[:]:
            if tool.name in {"channel_list", "channel_send", "channel_action"} and tool.func is not None:
                installed.append(
                    harness.agent.tool_registry.register(
                        tool.func, name=tool.name, description=tool.description, parameters=tool.parameters
                    )
                )
        host.tools.extend(installed)
        harness.agent.plugins.append(host.instructions)
        token = observer.set(recorder)
        finished = asyncio.Event()

        async def persist() -> None:
            while not finished.is_set():
                await recorder.flush(self.traces, run.id)
                with suppress(TimeoutError):
                    async with asyncio.timeout(0.2):
                        await finished.wait()
            await recorder.flush(self.traces, run.id)

        writers: list[asyncio.Task[None]] = []
        try:
            await self.traces.start(run.id, run.session_id, agent, source)
            recorder("channel_run", {"agent_id": agent, "session_id": run.session_id, "source": run.source})
            writers.append(asyncio.create_task(persist()))
            yield harness
        except asyncio.CancelledError:
            run.outcome = "cancelled"
            raise
        except BaseException:
            if run.outcome == "completed":
                run.outcome = "failed"
            raise
        finally:

            async def cleanup() -> None:
                try:
                    await harness.close()
                finally:
                    host.tools[:] = [tool for tool in host.tools if not any(tool is item for item in installed)]
                    finished.set()
                    for writer in writers:
                        await writer
                    await self.traces.finish(run.id, run.outcome)

            try:
                await _join(asyncio.create_task(cleanup()))
            finally:
                observer.reset(token)
