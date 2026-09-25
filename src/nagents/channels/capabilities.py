"""Bounded directional content declarations and pre-dispatch send validation."""

import json
import re

from .types import ChannelContentCapabilities
from .types import ChannelError
from .types import ChannelReceiveCapabilities
from .types import ChannelRenderCapabilities
from .types import ChannelSend
from .types import ChannelSendCapabilities
from .types import ChannelValue

_MIME = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*\Z", re.IGNORECASE | re.ASCII)


def _media(text: bool, media_types: tuple[str, ...]) -> dict[str, ChannelValue]:
    if type(text) is not bool:
        raise ChannelError("Content capability text must be a boolean")
    if type(media_types) is not tuple or len(media_types) > 32:
        raise ChannelError("Content capability file_media_types must be a tuple of at most 32 MIME types")
    values: list[ChannelValue] = []
    seen: set[str] = set()
    for media_type in media_types:
        if type(media_type) is not str or len(media_type) > 127 or not _MIME.fullmatch(media_type):
            raise ChannelError("Invalid content capability MIME type")
        normalized = media_type.lower()
        if normalized in seen:
            raise ChannelError("Duplicate content capability MIME type")
        seen.add(normalized)
        values.append(normalized)
    return {"text": text, "file_media_types": values}


def _limits(value: ChannelReceiveCapabilities | ChannelSendCapabilities) -> dict[str, ChannelValue]:
    result: dict[str, ChannelValue] = {}
    for name, limit in (
        ("max_files", value.max_files),
        ("max_file_bytes", value.max_file_bytes),
        ("max_total_bytes", value.max_total_bytes),
    ):
        if limit is not None:
            if type(limit) is not int or not 0 < limit <= 2**63 - 1:
                raise ChannelError("Content capability limits must be positive 64-bit integers")
            result[name] = limit
    return result


def content_capabilities_catalog(value: ChannelContentCapabilities) -> dict[str, ChannelValue]:
    """Validate an opt-in declaration and return detached, finite JSON data.

    Fixed typed fields avoid arbitrary metadata and accidental authority claims.
    Validation occurs when a host binds a connector, before it performs I/O.
    """
    if type(value) is not ChannelContentCapabilities:
        raise ChannelError("Invalid channel content capabilities")
    result: dict[str, ChannelValue] = {}
    if value.receive is not None:
        receive = value.receive
        if type(receive) is not ChannelReceiveCapabilities:
            raise ChannelError("Invalid receive content capabilities")
        if type(receive.via) is not str or receive.via not in ("host", "listen"):
            raise ChannelError("Content capability receive.via must be host or listen")
        result["receive"] = {"via": receive.via, **_media(receive.text, receive.file_media_types), **_limits(receive)}
    if value.send is not None:
        send = value.send
        if type(send) is not ChannelSendCapabilities:
            raise ChannelError("Invalid send content capabilities")
        result["send"] = {**_media(send.text, send.file_media_types), **_limits(send)}
    if value.render is not None:
        render = value.render
        if type(render) is not ChannelRenderCapabilities:
            raise ChannelError("Invalid render content capabilities")
        if type(render.playback) is not str or render.playback not in ("none", "browser_dependent"):
            raise ChannelError("Invalid content capability playback")
        result["render"] = {**_media(render.text, render.file_media_types), "playback": render.playback}
    if len(json.dumps(result, ensure_ascii=True, separators=(",", ":")).encode("utf-8")) > 8192:
        raise ChannelError("Channel content capabilities exceed 8 KiB")
    return result


def validate_content_send(descriptor: dict[str, ChannelValue], message: ChannelSend) -> None:
    """Check a bound catalog snapshot before dispatch, without delivering a subset.

    Missing send declarations leave legacy connector behavior intact. Explicit
    declarations can reject unsupported content but never grant permission or
    relax host limits. Receive/render declarations do not authorize sends.
    """
    send = descriptor.get("send")
    if send is None:
        return
    if not isinstance(send, dict):
        raise ChannelError("Invalid send content capabilities")
    if message.text and send.get("text") is not True:
        raise ChannelError("Channel does not support text delivery")
    if message.attachments:
        raise ChannelError("Declared content capabilities require local files, not attachment references")
    media_types = send.get("file_media_types", [])
    if not isinstance(media_types, list):
        raise ChannelError("Invalid send content capabilities")
    for file in message.files:
        if file.media_type.lower() not in media_types:
            raise ChannelError("Channel does not support an attachment media type")
    sizes = [len(file.data) for file in message.files]
    measures = {"max_files": len(sizes), "max_file_bytes": max(sizes, default=0), "max_total_bytes": sum(sizes)}
    for name, actual in measures.items():
        limit = send.get(name)
        if isinstance(limit, int) and actual > limit:
            raise ChannelError(f"Channel attachment limit exceeded: {name}")
