"""Bounded image outputs and metadata-only references, without transport dependencies."""

from __future__ import annotations

import base64
import unicodedata
from dataclasses import dataclass
from typing import Literal

from .types import ImageContent

IMAGE_ARTIFACT_TYPES = frozenset({"image/png", "image/jpeg"})
MAX_ARTIFACT_IMAGE_BYTES = 8 * 1024 * 1024
MAX_ARTIFACT_IMAGES = 3


def validate_artifact_image(data: bytes, media_type: str, filename: str) -> None:
    """Check bounded raster containers; native clients perform final decoding.

    SVG, HTML, URLs and arbitrary file formats are deliberately not image
    artifacts. Checking an encoded container does not execute or decode it.
    """
    if media_type not in IMAGE_ARTIFACT_TYPES:
        raise ValueError("Image artifacts support image/png and image/jpeg only")
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_ARTIFACT_IMAGE_BYTES:
        raise ValueError("An image artifact requires nonempty bytes, at most 8 MiB")
    if (
        not isinstance(filename, str)
        or not filename
        or len(filename) > 255
        or filename in {".", ".."}
        or any(character in "/\\" or unicodedata.category(character) in {"Cc", "Cs"} for character in filename)
    ):
        raise ValueError("Image artifact filename must be one component of at most 255 characters")
    if media_type == "image/png":
        valid = (
            len(data) >= 45
            and data.startswith(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
            and 0 < int.from_bytes(data[16:20], "big") <= 16384
            and 0 < int.from_bytes(data[20:24], "big") <= 16384
            and data.endswith(b"\x00\x00\x00\x00IEND\xaeB`\x82")
        )
    else:
        valid = len(data) >= 12 and data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9")
    if not valid:
        raise ValueError("Image artifact is malformed, incomplete, or does not match its media type")


@dataclass(frozen=True)
class ImageArtifact:
    """One complete image part; artifact identity remains scoped to its task."""

    artifact_id: str
    artifact_part: int
    data: bytes
    media_type: str
    filename: str
    caption: str = ""

    def __post_init__(self) -> None:
        if (
            not isinstance(self.artifact_id, str)
            or not 0 < len(self.artifact_id.encode("utf-8")) <= 256
            or any(unicodedata.category(character) in {"Cc", "Cs"} for character in self.artifact_id)
        ):
            raise ValueError("An image artifact requires a bounded artifact ID")
        if type(self.artifact_part) is not int or not 0 <= self.artifact_part < 256:
            raise ValueError("Image artifact part index must be from 0 to 255")
        if not isinstance(self.caption, str) or len(self.caption.encode("utf-8")) > 1048576:
            raise ValueError("Image artifact caption exceeds the supported text limit")
        validate_artifact_image(self.data, self.media_type, self.filename)

    def content(self) -> ImageContent:
        """An ordinary provider input; transport bytes never pass through model output."""
        return ImageContent(
            base64_data=base64.b64encode(self.data).decode("ascii"),
            media_type=self.media_type,
            filename=self.filename,
            artifact_id=self.artifact_id,
            artifact_part=self.artifact_part,
            caption=self.caption,
        )


@dataclass(frozen=True)
class ImageArtifactReference:
    """Display metadata for an authenticated host asset, never a remote URL."""

    artifact_id: str
    filename: str
    media_type: str
    byte_length: int
    path: str
    artifact_part: int = 0
    caption: str = ""
    type: Literal["artifact_image"] = "artifact_image"
