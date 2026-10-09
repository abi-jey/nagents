"""Reconcile transport indicators with the executing root's permanent chat owner."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import replace
from typing import TYPE_CHECKING

from nagents.channels.runtime import _activity
from nagents.channels.types import ChannelActivity

from ._async import finish_on_cancel

if TYPE_CHECKING:
    from collections.abc import Mapping

    from nagents.channels.types import Channel

    from .routing import RoutingStore


class ChannelActivities:
    def __init__(self, store: RoutingStore, channels: Mapping[str, Channel]) -> None:
        self.store = store
        self.channels = channels
        self.sources: dict[str, dict[str, str]] = {}
        self.current: dict[tuple[str, str, str], tuple[Channel, ChannelActivity]] = {}
        self.lock = asyncio.Lock()
        self.revision = 0

    async def set(self, session_id: str, active: bool, source: dict[str, str]) -> None:
        if active:
            self.sources[session_id] = dict(source)
        else:
            self.sources.pop(session_id, None)
        await self.refresh()

    async def refresh(self) -> None:
        self.revision += 1
        # DB/control failures must neither reject durable admission nor fail a
        # model turn, and raw connector errors may contain credentials.
        with suppress(Exception):
            # A cancelled ingress/producer still joins its control operation. This
            # prevents late starts from escaping subsequent run/connector cleanup.
            await finish_on_cancel(self._refresh())

    async def _refresh(self) -> None:
        async with self.lock:
            revision = self.revision
            desired: dict[tuple[str, str, str], tuple[Channel, ChannelActivity]] = {}
            for session_id, source in tuple(self.sources.items()):
                for binding in await self.store.activity_bindings(session_id):
                    channel = self.channels.get(binding["channel"])
                    if binding["session_id"] != session_id or channel is None:
                        continue
                    origin = (binding["channel"], binding["conversation_id"]) == (
                        source.get("channel"),
                        source.get("conversation_id"),
                    )
                    thread = source.get("thread_id", "") if origin else ""
                    key = (binding["channel"], binding["conversation_id"], thread)
                    desired.setdefault(
                        key, (channel, ChannelActivity(binding["conversation_id"], True, thread, session_id))
                    )
            # Another root still using this destination/thread keeps its
            # indicator active; a representative change must not send a stop.
            stopping = {
                key: value
                for key, value in self.current.items()
                if key not in desired or desired[key][0] is not value[0]
            }
            await asyncio.gather(
                *(_activity(channel, replace(event, active=False)) for channel, event in stopping.values())
            )
            for key in stopping:
                del self.current[key]
            # A newer execution/owner/connector change gets its own reconciliation. Do not
            # dispatch stale starts after a potentially slow stop or DB read.
            if revision != self.revision:
                return
            starting = {key: value for key, value in desired.items() if self.current.get(key) != value}
            self.current.update(starting)
            await asyncio.gather(*(_activity(channel, event) for channel, event in starting.values()))

    async def close(self) -> None:
        self.sources.clear()
        await self.refresh()
