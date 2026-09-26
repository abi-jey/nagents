"""Host-managed browser delivery; bytes travel only through authenticated assets."""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING

from fastapi import HTTPException
from starlette.responses import Response

from nagents.channels.dispatcher import MAX_OUTBOUND_FILES
from nagents.channels.dispatcher import MAX_OUTBOUND_FILE_BYTES
from nagents.channels.dispatcher import MAX_OUTBOUND_TOTAL_BYTES
from nagents.channels.local_delivery import LocalDeliveryService
from nagents.channels.runtime import INLINE_DOCUMENT_TYPES
from nagents.channels.runtime import INLINE_IMAGE_TYPES
from nagents.channels.runtime import MAX_INLINE_ATTACHMENT_BYTES
from nagents.channels.types import Channel
from nagents.channels.types import ChannelContentCapabilities
from nagents.channels.types import ChannelError
from nagents.channels.types import ChannelReceiveCapabilities
from nagents.channels.types import ChannelRenderCapabilities
from nagents.channels.types import ChannelSendCapabilities
from nagents.harness.execution import delivery_origin
from nagents.session.deliveries import DeliveryJournal

if TYPE_CHECKING:
    from collections.abc import Callable

    from fastapi import FastAPI

    from nagents.channels.delivery_types import DeliveryOrigin
    from nagents.channels.delivery_types import DeliveryReceipt
    from nagents.channels.types import ChannelDelivery
    from nagents.channels.types import ChannelReceiver
    from nagents.channels.types import ChannelSend
    from nagents.channels.types import ChannelValue

    from .service import WebState

MEDIA_TYPES = (
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "audio/mpeg",
    "audio/wav",
    "audio/ogg",
    "video/mp4",
    "video/webm",
)
SEND = ChannelSendCapabilities(
    text=True,
    file_media_types=MEDIA_TYPES,
    max_files=MAX_OUTBOUND_FILES,
    max_file_bytes=MAX_OUTBOUND_FILE_BYTES,
    max_total_bytes=MAX_OUTBOUND_TOTAL_BYTES,
)


def valid_media(media_type: str, data: bytes) -> bool:
    """Check the declared container, not codecs or universal browser decodability.

    In particular a renamed HTML/SVG document is never served as an active format.
    Native decoders still treat these bytes as untrusted and may reject the file.
    """
    if media_type == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if media_type == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if media_type == "image/gif":
        return data.startswith((b"GIF87a", b"GIF89a"))
    if media_type in {"image/webp", "audio/wav"}:
        return data.startswith(b"RIFF") and data[8:12] == (b"WEBP" if media_type == "image/webp" else b"WAVE")
    if media_type == "audio/mpeg":
        return data.startswith(b"ID3") or (len(data) >= 2 and data[0] == 255 and data[1] & 0xE0 == 0xE0)
    if media_type == "audio/ogg":
        return data.startswith(b"OggS")
    if media_type == "video/mp4":
        return len(data) >= 12 and data[4:8] == b"ftyp"
    if media_type == "video/webm":
        return data.startswith(b"\x1a\x45\xdf\xa3") and b"webm" in data[:4096]
    return False


def presentation(receipt: DeliveryReceipt, *, earlier: bool = False) -> dict[str, object]:
    return {
        "delivery_id": receipt.delivery_id,
        "session_id": receipt.origin.root_session_id,
        "channel": receipt.channel,
        "text": receipt.text,
        "sequence": receipt.sequence,
        "anchor_message_id": str(receipt.origin.anchor_message_id),
        "call_position": receipt.origin.call_position,
        "earlier": earlier,
        "assets": [asdict(asset) for asset in receipt.assets],
    }


class WebChannel(Channel):
    name = "builtin.web"
    description = (
        "Explicit text, image, audio or video delivery to this conversation. Ordinary replies already appear here."
    )
    capabilities = ("send_text", "send_files")
    content_capabilities = ChannelContentCapabilities(
        receive=ChannelReceiveCapabilities(
            via="host",
            text=True,
            file_media_types=tuple(sorted(INLINE_IMAGE_TYPES | INLINE_DOCUMENT_TYPES)),
            max_files=3,
            max_file_bytes=MAX_INLINE_ATTACHMENT_BYTES,
        ),
        send=SEND,
        render=ChannelRenderCapabilities(text=True, file_media_types=MEDIA_TYPES, playback="browser_dependent"),
    )

    def __init__(self, state: WebState) -> None:
        self.state = state
        self.journal = DeliveryJournal(state.harness.agent.session.db_path)
        self.service = LocalDeliveryService(self.journal, self.origin)

    def origin(self, channel: str, destination: str) -> DeliveryOrigin:
        origin = delivery_origin(self.state.running_harness, channel, destination)
        run = self.state.active
        if run is None or run.finished or (run.id, run.session_id) != (origin.host_run_id, origin.root_session_id):
            raise PermissionError("Inactive web execution")
        return origin

    async def listen(self, receive: ChannelReceiver) -> None:
        raise ChannelError("builtin.web input is host-managed; Channel.listen() is unsupported")

    async def notify(self, notice: dict[str, ChannelValue]) -> None:
        run = self.state.active
        if run is not None and run.session_id == notice["session_id"]:
            await self.state.send(run, {"event": "local_delivery", **notice})

    async def send(self, message: ChannelSend) -> ChannelDelivery:
        if await self.state.channels.execution_owner() is not None:
            raise ChannelError("Local delivery is unavailable in externally owned conversations")
        for file in message.files:
            if not valid_media(file.media_type, file.data):
                raise ChannelError("Unsupported or mismatched media container")
        return await self.service.send(self.name, message, SEND, notify=self.notify)


def register_assets(app: FastAPI, get: Callable[[], WebState]) -> None:
    @app.get("/api/sessions/{session_id}/deliveries/{delivery_id}/assets/{asset_id}")
    async def asset(session_id: str, delivery_id: str, asset_id: str) -> Response:
        journal = get().channels.local.journal
        try:
            history = await journal.history(session_id)
            assets = [
                asset
                for receipt in (*history.earlier, *history.current)
                if receipt.delivery_id == delivery_id
                for asset in receipt.assets
                if asset.asset_id == asset_id
            ]
            if len(assets) != 1 or assets[0].media_type not in MEDIA_TYPES:
                raise ChannelError("Unavailable asset")
            data = await journal.read_asset(session_id, delivery_id, asset_id)
            if not valid_media(assets[0].media_type, data):
                raise ChannelError("Invalid media")
        except ChannelError:
            raise HTTPException(404, "Delivery asset not found in this active conversation.") from None
        return Response(data, media_type=assets[0].media_type, headers={"X-Content-Type-Options": "nosniff"})
