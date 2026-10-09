"""Session-owned agent runtimes and exact task-local execution identities."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from typing import TYPE_CHECKING

from fastapi import HTTPException

from nagents.provider.openai import OpenAIProvider

from ._async import join_owned

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from collections.abc import Callable
    from collections.abc import Iterator

    from nagents.harness.config import HarnessConfig
    from nagents.harness.runtime import Harness
    from nagents.types import ToolDefinition

    from .service import Run
    from .service import WebState

MAX_PARALLEL_RUNS = 4


class Executions:
    def __init__(self, state: WebState, factory: Callable[[HarnessConfig], Harness]) -> None:
        self.state, self.factory = state, factory
        self.runs: dict[str, Run] = {}
        self.owners: dict[str, Harness] = {}
        self.current: ContextVar[Run | None] = ContextVar("ngn_web_execution", default=None)
        self._tools: dict[str, list[ToolDefinition]] = {}
        self.closed = False

    def active(self) -> Run | None:
        current = self.current.get()
        if current is not None:
            # Inherited context cannot become authority for a different run
            # after its original owner has ended.
            return current if self.runs.get(current.session_id) is current and not current.finished else None
        return self.runs.get(self.state.selected_session_id) or next(iter(self.runs.values()), None)

    def reserve(self, run: Run) -> None:
        previous = self.runs.get(run.session_id)
        if previous is run:
            return
        if self.closed or previous is not None or len(self.runs) >= MAX_PARALLEL_RUNS:
            raise HTTPException(409, "This chat is running or all execution slots are busy. Queue the message instead.")
        self.runs[run.session_id] = run

    def release(self, run: Run) -> None:
        if self.runs.get(run.session_id) is run:
            self.runs.pop(run.session_id)

    @contextmanager
    def scope(self, run: Run) -> Iterator[None]:
        if self.runs.get(run.session_id) is not run:
            raise RuntimeError("The web run no longer owns its session")
        token = self.current.set(run)
        try:
            yield
        finally:
            self.current.reset(token)

    async def borrow_auth(self, harness: Harness) -> None:
        primary = self.state.harness
        previous = harness.openai_auth
        provider = harness.agent.provider
        if (
            isinstance(provider, OpenAIProvider)
            and provider.uses_chatgpt_auth
            and provider._credentials == previous.credentials
        ):
            provider._credentials = primary.openai_auth.credentials
        harness.openai_auth = primary.openai_auth
        harness._owns_auth = False
        if previous is not primary.openai_auth:
            await previous.close()

    @asynccontextmanager
    async def harness(self, run: Run) -> AsyncIterator[Harness]:
        with self.scope(run):
            harness = self.owners.get(run.session_id)
            if harness is None:
                primary = self.state.harness
                harness = (
                    primary
                    if primary not in self.owners.values() and not primary._busy
                    else self.factory(deepcopy(primary.config))
                )
                if harness in self.owners.values():
                    raise RuntimeError("The harness factory must provide an independent runtime for another chat")
                self.owners[run.session_id] = harness
                run.harness = harness
                if harness is not primary:
                    try:
                        harness._permission_ceiling = primary._permission_ceiling
                        harness.allow_subagents = primary.allow_subagents
                        await self.borrow_auth(harness)
                        harness.session_id = run.session_id
                        harness.agent.session = self.state.history
                        harness.approval_handler = self.state.approve
                        harness.wakeup_handler = self.state.schedule
                        harness.resources.logger = primary.resources.logger
                        harness.agent.plugins.append(self.state.history.identity)
                        await harness.initialize(create_session=False)
                        self._tools[run.session_id] = self.state.channels.register_runtime(harness)
                    except BaseException:
                        await self.dispose(run.session_id)
                        raise
            run.harness = harness
            if harness is not self.state.harness:
                # Global routing/settings changes happen only when every root
                # is idle. Apply their next-turn view without losing this lane's
                # retained children or scheduled task handles.
                primary = self.state.harness
                await harness.reconfigure_provider(deepcopy(primary.config))
                if settings := getattr(self.state, "settings", None):
                    settings.values.apply(harness)
            yield harness

    async def dispose(self, session_id: str) -> None:
        harness = self.owners.pop(session_id, None)
        if harness is None:
            return
        if harness is self.state.harness:
            if not harness._busy:
                harness.session_id = self.state.selected_session_id
                harness.tools.read_hashes.clear()
            return
        try:
            await join_owned(asyncio.create_task(harness.close()))
        finally:
            installed = self._tools.pop(session_id, [])
            self.state.channels.unregister_runtime(harness, installed)

    async def release_idle(self, session_id: str) -> None:
        harness = self.owners.get(session_id)
        if (
            harness is not None
            and session_id not in self.runs
            and not harness.tasks._infos
            and not any(item.session_id == session_id for item in self.state.wakeups.pending.values())
        ):
            await self.dispose(session_id)

    async def close(self) -> None:
        self.closed = True
        await asyncio.gather(*(self.state.stop(run) for run in tuple(self.runs.values())))
        for session_id in tuple(self.owners):
            await self.dispose(session_id)
