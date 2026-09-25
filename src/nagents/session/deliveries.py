"""Owned, atomic SQLite storage for explicit local channel sends.

Preparation is synchronous and detached; it grants no execution authority.
The host validates the live invocation. Storage validates durable membership
and the exact assistant anchor again inside the write transaction.
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from contextlib import closing
from contextlib import suppress
from dataclasses import astuple
from dataclasses import replace
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from nagents._async import join_owned
from nagents.channels.capabilities import content_capabilities_catalog
from nagents.channels.capabilities import validate_content_send
from nagents.channels.delivery_types import DeliveryAsset
from nagents.channels.delivery_types import DeliveryHistory
from nagents.channels.delivery_types import DeliveryOrigin
from nagents.channels.delivery_types import DeliveryReceipt
from nagents.channels.dispatcher import MAX_OUTBOUND_FILES
from nagents.channels.dispatcher import MAX_OUTBOUND_FILE_BYTES
from nagents.channels.dispatcher import MAX_OUTBOUND_TOTAL_BYTES
from nagents.channels.dispatcher import MAX_TEXT_LENGTH
from nagents.channels.types import ChannelContentCapabilities
from nagents.channels.types import ChannelError
from nagents.channels.types import ChannelFile
from nagents.channels.types import ChannelSend
from nagents.channels.types import ChannelSendCapabilities
from nagents.migrations.manager import MigrationManager
from nagents.migrations.sessions import migrations

_ORIGIN_COLUMNS = (
    "root_session_id, actor_session_id, host_run_id, turn_id, task_id, activation, "
    "invocation_id, anchor_message_id, call_position, call_id, tool_name"
)


def delivery_cleanup_statements(session_id: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Statements for the caller's existing transaction; root OR actor membership."""
    predicate = "root_session_id = ? OR actor_session_id = ?"
    return (
        (
            "DELETE FROM ngn_local_delivery_assets WHERE delivery_id IN "
            f"(SELECT delivery_id FROM ngn_local_deliveries WHERE {predicate})",
            (session_id, session_id),
        ),
        (f"DELETE FROM ngn_local_deliveries WHERE {predicate}", (session_id, session_id)),
    )


def cleanup_deliveries(db: sqlite3.Connection, session_id: str) -> None:
    """Purge using an existing SQLite connection, without committing it."""
    if not _table(db, "ngn_local_deliveries"):
        return
    for sql, parameters in delivery_cleanup_statements(session_id):
        db.execute(sql, parameters)


def _table(db: sqlite3.Connection, name: str) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def _active(db: sqlite3.Connection, root: str) -> int:
    if not _table(db, "harness_sessions"):
        raise ChannelError("Local delivery root is not active")
    row = db.execute(
        "SELECT s.compacted_at_message_id FROM v2_sessions s JOIN harness_sessions h ON h.id = s.id WHERE s.id = ?",
        (root,),
    ).fetchone()
    if row is None or (
        _table(db, "ngn_web_session_trash")
        and db.execute("SELECT 1 FROM ngn_web_session_trash WHERE id = ?", (root,)).fetchone() is not None
    ):
        raise ChannelError("Local delivery root is not active")
    return int(row[0] or 0)


def _anchor(db: sqlite3.Connection, origin: DeliveryOrigin) -> None:
    if origin.actor_session_id != origin.root_session_id or origin.call_position < 0:
        raise ChannelError("Local delivery assistant anchor is inconsistent")
    row = db.execute(
        "SELECT role, tool_calls FROM v2_messages WHERE id = ? AND session_id = ?",
        (origin.anchor_message_id, origin.actor_session_id),
    ).fetchone()
    try:
        calls = json.loads(row[1]) if row is not None and row[0] == "assistant" else []
        call = calls[origin.call_position]
        if call["id"] == origin.call_id and call["name"] == origin.tool_name:
            return
    except (ValueError, TypeError, KeyError, IndexError):
        pass
    raise ChannelError("Local delivery assistant anchor is missing or inconsistent")


