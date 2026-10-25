"""Current-task image delivery for the ngn A2A exporter, under normal approvals."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from nagents.artifacts import IMAGE_ARTIFACT_TYPES
from nagents.artifacts import MAX_ARTIFACT_IMAGES
from nagents.artifacts import MAX_ARTIFACT_IMAGE_BYTES
from nagents.artifacts import ImageArtifact
from nagents.channels.types import Channel
from nagents.channels.types import ChannelContentCapabilities
from nagents.channels.types import ChannelDelivery
from nagents.channels.types import ChannelError
from nagents.channels.types import ChannelSendCapabilities

from .images import _active_publisher

if TYPE_CHECKING:
    from nagents.channels.types import ChannelReceiver
    from nagents.channels.types import ChannelSend


class A2AOutputChannel(Channel):
    name = "builtin.a2a"
    description = (
        "Publish PNG/JPEG files to this A2A task. Use destination 'current' and empty text; "
        "put captions in your normal assistant reply. File access and sends require normal host approval."
    )
    capabilities = ("send_files",)
    content_capabilities = ChannelContentCapabilities(
        send=ChannelSendCapabilities(
            text=False,
            file_media_types=tuple(sorted(IMAGE_ARTIFACT_TYPES)),
            max_files=MAX_ARTIFACT_IMAGES,
            max_file_bytes=MAX_ARTIFACT_IMAGE_BYTES,
            max_total_bytes=MAX_ARTIFACT_IMAGE_BYTES,
        )
    )

    async def listen(self, receive: ChannelReceiver) -> None:
        raise ChannelError("A2A input is owned by its authenticated request handler")

    async def send(self, message: ChannelSend) -> ChannelDelivery:
        if (
            message.destination != "current"
            or message.thread_id
            or message.reply_to
            or message.text
            or not message.files
        ):
            raise ChannelError(
                "A2A image delivery requires files, destination 'current', and empty text/thread/reply fields"
            )
        images = tuple(
            ImageArtifact(uuid.uuid4().hex, 0, file.data, file.media_type, file.filename) for file in message.files
        )
        return ChannelDelivery(await _active_publisher().publish_batch(images))
