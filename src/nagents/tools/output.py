"""Native image tool outputs with a safe terminal representation."""

import base64
import binascii
import os
import tempfile
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

from nagents.types import ImageContent


@dataclass(frozen=True)
class ToolOutput:
    """Separate provider image inputs from text intended for display."""

    text: str = ""
    images: tuple[ImageContent, ...] = field(default=(), repr=False)

    def __str__(self) -> str:
        return "\n".join([self.text, *(f"[Image: {image.media_type}]" for image in self.images)]).strip()

    def save_for_display(self, directory: Path) -> str:
        """Save private, uniquely named raster files and return browser-openable URIs.

        Names and extensions are host-owned, never supplied by an MCP server.
        Saving is explicit rather than a side effect of string formatting.
        """
        lines = [self.text] if self.text else []
        extensions = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}
        for image in self.images:
            try:
                extension = extensions[image.media_type]
                if len(image.base64_data) > 12 * 1024 * 1024:
                    raise ValueError("Image too large")
                data = base64.b64decode(image.base64_data, validate=True)
                if not data or len(data) > 8 * 1024 * 1024:
                    raise ValueError("Image too large or empty")
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                descriptor, filename = tempfile.mkstemp(prefix="image-", suffix=extension, dir=directory)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(data)
                lines.append(f"[Image: {image.media_type}] {Path(filename).resolve().as_uri()}")
            except (OSError, ValueError, KeyError, binascii.Error):
                lines.append(f"[Image: {image.media_type}] (local file unavailable)")
        return "\n".join(lines)