class PreparedDelivery:
    """Single-use write. Cancellation joins storage; inspect receipt/outcome afterward.

    ``receipt`` is a zero-or-one tuple, including after caller cancellation.
    ``outcome`` is prepared, pending, committed, failed, or unknown. A second commit raises
    RuntimeError, even after failure; separate prepares always allocate fresh IDs.
    Failed means no commit or established rollback; unknown raises a sanitized
    ChannelError with outcome_unknown=True. Recovery only reads the allocated ID.
    """

    def __init__(self, journal: DeliveryJournal, origin: DeliveryOrigin, channel: str, message: ChannelSend):
        self.delivery_id = uuid4().hex
        self.receipt: tuple[DeliveryReceipt, ...] = ()
        self.outcome: Literal["prepared", "pending", "committed", "failed", "unknown"] = "prepared"
        self._journal = journal
        self._origin = origin
        self._channel = channel
        self._message = message

    def _confirmed(self, receipt: DeliveryReceipt) -> DeliveryReceipt:
        self.receipt = (receipt,)
        self.outcome = "committed"
        return receipt

    async def commit(self) -> DeliveryReceipt:
        if self.outcome != "prepared":
            raise RuntimeError("Prepared delivery commit is single-use")
        self.outcome = "pending"

        async def owned() -> DeliveryReceipt:
            try:
                await self._journal.initialize()
            except Exception:
                self.outcome = "failed"
                raise ChannelError("Local delivery storage initialization failed") from None
            try:
                return await asyncio.to_thread(self._journal._commit, self)
            except Exception:
                if self.receipt:
                    return self.receipt[0]
                if self.outcome == "pending":
                    self.outcome = "unknown"
                    raise ChannelError(
                        "Local delivery outcome could not be established", outcome_unknown=True
                    ) from None
                raise

        return await join_owned(asyncio.create_task(owned()))


