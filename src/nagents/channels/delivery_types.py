"""Host-derived identities and metadata-only records for local deliveries."""

from dataclasses import dataclass

from .types import ChannelDelivery
from .types import ChannelValue


@dataclass(frozen=True)
class DeliveryOrigin:
    """Durable correlation captured by the owned execution/assistant-write bridge.

    Values are data, not execution authority. A host must validate its live
    invocation before using an origin to commit a local delivery.
    """

    root_session_id: str
    actor_session_id: str
    host_run_id: str
    turn_id: str
    task_id: str
    activation: int
    invocation_id: str
    anchor_message_id: int
    call_position: int
    call_id: str
    tool_name: str


@dataclass(frozen=True)
class DeliveryAsset:
    """An opaque attachment reference; no content bytes in presentation metadata."""

    asset_id: str
    position: int
    filename: str
    media_type: str
    byte_length: int


@dataclass(frozen=True)
class DeliveryReceipt:
    """One committed local delivery, independently identifiable across reloads."""

    delivery_id: str
    sequence: int
    channel: str
    origin: DeliveryOrigin
    text: str
    created_at: str
    assets: tuple[DeliveryAsset, ...] = ()

    def channel_delivery(self) -> ChannelDelivery:
        """Return a compact tool receipt, without duplicating text or media bytes."""
        return ChannelDelivery(
            (self.delivery_id,),
            metadata={
                "delivery_id": self.delivery_id,
                "channel": self.channel,
                "destination": self.origin.root_session_id,
                "asset_ids": [asset.asset_id for asset in self.assets],
            },
        )

    def notification(self) -> dict[str, ChannelValue]:
        """Return a small invalidation notice; hosts retrieve content separately.

        Even a large text delivery must not exceed a bounded replay/subscription
        queue's budget. Repeated notices identify the same committed delivery.
        """
        return {
            "delivery_id": self.delivery_id,
            "session_id": self.origin.root_session_id,
            "channel": self.channel,
            "sequence": self.sequence,
        }


@dataclass(frozen=True)
class DeliveryHistory:
    """Stable anchor-based groups; rendering belongs to the owning interface."""

    boundary: int
    earlier: tuple[DeliveryReceipt, ...] = ()
    current: tuple[DeliveryReceipt, ...] = ()
