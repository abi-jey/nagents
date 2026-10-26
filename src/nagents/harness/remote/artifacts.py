"""Owned history-backed image references; no remote URL fetches or blob duplication."""

from __future__ import annotations

import asyncio
import base64
import binascii
import re
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING
from urllib.parse import quote

from nagents._async import finish_on_cancel
from nagents.artifacts import MAX_ARTIFACT_IMAGES
from nagents.artifacts import MAX_ARTIFACT_IMAGE_BYTES
from nagents.artifacts import ImageArtifact
from nagents.artifacts import ImageArtifactReference
from nagents.session.manager import SessionManager
from nagents.types import ImageContent

if TYPE_CHECKING:
    from pathlib import Path


def image_bytes(part: ImageContent) -> bytes:
    try:
        if len(part.base64_data) > (MAX_ARTIFACT_IMAGE_BYTES + 2) // 3 * 4:
            raise ValueError("Image artifact exceeds 8 MiB")
        data = base64.b64decode(part.base64_data, validate=True)
        ImageArtifact(part.artifact_id, part.artifact_part, data, part.media_type, part.filename, part.caption)
        return data
    except (ValueError, TypeError, binascii.Error):
        raise ValueError("Stored image artifact is invalid or exceeds its limit") from None


def image_references(content: object, root: str, history_id: int) -> tuple[ImageArtifactReference, ...]:
    if not isinstance(content, list) or history_id <= 0:
        return ()
    references: list[ImageArtifactReference] = []
    size = 0
    for index, part in enumerate(content):
        if not isinstance(part, ImageContent) or not part.artifact_id:
            continue
        data = image_bytes(part)
        size += len(data)
        if len(references) >= MAX_ARTIFACT_IMAGES or size > MAX_ARTIFACT_IMAGE_BYTES:
            raise ValueError("Image artifacts exceed three images or 8 MiB in total")
        references.append(
            ImageArtifactReference(
                part.artifact_id,
                part.filename,
                part.media_type,
                len(data),
                f"/api/sessions/{quote(root, safe='')}/artifacts/{history_id}/{index}",
                part.artifact_part,
                part.caption.encode("utf-8")[:256].decode("utf-8", errors="ignore"),
            )
        )
    return tuple(references)


async def referenced_images(
    path: Path, root: str, actor: str, references: tuple[ImageArtifactReference, ...]
) -> list[ImageContent]:
    """Read only the exact immediate child's committed parts for parent vision.

    A path is display data, not authority: the caller supplies the host-owned
    private actor separately and it must match the stored message's session.
    """
    prefix = f"/api/sessions/{quote(root, safe='')}/artifacts/"

    def read() -> list[ImageContent]:
        result: list[ImageContent] = []
        with closing(sqlite3.connect(path, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            for reference in references:
                location = reference.path.removeprefix(prefix)
                if not reference.path.startswith(prefix) or not re.fullmatch(r"[1-9][0-9]*/[0-9]+", location):
                    raise ValueError("Image artifact does not belong to this conversation")
                history, index = map(int, location.split("/"))
                row = db.execute(
                    "SELECT m.* FROM v2_messages m WHERE m.id = ? AND m.session_id = ? AND (m.session_id = ? OR EXISTS ("
                    "SELECT 1 FROM nagents_agent_sessions a WHERE a.storage_id = m.session_id AND a.root_session_id = ? "
                    "AND NOT EXISTS (SELECT 1 FROM harness_sessions h WHERE h.id = m.session_id)))",
                    (history, actor, root, root),
                ).fetchone()
                if row is None:
                    raise ValueError("Image artifact history is unavailable")
                message = SessionManager(path)._row_to_message(row)
                if not isinstance(message.content, list) or not 0 <= index < len(message.content):
                    raise ValueError("Image artifact part is unavailable")
                part = message.content[index]
                if not isinstance(part, ImageContent) or (
                    part.artifact_id,
                    part.artifact_part,
                    part.filename,
                    part.media_type,
                ) != (reference.artifact_id, reference.artifact_part, reference.filename, reference.media_type):
                    raise ValueError("Image artifact identity is inconsistent")
                if len(image_bytes(part)) != reference.byte_length:
                    raise ValueError("Image artifact byte length is inconsistent")
                result.append(part)
        return result

    return await finish_on_cancel(asyncio.to_thread(read))
