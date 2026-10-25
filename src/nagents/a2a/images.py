"""Host-owned image publication from an approved tool, without model-encoded bytes."""

from __future__ import annotations

import asyncio
import uuid
from contextvars import ContextVar
from typing import TYPE_CHECKING

from nagents._async import finish_on_cancel
from nagents.artifacts import MAX_ARTIFACT_IMAGES
from nagents.artifacts import MAX_ARTIFACT_IMAGE_BYTES
from nagents.artifacts import ImageArtifact

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from collections.abc import Callable
    from pathlib import Path


class ImagePublisher:
    def __init__(
        self,
        sink: Callable[[ImageArtifact], Awaitable[None]],
        consume: Callable[[int], None],
        modes: Callable[[], tuple[str, ...]],
    ) -> None:
        self.sink, self.consume, self.modes = sink, consume, modes
        self.active = True
        self.count = 0
        self.bytes = 0
        self.identifiers: set[str] = set()

    async def publish(self, data: bytes, media_type: str, filename: str, caption: str) -> str:
        image = ImageArtifact(uuid.uuid4().hex, 1 if caption else 0, data, media_type, filename, caption)
        return (await self.publish_batch((image,)))[0]

    async def publish_batch(self, images: tuple[ImageArtifact, ...]) -> tuple[str, ...]:
        """Reserve a complete validated file batch before publishing its first part."""
        if not self.active:
            raise RuntimeError("The A2A image publisher is no longer active")
        if not images:
            raise ValueError("Image publication requires at least one image")
        modes = self.modes()
        if modes and any(image.media_type not in modes for image in images):
            raise ValueError("The client did not accept this image output mode")
        size = sum(len(image.data) for image in images)
        if self.count + len(images) > MAX_ARTIFACT_IMAGES or self.bytes + size > MAX_ARTIFACT_IMAGE_BYTES:
            raise ValueError("Image output exceeds three images or 8 MiB in total")
        identifiers = tuple(image.artifact_id for image in images)
        if len(set(identifiers)) != len(identifiers) or self.identifiers.intersection(identifiers):
            raise ValueError("An image artifact ID cannot be republished in this task")
        # Shared text/image accounting is synchronous before the queue await.
        self.consume(size + sum(len(image.caption.encode("utf-8")) for image in images))
        self.count += len(images)
        self.bytes += size
        self.identifiers.update(identifiers)
        for image in images:
            await self.sink(image)
        return identifiers


current_image_publisher: ContextVar[ImagePublisher] = ContextVar("nagents_a2a_image_publisher")


def _active_publisher() -> ImagePublisher:
    try:
        publisher = current_image_publisher.get()
    except LookupError:
        raise RuntimeError("Image publication requires an active A2A server execution") from None
    if not publisher.active:
        raise RuntimeError("The A2A image publisher is no longer active")
    return publisher


async def publish_image(data: bytes, *, media_type: str, filename: str, caption: str = "") -> str:
    """Publish complete PNG/JPEG bytes from a trusted runtime's approved tool.

    This does not authorize file access or another tool operation. The host's
    normal tool executor, approvals and read policy remain authoritative.
    Returns a compact artifact ID; bytes never enter the model's tool result.
    """
    return await _active_publisher().publish(data, media_type, filename, caption)


async def publish_image_file(path: Path, *, media_type: str, caption: str = "") -> str:
    """Read an already host-approved file with a bounded read, then publish it.

    Calling this helper grants no filesystem permission. Embeddings must review
    or otherwise authorize the exact file before passing it here.
    """
    publisher = _active_publisher()

    def read() -> bytes:
        with path.open("rb") as stream:
            return stream.read(MAX_ARTIFACT_IMAGE_BYTES + 1)

    data = await finish_on_cancel(asyncio.to_thread(read))
    return await publisher.publish(data, media_type, path.name, caption)
