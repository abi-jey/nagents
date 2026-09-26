"""Transactional browser-upload bounds and immutable message attachment binding."""

from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING

from fastapi import HTTPException

from nagents.channels.runtime import INLINE_DOCUMENT_TYPES
from nagents.channels.runtime import INLINE_IMAGE_TYPES
from nagents.channels.runtime import MAX_INLINE_ATTACHMENTS
from nagents.channels.runtime import MAX_INLINE_ATTACHMENT_BYTES

if TYPE_CHECKING:
    import sqlite3

TYPES = tuple(sorted(INLINE_IMAGE_TYPES | INLINE_DOCUMENT_TYPES))
TTL = 3600
STAGED_BYTES = 128 * 1024 * 1024


def expire(db: sqlite3.Connection) -> None:
    db.execute("DELETE FROM ngn_web_uploads WHERE inbox_id = 0 AND expires_at <= ?", (time.time(),))


def scope(db: sqlite3.Connection, root: str) -> str:
    """A clear/delete invalidates in-flight uploads as well as committed drafts."""
    db.execute("INSERT OR IGNORE INTO ngn_web_upload_scopes VALUES (?, ?)", (root, uuid.uuid4().hex))
    return str(db.execute("SELECT generation FROM ngn_web_upload_scopes WHERE session_id = ?", (root,)).fetchone()[0])


def bind(db: sqlite3.Connection, root: str, inbox_id: int, ids: tuple[str, ...], supported: tuple[str, ...]) -> None:
    expire(db)
    if len(ids) > MAX_INLINE_ATTACHMENTS or len(set(ids)) != len(ids):
        raise HTTPException(422, "Choose at most three distinct attachments.")
    for position, id in enumerate(ids):
        row = db.execute(
            "SELECT session_id, media_type, inbox_id FROM ngn_web_uploads WHERE upload_id = ?", (id,)
        ).fetchone()
        if row is None or row[0] != root or row[2] != 0:
            raise HTTPException(
                409, "An attachment expired, was removed, or belongs to another submission. Attach it again."
            )
        if row[1] not in supported:
            raise HTTPException(415, "The selected provider/model does not declare support for this attachment type.")
        db.execute(
            "UPDATE ngn_web_uploads SET inbox_id = ?, position = ? WHERE upload_id = ?", (inbox_id, position, id)
        )
    if ids:
        prompt = str(db.execute("SELECT prompt FROM ngn_web_inbox WHERE id = ?", (inbox_id,)).fetchone()[0])
        names = [
            row[0]
            for row in db.execute(
                "SELECT filename FROM ngn_web_uploads WHERE inbox_id = ? ORDER BY position", (inbox_id,)
            )
        ]
        title = prompt if prompt.strip() else "Attachments: " + ", ".join(names)
        # Name the admitted turn before Harness synthesizes its untrusted-input
        # preamble. Internal wrapper text is not a useful conversation title.
        db.execute(
            "UPDATE harness_sessions SET title = ? WHERE id = ? AND title = ''", (" ".join(title.split())[:80], root)
        )


def stage(
    db: sqlite3.Connection, root: str, generation: str, id: str, filename: str, media_type: str, data: bytes
) -> dict[str, object]:
    if db.execute("SELECT generation FROM ngn_web_upload_scopes WHERE session_id = ?", (root,)).fetchone() != (
        generation,
    ):
        raise HTTPException(409, "Conversation was cleared or deleted during upload. Attach the file again.")
    expire(db)
    previous = db.execute(
        "SELECT session_id, filename, media_type, data, expires_at, inbox_id FROM ngn_web_uploads WHERE upload_id = ?",
        (id,),
    ).fetchone()
    if previous:
        if previous[:4] != (root, filename, media_type, data) or previous[5] != 0:
            raise HTTPException(409, "Upload identity already belongs to another attachment or submission.")
        expires = previous[4]
    else:
        if (
            db.execute(
                "SELECT count(*) FROM ngn_web_uploads WHERE session_id = ? AND inbox_id = 0", (root,)
            ).fetchone()[0]
            >= MAX_INLINE_ATTACHMENTS
        ):
            raise HTTPException(
                409, "This conversation already has three staged attachments. Remove one or wait for expiry."
            )
        total = db.execute("SELECT COALESCE(SUM(byte_length), 0) FROM ngn_web_uploads WHERE inbox_id = 0").fetchone()[0]
        if total + len(data) > STAGED_BYTES:
            raise HTTPException(429, "Staged upload storage is full. Remove unused drafts or retry after expiry.")
        if media_type not in TYPES or not 0 < len(data) <= MAX_INLINE_ATTACHMENT_BYTES:
            raise HTTPException(413, "Attachment exceeds the supported type or byte limit.")
        expires = time.time() + TTL
        db.execute(
            "INSERT INTO ngn_web_uploads VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0)",
            (id, root, filename, media_type, len(data), data, expires),
        )
    return {
        "upload_id": id,
        "session_id": root,
        "filename": filename,
        "media_type": media_type,
        "byte_length": len(data),
        "expires_at": expires,
    }
