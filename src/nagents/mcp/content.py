"""Convert MCP image blocks without exposing binary payloads as text."""

import base64
import binascii
from collections.abc import Mapping
from collections.abc import Sequence

from nagents.tools.output import ToolOutput
from nagents.types import ImageContent

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGES = 3


def parse_tool_content(content: Sequence[Mapping[str, object]]) -> str | ToolOutput:
    """Retain bounded inline raster images; never fetch resource URLs."""
    texts: list[str] = []
    images: list[ImageContent] = []
    total = 0
    for item in content:
        if item.get("type") == "text":
            text = item.get("text", "")
            if isinstance(text, str):
                texts.append(text)
        elif item.get("type") == "image":
            media = item.get("mimeType")
            encoded = item.get("data")
            if media not in ("image/png", "image/jpeg", "image/webp", "image/gif"):
                texts.append("[Image omitted: unsupported media type]")
                continue
            if (
                len(images) >= MAX_IMAGES
                or not isinstance(encoded, str)
                or len(encoded) > (MAX_IMAGE_BYTES * 4 // 3 + 4)
            ):
                texts.append("[Image omitted: invalid data or image budget exceeded]")
                continue
            try:
                data = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error):
                texts.append("[Image omitted: invalid base64 data]")
                continue
            valid = (
                (media == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n"))
                or (media == "image/jpeg" and data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9"))
                or (media == "image/gif" and data[:6] in (b"GIF87a", b"GIF89a"))
                or (media == "image/webp" and data.startswith(b"RIFF") and data[8:12] == b"WEBP")
            )
            if not valid or total + len(data) > MAX_IMAGE_BYTES:
                texts.append("[Image omitted: malformed image or image budget exceeded]")
                continue
            total += len(data)
            images.append(ImageContent(base64_data=encoded, media_type=str(media)))
    text = "\n".join(texts)
    return ToolOutput(text=text, images=tuple(images)) if images else text
