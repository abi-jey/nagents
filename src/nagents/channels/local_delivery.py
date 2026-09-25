"""Host-owned local delivery, separating live authority from durable storage."""

import asyncio
from collections.abc import Awaitable
from collections.abc import Callable

from nagents._async import join_owned
from nagents.session.deliveries import DeliveryJournal

from .delivery_types import DeliveryOrigin
from .types import ChannelDelivery
from .types import ChannelError
from .types import ChannelSend
from .types import ChannelSendCapabilities
from .types import ChannelValue

DeliveryAuthority = Callable[[str, str], DeliveryOrigin]
DeliveryNotifier = Callable[[dict[str, ChannelValue]], Awaitable[None]]
NOTIFICATION_TIMEOUT = 2.0


async def discard_notification(notice: dict[str, ChannelValue]) -> None:
    """A durable delivery is recoverable even when no presentation is attached."""


class LocalDeliveryService:
    """Connect an execution-bound authority to a journal without starting listeners.

    Trusted hosts supply an authority backed by their approved invocation bridge,
    never by tool arguments or an inherited context variable alone. This service
    does not register channels, grant approval or infer an executing session.
    """

    def __init__(self, journal: DeliveryJournal, authority: DeliveryAuthority) -> None:
        self.journal = journal
        self.authority = authority

    async def send(
        self,
        channel: str,
        message: ChannelSend,
        capabilities: ChannelSendCapabilities,
        *,
        notify: DeliveryNotifier = discard_notification,
    ) -> ChannelDelivery:
        """Commit once, then publish a small best-effort invalidation notice.

        Cancellation joins the owned commit. If commit succeeded its receipt and
        identity remain durable even when cancellation prevents a returned tool
        result. Notification failure never makes a known commit a failed send.
        Observers must cooperate with cancellation, like other channel hooks.
        """
        try:
            origin = self.authority(channel, message.destination)
            prepared = self.journal.prepare(origin, channel, message, capabilities)
            # No await separates these checks and handoff to the owned commit.
            # The journal rechecks durable state in its write transaction.
            if self.authority(channel, message.destination) != origin:
                raise ChannelError("Local delivery execution changed before commit")
        except ChannelError:
            raise
        except Exception:
            # The authority is a synchronous, side-effect-free host check. A
            # PermissionError here is a known rejection, not an uncertain send.
            raise ChannelError("Local delivery execution is unavailable") from None
        receipt = await prepared.commit()

        async def publish() -> None:
            try:
                async with asyncio.timeout(NOTIFICATION_TIMEOUT):
                    await notify(receipt.notification())
            except (asyncio.CancelledError, Exception):
                # This is an isolated observer task. Owner cancellation is joined
                # and propagated by join_owned; observer self-cancellation is not.
                pass

        await join_owned(asyncio.create_task(publish()))
        return receipt.channel_delivery()