class DeliveryJournal:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._initialized = False
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self._lock:
            if not self._initialized:
                self.db_path.parent.mkdir(parents=True, exist_ok=True)
                await MigrationManager(self.db_path, db_name="sessions", migrations=list(migrations)).initialize()
                self._initialized = True

    def prepare(
        self, origin: DeliveryOrigin, channel: str, message: ChannelSend, capabilities: ChannelSendCapabilities
    ) -> PreparedDelivery:
        """Validate actual bytes and snapshot the entire payload before any await."""
        if (
            type(origin) is not DeliveryOrigin
            or type(message) is not ChannelSend
            or type(capabilities) is not ChannelSendCapabilities
        ):
            raise ChannelError("Invalid local delivery types")
        for name in (
            "root_session_id",
            "actor_session_id",
            "host_run_id",
            "turn_id",
            "task_id",
            "invocation_id",
            "call_id",
            "tool_name",
        ):
            value = getattr(origin, name)
            if type(value) is not str or len(value) > 512 or (value and not value.isprintable()):
                raise ChannelError("Invalid local delivery origin")
            if name != "task_id" and not value:
                raise ChannelError("Missing local delivery origin")
        for value, minimum in ((origin.activation, 0), (origin.anchor_message_id, 1), (origin.call_position, 0)):
            if type(value) is not int or not minimum <= value < 2**63:
                raise ChannelError("Invalid local delivery origin position")
        if origin.actor_session_id != origin.root_session_id or origin.task_id:
            raise ChannelError("Local deliveries currently require the root actor")
        if type(message.destination) is not str or message.destination != origin.root_session_id:
            raise ChannelError("Local delivery destination must match the root")
        if (
            type(message.thread_id) is not str
            or message.thread_id
            or type(message.reply_to) is not str
            or message.reply_to
        ):
            raise ChannelError("Local deliveries do not support thread or reply references")
        if type(message.metadata) is not dict or message.metadata:
            raise ChannelError("Local deliveries do not support metadata")
        if type(channel) is not str or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}", channel):
            raise ChannelError("Invalid local delivery channel")
        if type(message.text) is not str or len(message.text) > MAX_TEXT_LENGTH or "\x00" in message.text:
            raise ChannelError("Invalid local delivery text")
        try:
            message.text.encode("utf-8")
        except UnicodeError as error:
            raise ChannelError("Invalid local delivery text") from error
        if (
            type(message.attachments) is not tuple
            or message.attachments
            or type(message.files) is not tuple
            or len(message.files) > MAX_OUTBOUND_FILES
        ):
            raise ChannelError("Invalid local delivery attachments")
        files: list[ChannelFile] = []
        total = 0
        for file in message.files:
            if type(file) is not ChannelFile or type(file.data) is not bytes:
                raise ChannelError("Local delivery files require bytes")
            if (
                type(file.filename) is not str
                or not file.filename
                or len(file.filename) > 255
                or not file.filename.isprintable()
                or any(c in file.filename for c in "/\\:")
                or file.filename in (".", "..")
            ):
                raise ChannelError("Invalid local delivery filename")
            if (
                type(file.media_type) is not str
                or len(file.media_type) > 127
                or not re.fullmatch(r"[a-zA-Z0-9!#$&^_.+-]+/[a-zA-Z0-9!#$&^_.+-]+", file.media_type)
            ):
                raise ChannelError("Invalid local delivery MIME type")
            total += len(file.data)
            if len(file.data) > MAX_OUTBOUND_FILE_BYTES or total > MAX_OUTBOUND_TOTAL_BYTES:
                raise ChannelError("Local delivery attachment size limit exceeded")
            files.append(replace(file))
        if not message.text.strip() and not files:
            raise ChannelError("Local delivery is empty")
        snapshot = ChannelSend(destination=message.destination, text=message.text, files=tuple(files))
        validate_content_send(content_capabilities_catalog(ChannelContentCapabilities(send=capabilities)), snapshot)
        return PreparedDelivery(self, replace(origin), channel, snapshot)

    def _commit(self, prepared: PreparedDelivery) -> DeliveryReceipt:
        try:
            db = self._connect_write()
        except Exception:
            prepared.outcome = "failed"
            raise ChannelError("Local delivery storage could not be opened") from None
        try:
            try:
                candidate = self._insert(db, prepared)
            except Exception as error:
                # No COMMIT has been attempted. Closing also rolls back if this
                # explicit rollback fails; this operation cannot have committed.
                with suppress(Exception):
                    db.rollback()
                prepared.outcome = "failed"
                if isinstance(error, ChannelError):
                    raise
                raise ChannelError("Local delivery transaction failed") from None
            try:
                db.commit()
            except Exception:
                # A still-open transaction proves COMMIT did not complete. Only
                # a successful rollback establishes that outcome conclusively.
                rolled_back = False
                try:
                    if db.in_transaction:
                        db.rollback()
                        rolled_back = True
                except Exception:
                    pass
                if rolled_back:
                    prepared.outcome = "failed"
                    raise ChannelError("Local delivery transaction rolled back") from None
                try:
                    recovered = self._recover_receipt(prepared.delivery_id)
                except Exception:
                    recovered = ()
                if recovered == (candidate,):
                    return prepared._confirmed(candidate)
                # Absence alone cannot prove rollback: a concurrent lifecycle
                # purge could have removed a committed delivery before recovery.
                prepared.outcome = "unknown"
                raise ChannelError("Local delivery outcome could not be established", outcome_unknown=True) from None
            # Publish confirmation before close or any other fallible cleanup.
            return prepared._confirmed(candidate)
        finally:
            with suppress(Exception):
                db.close()

    def _connect_write(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _recover_receipt(self, delivery_id: str) -> tuple[DeliveryReceipt, ...]:
        """Resolve acknowledgement loss by identity, never by reissuing a send.

        Recovery ignores current membership: trash or ownership changes cannot
        change whether this exact operation committed. Read-only open avoids
        accidentally creating an empty database when storage is unavailable.
        """
        uri = self.db_path.resolve().as_uri() + "?mode=ro"
        db = sqlite3.connect(uri, uri=True)
        try:
            db.execute("BEGIN")
            row = db.execute(
                f"SELECT delivery_id, sequence, channel, {_ORIGIN_COLUMNS}, text, created_at "
                "FROM ngn_local_deliveries WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
            if row is None:
                return ()
            assets = tuple(
                DeliveryAsset(*asset)
                for asset in db.execute(
                    "SELECT asset_id, position, filename, media_type, byte_length "
                    "FROM ngn_local_delivery_assets WHERE delivery_id = ? ORDER BY position",
                    (delivery_id,),
                )
            )
            return (DeliveryReceipt(row[0], row[1], row[2], DeliveryOrigin(*row[3:14]), row[14], row[15], assets),)
        finally:
            # A completed read establishes status even if reader cleanup fails.
            with suppress(Exception):
                db.close()

    def _insert(self, db: sqlite3.Connection, prepared: PreparedDelivery) -> DeliveryReceipt:
        origin = prepared._origin
        created_at = datetime.now(UTC).isoformat()
        assets = tuple(
            DeliveryAsset(uuid4().hex, i, f.filename, f.media_type, len(f.data))
            for i, f in enumerate(prepared._message.files)
        )
        db.execute("BEGIN IMMEDIATE")
        _active(db, origin.root_session_id)
        _anchor(db, origin)
        cursor = db.execute(
            f"INSERT INTO ngn_local_deliveries (delivery_id, channel, {_ORIGIN_COLUMNS}, text, created_at) "
            f"VALUES ({','.join('?' for _ in range(15))})",
            (prepared.delivery_id, prepared._channel, *astuple(origin), prepared._message.text, created_at),
        )
        sequence = cursor.lastrowid
        assert sequence is not None
        for asset, file in zip(assets, prepared._message.files, strict=True):
            db.execute(
                "INSERT INTO ngn_local_delivery_assets VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    asset.asset_id,
                    prepared.delivery_id,
                    asset.position,
                    asset.filename,
                    asset.media_type,
                    asset.byte_length,
                    file.data,
                ),
            )
        return DeliveryReceipt(
            prepared.delivery_id, sequence, prepared._channel, origin, prepared._message.text, created_at, assets
        )

    async def history(self, root_session_id: str) -> DeliveryHistory:
        await self.initialize()
        return await join_owned(asyncio.create_task(asyncio.to_thread(self._history, root_session_id)))

    def _history(self, root: str) -> DeliveryHistory:
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("BEGIN")
            boundary = _active(db, root)
            earlier: list[DeliveryReceipt] = []
            current: list[DeliveryReceipt] = []
            rows = db.execute(
                f"SELECT delivery_id, sequence, channel, {_ORIGIN_COLUMNS}, text, created_at "
                "FROM ngn_local_deliveries WHERE root_session_id = ? "
                "ORDER BY anchor_message_id, call_position, sequence",
                (root,),
            ).fetchall()
            for row in rows:
                origin = DeliveryOrigin(*row[3:14])
                _anchor(db, origin)
                assets = tuple(
                    DeliveryAsset(*asset)
                    for asset in db.execute(
                        "SELECT asset_id, position, filename, media_type, byte_length "
                        "FROM ngn_local_delivery_assets WHERE delivery_id = ? ORDER BY position",
                        (row[0],),
                    )
                )
                receipt = DeliveryReceipt(row[0], row[1], row[2], origin, row[14], row[15], assets)
                (earlier if origin.anchor_message_id < boundary else current).append(receipt)
            return DeliveryHistory(boundary, tuple(earlier), tuple(current))

    async def read_asset(self, root_session_id: str, delivery_id: str, asset_id: str) -> bytes:
        await self.initialize()
        return await join_owned(
            asyncio.create_task(asyncio.to_thread(self._read_asset, root_session_id, delivery_id, asset_id))
        )

    def _read_asset(self, root: str, delivery_id: str, asset_id: str) -> bytes:
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("BEGIN")
            _active(db, root)
            row = db.execute(
                "SELECT a.byte_length, length(a.data), typeof(a.data) FROM ngn_local_delivery_assets a "
                "JOIN ngn_local_deliveries d ON d.delivery_id = a.delivery_id "
                "WHERE d.root_session_id = ? AND d.delivery_id = ? AND a.asset_id = ?",
                (root, delivery_id, asset_id),
            ).fetchone()
            if row is None or row[2] != "blob" or not 0 <= row[0] == row[1] <= MAX_OUTBOUND_FILE_BYTES:
                raise ChannelError("Local delivery asset is missing or invalid")
            data = db.execute("SELECT data FROM ngn_local_delivery_assets WHERE asset_id = ?", (asset_id,)).fetchone()[
                0
            ]
            return bytes(data)
