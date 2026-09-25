"""First-party, host-managed terminal delivery and owned transcript reads."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import ExitStack
from contextlib import closing
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from nagents._async import join_owned
from nagents.channels.dispatcher import ChannelDispatcher
from nagents.channels.local_delivery import LocalDeliveryService
from nagents.channels.types import Channel
from nagents.channels.types import ChannelContentCapabilities
from nagents.channels.types import ChannelError
from nagents.channels.types import ChannelReceiveCapabilities
from nagents.channels.types import ChannelRenderCapabilities
from nagents.channels.types import ChannelSendCapabilities
from nagents.extensions import AgentPlugin
from nagents.harness.execution import bind_channel_send
from nagents.harness.execution import delivery_origin
from nagents.harness.runtime import _HarnessSession
from nagents.session.deliveries import DeliveryJournal
from nagents.types import Message

if TYPE_CHECKING:
    from nagents.channels.delivery_types import DeliveryHistory
    from nagents.channels.local_delivery import DeliveryNotifier
    from nagents.channels.types import ChannelDelivery
    from nagents.channels.types import ChannelReceiver
    from nagents.channels.types import ChannelSend
    from nagents.extensions import ModelRequest
    from nagents.extensions import RunContext
    from nagents.harness import Harness

TEXT = ChannelSendCapabilities(text=True)


class _Instructions(AgentPlugin):
    async def before_model(self, context: RunContext, request: ModelRequest) -> ModelRequest:
        request.messages.insert(
            0,
            Message(
                role="system",
                content=(
                    "Ordinary replies already appear in this terminal. For an explicitly requested text delivery, "
                    f"use channel_send with channel builtin.tui and destination {context.session_id!r}. "
                    "Files, thread_id and reply_to are unsupported. Discover capabilities with channel_list."
                ),
            ),
        )
        return request


class TuiChannel(Channel):
    name = "builtin.tui"
    description = "Explicit text delivery to this terminal. Ordinary replies already appear here."
    capabilities = ("send_text",)
    content_capabilities = ChannelContentCapabilities(
        receive=ChannelReceiveCapabilities(via="host", text=True),
        send=TEXT,
        render=ChannelRenderCapabilities(text=True),
    )

    def __init__(self, service: LocalDeliveryService, notify: DeliveryNotifier) -> None:
        self.service = service
        self.notify = notify

    async def listen(self, receive: ChannelReceiver) -> None:
        raise ChannelError("builtin.tui input is host-managed; Channel.listen() is unsupported")

    async def send(self, message: ChannelSend) -> ChannelDelivery:
        return await self.service.send(self.name, message, TEXT, notify=self.notify)


@dataclass(frozen=True)
class TranscriptSnapshot:
    rows: tuple[tuple[int, Message], ...]
    deliveries: DeliveryHistory


class TuiChannelHost:
    """Registration lifetime belongs to the app, never to a listener or selection."""

    def __init__(self, harness: Harness, notify: DeliveryNotifier) -> None:
        self.harness = harness
        self._owned = ExitStack()
        self.journal = DeliveryJournal(harness.agent.session.db_path)
        self.channel = TuiChannel(LocalDeliveryService(self.journal, partial(delivery_origin, harness)), notify)

    def open(self) -> None:
        agent = self.harness.agent
        if self.channel.name in agent._channels or any(
            channel.name == self.channel.name for channel in agent._channels.values()
        ):
            raise ChannelError("Channel name collision: builtin.tui")
        dispatcher = ChannelDispatcher((self.channel,), workspace=self.harness.workspace)
        with ExitStack() as owned:
            definitions = owned.enter_context(dispatcher.register_tools(agent.tool_registry))
            definition = next(tool for tool in definitions if tool.name == "channel_send")
            owned.enter_context(bind_channel_send(self.harness, definition))
            instructions = _Instructions()
            agent.plugins.append(instructions)
            owned.callback(lambda: agent.plugins.remove(instructions) if instructions in agent.plugins else None)
            self._owned = owned.pop_all()

    def close(self) -> None:
        self._owned.close()

    async def snapshot(self, session_id: str) -> TranscriptSnapshot:
        # Deliberately exact type: custom adapters keep their ordinary history
        # contract and cannot accidentally opt into SQLite row identity.
        adapter = self.harness.agent.session
        if type(adapter) is not _HarnessSession:
            raise ChannelError("This persistence adapter does not support terminal delivery history")
        await self.journal.initialize()

        def read() -> TranscriptSnapshot:
            with closing(sqlite3.connect(adapter.db_path)) as db:
                db.execute("BEGIN")
                deliveries = self.journal._history_in(db, session_id)
                db.row_factory = sqlite3.Row
                rows = db.execute(
                    "SELECT * FROM v2_messages WHERE session_id = ? AND id >= ? ORDER BY id",
                    (session_id, deliveries.boundary),
                ).fetchall()
                return TranscriptSnapshot(tuple((row["id"], adapter._row_to_message(row)) for row in rows), deliveries)

        return await join_owned(asyncio.create_task(asyncio.to_thread(read)))

    @property
    def supports_history(self) -> bool:
        return type(self.harness.agent.session) is _HarnessSession
