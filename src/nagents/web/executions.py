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
    from nagents.provider import Provider
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
        self._auxiliary: set[asyncio.Task[object]] = set()
        self._leases: dict[Harness, int] = {}
        self._released: dict[Harness, asyncio.Event] = {}
        self._retired: dict[Harness, list[ToolDefinition]] = {}
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
        run.agent = run.agent or self.state.session_agents.selected(run.session_id)
        self.runs[run.session_id] = run

    def release(self, run: Run) -> None:
        if self.runs.get(run.session_id) is run:
            self.runs.pop(run.session_id)

    def in_use(self, harness: Harness) -> bool:
        """Include admitted owners before startup and after the Agent finishes."""
        return any(
            run.harness is harness or self.owners.get(session_id) is harness for session_id, run in self.runs.items()
        )

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
    async def auxiliary_provider(self, *, configuring: bool = False) -> AsyncIterator[Provider]:
        """Lease the accepted provider while a read-only request uses it.

        A settings save detaches its configuration runtime rather than changing
        this provider. The last reader joins cleanup if its owner has retired.
        ``configuring`` is only for Live's already-owned admission boundary.
        """
        if self.closed:
            raise HTTPException(503, "The server is shutting down.")
        if self.state.mutating and not configuring:
            raise HTTPException(409, "Settings are being saved. Retry the request shortly.")
        harness = self.state.harness
        task = asyncio.current_task()
        assert task is not None
        if task in self._auxiliary:
            raise RuntimeError("Auxiliary provider leases cannot be nested in one request")
        self._auxiliary.add(task)
        self._leases[harness] = self._leases.get(harness, 0) + 1
        self._released.setdefault(harness, asyncio.Event())
        try:
            yield harness.agent.provider
        finally:
            try:
                self._leases[harness] -= 1
                if not self._leases[harness]:
                    del self._leases[harness]
                    self._released.pop(harness).set()
                    await self._close_retired(harness)
            finally:
                self._auxiliary.discard(task)

    async def _close_retired(self, harness: Harness) -> None:
        if harness in self._leases or harness in self.owners.values() or harness not in self._retired:
            return
        installed = self._retired.pop(harness)
        try:
            await join_owned(asyncio.create_task(harness.close()))
        finally:
            self.state.channels.unregister_runtime(harness, installed)

    async def configuration_owner(self) -> None:
        """Detach settings from a retained execution before changing its defaults.

        A running root owns its provider, configuration and children until it
        finishes. The first edit creates a new configuration owner; subsequent
        edits reuse it. Existing lanes adopt the saved defaults on their next run.
        The caller holds the host mutation boundary throughout this operation.
        """
        primary = self.state.harness
        if primary not in self.owners.values() and primary not in self._leases:
            return
        replacement = self.factory(deepcopy(primary.config))
        if replacement is primary or replacement in self.owners.values():
            raise RuntimeError("The harness factory must provide an independent configuration runtime")
        installed: list[ToolDefinition] = []
        try:
            replacement._permission_ceiling = primary._permission_ceiling
            replacement.allow_subagents = primary.allow_subagents
            await self.borrow_auth(replacement)
            if replacement._built_provider_profile == primary._built_provider_profile:
                replacement.agent.provider.restore_context_metadata(primary.agent.provider.context_metadata_snapshot())
            replacement.session_id = self.state.selected_session_id
            replacement.agent.session = self.state.history
            replacement.approval_handler = self.state.approve
            replacement.wakeup_handler = self.state.schedule
            replacement.resources.logger = primary.resources.logger
            replacement.resources.inherit_mcp_failures(primary.resources)
            replacement.agent.plugins.append(self.state.history.identity)
            await replacement.initialize(create_session=False)
            self.state.settings.values.apply(replacement)
            installed = self.state.channels.register_runtime(replacement)
        except BaseException:
            await join_owned(asyncio.create_task(replacement.close()))
            self.state.channels.unregister_runtime(replacement, installed)
            raise
        # No await during ownership transfer: the old lane must not close the
        # shared login when it releases its runtime after this point.
        primary._owns_auth = False
        replacement._owns_auth = True
        self._retired[primary] = [
            tool for tool in self.state.channels.tools if primary.agent.tool_registry.get(tool.name) is tool
        ]
        self.state.harness = replacement
        self.state.settings.harness = replacement
        if self.state._running_harness is primary:
            self.state._running_harness = replacement
        await self._close_retired(primary)

    @asynccontextmanager
    async def harness(self, run: Run) -> AsyncIterator[Harness]:
        with self.scope(run):
            harness = self.owners.get(run.session_id)
            if harness is None:
                primary = self.state.harness
                harness = (
                    primary
                    if primary not in self.owners.values() and not primary._busy and primary.config.agent == run.agent
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
                        if harness._built_provider_profile == primary._built_provider_profile:
                            harness.agent.provider.restore_context_metadata(
                                primary.agent.provider.context_metadata_snapshot()
                            )
                        harness.session_id = run.session_id
                        harness.agent.session = self.state.history
                        harness.approval_handler = self.state.approve
                        harness.wakeup_handler = self.state.schedule
                        harness.resources.logger = primary.resources.logger
                        harness.resources.inherit_mcp_failures(primary.resources)
                        harness.agent.plugins.append(self.state.history.identity)
                        await harness.initialize(create_session=False)
                        self._tools[run.session_id] = self.state.channels.register_runtime(harness)
                    except BaseException:
                        await self.dispose(run.session_id)
                        raise
            run.harness = harness
            if harness is not self.state.harness:
                # Apply the latest saved next-turn view without changing a
                # running lane or losing its retained children and wakeups.
                if released := self._released.get(harness):
                    # A retained lane may still serve a catalog/voice summary
                    # from its previous turn. Only this next turn waits; saved
                    # defaults and independent chats remain available.
                    await released.wait()
                primary = self.state.harness
                harness.providers = deepcopy(primary.providers)
                await harness.reconfigure_provider(deepcopy(primary.config))
                if settings := getattr(self.state, "settings", None):
                    harness._permission_ceiling = primary._permission_ceiling
                    settings.values.apply(harness)
            await self.state.session_agents.apply(run, harness)
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
        installed = self._tools.pop(session_id, [])
        self._retired.setdefault(harness, installed)
        await self._close_retired(harness)

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
        auxiliary = tuple(self._auxiliary)
        for task in auxiliary:
            task.cancel()
        await asyncio.gather(*auxiliary, return_exceptions=True)
        await asyncio.gather(*(self.state.stop(run) for run in tuple(self.runs.values())))
        for session_id in tuple(self.owners):
            await self.dispose(session_id)
