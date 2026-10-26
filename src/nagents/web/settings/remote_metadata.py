"""Bounded asynchronous refresh of remote AgentCard presentation metadata."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from nagents._async import finish_on_cancel
from nagents.harness.remote.credentials import RemoteCredentials

if TYPE_CHECKING:
    from nagents.harness.config import AgentProfile
    from nagents.harness.remote.store import RemoteAgentStore
    from nagents.harness.runtime.core import Harness

logger = logging.getLogger(__name__)
METADATA_TTL_SECONDS = 60
FAILURE_BACKOFF_SECONDS = 60
MAX_REFRESHES = 4
REMOTE_CARD_TIMEOUT_SECONDS = 12


class RemoteMetadataRefresh:
    """Coalesce stale card reads and own their lifetime under the web app."""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._retry_after: dict[str, float] = {}
        self._closed = False

    def status(self, fingerprint: str, observed_at: float) -> str:
        if fingerprint in self._tasks:
            return "checking"
        if self._retry_after.get(fingerprint, 0) > time.monotonic():
            return "refresh_failed"
        if observed_at and time.time() - observed_at < METADATA_TTL_SECONDS:
            return "current"
        return "stale"

    def schedule(self, harness: Harness) -> None:
        if self._closed or harness.config.demo or harness.mode == "reviewer":
            return
        try:
            catalog = harness._agent_catalog.remote_store.load()
        except Exception as error:
            logger.warning("Remote card metadata refresh deferred error_type=%s", type(error).__name__)
            return
        now = time.time()
        store = harness._agent_catalog.remote_store
        for alias, profile in catalog.agents.items():
            fingerprint = profile.a2a.fingerprint
            if fingerprint in self._tasks:
                continue
            if self._retry_after.get(fingerprint, 0) > time.monotonic():
                continue
            previous = catalog.advertised.get(alias)
            if previous is not None and now - previous.observed_at < METADATA_TTL_SECONDS:
                continue
            if len(self._tasks) >= MAX_REFRESHES:
                break
            task = asyncio.create_task(
                self._refresh(store, alias, profile),
                name=f"ngn-remote-card:{alias}",
            )
            self._tasks[fingerprint] = task

            def finished(done: asyncio.Task[None], *, key: str = fingerprint) -> None:
                self._finished(key, done)

            task.add_done_callback(finished)

    def _finished(self, fingerprint: str, task: asyncio.Task[None]) -> None:
        self._tasks.pop(fingerprint, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self._retry_after[fingerprint] = time.monotonic() + FAILURE_BACKOFF_SECONDS
            if len(self._retry_after) > 512:
                now = time.monotonic()
                self._retry_after = {key: value for key, value in self._retry_after.items() if value > now}
                for key in tuple(self._retry_after)[: max(0, len(self._retry_after) - 256)]:
                    self._retry_after.pop(key, None)
            logger.warning("Remote card metadata refresh failed error_type=%s", type(error).__name__)

    async def _refresh(self, store: RemoteAgentStore, alias: str, profile: AgentProfile) -> None:
        from nagents.a2a.client.transport import RemoteAgent

        credentials = RemoteCredentials(profile.a2a)
        token_provider = credentials.token if profile.a2a.issuer or profile.a2a.token_env else None
        async with asyncio.timeout(REMOTE_CARD_TIMEOUT_SECONDS):
            remote = await finish_on_cancel(asyncio.to_thread(RemoteAgent.from_config, profile.a2a, token_provider))
            async with remote:
                await finish_on_cancel(
                    asyncio.to_thread(
                        store.update_metadata,
                        profile.a2a.fingerprint,
                        remote.card.name[:200],
                        remote.card.description[:2000],
                    )
                )

    async def close(self) -> None:
        self._closed = True
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
