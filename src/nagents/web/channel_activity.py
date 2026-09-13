"""Reconcile transport indicators with the currently attached, active root session."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import replace
from typing import TYPE_CHECKING

from nagents.channels.runtime import _activity
from nagents.channels.store import finish_on_cancel
from nagents.channels.types import ChannelActivity

if TYPE_CHECKING:
    from collections.abc import Mapping

    from nagents.channels.types import Channel

    from .routing import RoutingStore


class ChannelActivities:
    def __init__(self, store: RoutingStore, channels: Mapping[str, Channel]) -> None:
        self.store = store
        self.channels = channels
        self.session_id = ""
        self.source: dict[str, str] = {}
        self.current: dict[tuple[str, str], tuple[Channel, ChannelActivity]] = {}
        self.lock = asyncio.Lock()
        self.revision = 0

    async def set(self, session_id: str, active: bool, source: dict[str, str]) -> None:
        if active:
            self.session_id = session_id
            self.source = dict(source)
        elif self.session_id == session_id:
            self.session_id = ""
            self.source = {}
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
            bindings = await self.store.bindings() if self.session_id else []
            desired: dict[tuple[str, str], tuple[Channel, ChannelActivity]] = {}
            for binding in bindings:
                channel = self.channels.get(binding["channel"])
                if binding["session_id"] != self.session_id or channel is None:
                    continue
                key = (binding["channel"], binding["conversation_id"])
                origin = key == (self.source.get("channel"), self.source.get("conversation_id"))
                desired[key] = (
                    channel,
                    ChannelActivity(
                        binding["conversation_id"],
                        True,
                        self.source.get("thread_id", "") if origin else "",
                        self.session_id,
                    ),
                )
            stopping = {key: value for key, value in self.current.items() if desired.get(key) != value}
            await asyncio.gather(
                *(_activity(channel, replace(event, active=False)) for channel, event in stopping.values())
            )
            for key in stopping:
                del self.current[key]
            # A newer binding/owner change gets its own reconciliation. Do not
            # dispatch stale starts after a potentially slow stop or DB read.
            if revision != self.revision:
                return
            starting = {key: value for key, value in desired.items() if self.current.get(key) != value}
            self.current.update(starting)
            await asyncio.gather(*(_activity(channel, event) for channel, event in starting.values()))

    async def close(self) -> None:
        self.session_id = ""
        self.source = {}
        await self.refresh()
