"""Durability, exact anchors, cancellation ownership, and lifecycle cleanup."""

import asyncio
import sqlite3
from contextlib import closing
from dataclasses import asdict
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest

from nagents.channels.delivery_types import DeliveryOrigin
from nagents.channels.delivery_types import DeliveryReceipt
from nagents.channels.types import ChannelAttachment
from nagents.channels.types import ChannelError
from nagents.channels.types import ChannelFile
from nagents.channels.types import ChannelSend
from nagents.channels.types import ChannelSendCapabilities
from nagents.session import deliveries
from nagents.session.deliveries import DeliveryJournal
from nagents.session.deliveries import PreparedDelivery
from nagents.session.manager import SessionManager
from nagents.types import Message
from nagents.types import ToolCall

CAPS = ChannelSendCapabilities(text=True, file_media_types=("text/plain",))
SEND = ChannelSend("root", "hello", files=(ChannelFile("note.txt", "text/plain", b"saved"),))


async def setup(path: Path) -> tuple[SessionManager, DeliveryJournal, DeliveryOrigin]:
    manager = SessionManager(path)
    await manager.get_or_create_session("root", "user")
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE harness_sessions (id TEXT PRIMARY KEY, title TEXT DEFAULT '')")
        db.execute("INSERT INTO harness_sessions(id) VALUES ('root')")
    anchor = await manager.add_message(
        "root", Message(role="assistant", tool_calls=[ToolCall(id="call", name="channel_send", arguments={})])
    )
    return (
        manager,
        DeliveryJournal(path),
        DeliveryOrigin("root", "root", "run", "turn", "", 0, "invoke", anchor, 0, "call", "channel_send"),
    )


def test_restart_backup_and_distinct_sends(tmp_path: Path) -> None:
    async def run() -> None:
        path = tmp_path / "sessions.db"
        _, journal, origin = await setup(path)
        workspace = tmp_path / "note.txt"
        workspace.write_bytes(b"saved")
        prepared = journal.prepare(
            origin,
            "web",
            replace(SEND, files=(ChannelFile(workspace.name, "text/plain", workspace.read_bytes()),)),
            CAPS,
        )
        workspace.unlink()
        first = await prepared.commit()
        second = await journal.prepare(origin, "web", SEND, CAPS).commit()
        assert first.delivery_id != second.delivery_id
        assert first.sequence < second.sequence
        assert "data" not in asdict(first.assets[0])
        backup = tmp_path / "backup.db"
        with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(backup)) as target:
            source.backup(target)
        restored = DeliveryJournal(backup)
        assert (await restored.history("root")).current == (first, second)
        assert await restored.read_asset("root", first.delivery_id, first.assets[0].asset_id) == b"saved"
        with pytest.raises(ChannelError):
            await restored.read_asset("other", first.delivery_id, first.assets[0].asset_id)
        with pytest.raises(RuntimeError):
            await prepared.commit()

    asyncio.run(run())


def test_late_asset_failure_rolls_back(tmp_path: Path) -> None:
    async def run() -> None:
        _, journal, origin = await setup(tmp_path / "sessions.db")
        with closing(sqlite3.connect(journal.db_path)) as db, db:
            db.execute(
                "CREATE TRIGGER fail_asset BEFORE INSERT ON ngn_local_delivery_assets "
                "WHEN NEW.position = 1 BEGIN SELECT RAISE(ABORT, 'late asset'); END"
            )
        prepared = journal.prepare(origin, "web", replace(SEND, files=SEND.files * 2), CAPS)
        with pytest.raises(ChannelError) as failure:
            await prepared.commit()
        assert failure.value.outcome_unknown is False
        assert prepared.outcome == "failed" and not prepared.receipt
        assert not (await journal.history("root")).current
        with closing(sqlite3.connect(journal.db_path)) as db:
            assert db.execute("SELECT count(*) FROM ngn_local_delivery_assets").fetchone()[0] == 0

    asyncio.run(run())


@pytest.mark.parametrize(
    "field,value",
    [
        ("destination", "other"),
        ("destination", ""),
        ("thread_id", "thread"),
        ("reply_to", "reference"),
        ("metadata", {"extra": "value"}),
        ("thread_id", 0),
        ("reply_to", None),
        ("metadata", []),
    ],
)
def test_rejects_unsupported_envelope_without_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: object
) -> None:
    def no_database(*args: object, **kwargs: object) -> sqlite3.Connection:
        pytest.fail("prepare must not open storage")

    monkeypatch.setattr(sqlite3, "connect", no_database)
    origin = DeliveryOrigin("root", "root", "run", "turn", "", 0, "invocation", 1, 0, "call", "channel_send")
    journal = DeliveryJournal(tmp_path / "not-created.db")
    message = replace(SEND)
    object.__setattr__(message, field, value)
    with pytest.raises(ChannelError) as failure:
        journal.prepare(origin, "web", message, CAPS)
    assert failure.value.outcome_unknown is False
    assert not journal.db_path.exists()


def test_child_task_cannot_claim_root_origin(tmp_path: Path) -> None:
    origin = DeliveryOrigin("root", "root", "run", "turn", "child-task", 1, "invocation", 1, 0, "call", "channel_send")
    journal = DeliveryJournal(tmp_path / "not-created.db")
    with pytest.raises(ChannelError, match="root actor"):
        journal.prepare(origin, "web", SEND, CAPS)
    assert not journal.db_path.exists()


@pytest.mark.parametrize("fault", ["before_commit", "ack", "close", "ack_and_close", "unknown"])
def test_commit_boundary_outcomes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str) -> None:
    async def run() -> None:
        _, journal, origin = await setup(tmp_path / "sessions.db")
        prepared = journal.prepare(origin, "web", SEND, CAPS)
        commit_calls = 0
        recovery_calls: list[str] = []
        original_recover = journal._recover_receipt

        class FaultConnection(sqlite3.Connection):
            def commit(self) -> None:
                nonlocal commit_calls
                commit_calls += 1
                if fault == "before_commit":
                    raise sqlite3.OperationalError("private precommit failure")
                super().commit()
                if fault in ("ack", "ack_and_close", "unknown"):
                    raise sqlite3.OperationalError("private acknowledgement failure")

            def close(self) -> None:
                if fault in ("close", "ack_and_close"):
                    assert prepared.outcome == "committed"
                    assert prepared.receipt
                super().close()
                if fault in ("close", "ack_and_close"):
                    raise sqlite3.OperationalError("private close failure")

        def recover(delivery_id: str) -> tuple[DeliveryReceipt, ...]:
            recovery_calls.append(delivery_id)
            if fault == "unknown":
                raise sqlite3.OperationalError("private storage unavailable")
            return original_recover(delivery_id)

        monkeypatch.setattr(
            journal, "_connect_write", lambda: sqlite3.connect(journal.db_path, factory=FaultConnection)
        )
        monkeypatch.setattr(journal, "_recover_receipt", recover)
        if fault in ("before_commit", "unknown"):
            with pytest.raises(ChannelError) as failure:
                await prepared.commit()
            assert failure.value.outcome_unknown is (fault == "unknown")
            assert "private" not in str(failure.value)
            assert prepared.outcome == ("unknown" if fault == "unknown" else "failed")
            assert prepared.receipt == ()
        else:
            receipt = await prepared.commit()
            assert prepared.outcome == "committed" and prepared.receipt == (receipt,)
            assert (await journal.history("root")).current == (receipt,)
        assert commit_calls == 1
        assert recovery_calls == ([prepared.delivery_id] if fault in ("ack", "ack_and_close", "unknown") else [])
        with pytest.raises(RuntimeError):
            await prepared.commit()
        with closing(sqlite3.connect(journal.db_path)) as db:
            for table in ("ngn_local_deliveries", "ngn_local_delivery_assets"):
                assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == (fault != "before_commit")

    asyncio.run(run())


def test_cancel_during_ack_recovery_joins_and_retains_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        _, journal, origin = await setup(tmp_path / "sessions.db")
        prepared = journal.prepare(origin, "web", SEND, CAPS)
        entered, release = Event(), Event()
        original_recover = journal._recover_receipt

        class LostAckConnection(sqlite3.Connection):
            def commit(self) -> None:
                super().commit()
                raise sqlite3.OperationalError("lost acknowledgement")

        def recover(delivery_id: str) -> tuple[DeliveryReceipt, ...]:
            entered.set()
            assert release.wait(5)
            return original_recover(delivery_id)

        monkeypatch.setattr(
            journal, "_connect_write", lambda: sqlite3.connect(journal.db_path, factory=LostAckConnection)
        )
        monkeypatch.setattr(journal, "_recover_receipt", recover)
        task = asyncio.create_task(prepared.commit())
        assert await asyncio.to_thread(entered.wait, 5)
        try:
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert prepared.outcome == "committed"
        assert len(prepared.receipt) == 1
        assert (await journal.history("root")).current == prepared.receipt

    asyncio.run(run())


def test_bounds_and_capabilities(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        _, journal, origin = await setup(tmp_path / "sessions.db")
        invalid = [
            replace(SEND, text=1),  # type: ignore[arg-type]
            replace(SEND, text="\ud800"),
            replace(SEND, files=(ChannelFile("../file", "text/plain", b"x"),)),
            replace(SEND, files=(ChannelFile("f", "bad", b"x"),)),
            replace(SEND, files=(ChannelFile("f", "text/plain", bytearray(b"x")),)),  # type: ignore[arg-type]
            replace(SEND, files=SEND.files * 4),
            replace(SEND, attachments=(ChannelAttachment("/tmp/file"),)),
        ]
        for message in invalid:
            with pytest.raises(ChannelError):
                journal.prepare(origin, "web", message, CAPS)
        with pytest.raises(ChannelError):
            journal.prepare(origin, "web", SEND, None)  # type: ignore[arg-type]
        for caps in (
            ChannelSendCapabilities(text=True),
            replace(CAPS, text=False),
            replace(CAPS, max_file_bytes=1),
            replace(CAPS, max_total_bytes=1),
        ):
            with pytest.raises(ChannelError):
                journal.prepare(origin, "web", SEND, caps)
        monkeypatch.setattr(deliveries, "MAX_OUTBOUND_FILE_BYTES", 4)
        with pytest.raises(ChannelError):
            journal.prepare(origin, "web", SEND, CAPS)
        monkeypatch.setattr(deliveries, "MAX_OUTBOUND_FILE_BYTES", 10)
        monkeypatch.setattr(deliveries, "MAX_OUTBOUND_TOTAL_BYTES", 9)
        with pytest.raises(ChannelError):
            journal.prepare(origin, "web", replace(SEND, files=SEND.files * 2), CAPS)

    asyncio.run(run())


def test_history_uses_one_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        _, journal, origin = await setup(tmp_path / "sessions.db")
        receipt = await journal.prepare(origin, "web", SEND, CAPS).commit()
        entered, release = Event(), Event()
        original = deliveries._active
        with closing(sqlite3.connect(journal.db_path)) as db:
            db.execute("PRAGMA journal_mode = WAL")

        def blocked(db: sqlite3.Connection, root: str) -> int:
            boundary = original(db, root)
            entered.set()
            assert release.wait(5)
            return boundary

        monkeypatch.setattr(deliveries, "_active", blocked)
        task = asyncio.create_task(journal.history("root"))
        assert await asyncio.to_thread(entered.wait, 5)
        try:
            with closing(sqlite3.connect(journal.db_path)) as db, db:
                db.execute("UPDATE v2_sessions SET compacted_at_message_id = 100")
                db.execute("DELETE FROM ngn_local_delivery_assets")
                db.execute("DELETE FROM ngn_local_deliveries")
        finally:
            release.set()
        history = await task
        assert history.boundary == 0 and history.current == (receipt,)

    asyncio.run(run())


def test_membership_revalidation_and_historical_ownership(tmp_path: Path) -> None:
    async def run() -> None:
        _, journal, origin = await setup(tmp_path / "sessions.db")
        receipt = await journal.prepare(origin, "web", SEND, CAPS).commit()
        with closing(sqlite3.connect(journal.db_path)) as db, db:
            db.execute("CREATE TABLE ngn_web_session_owners (session_id TEXT PRIMARY KEY, channel TEXT)")
            db.execute("INSERT INTO ngn_web_session_owners VALUES ('root', 'external')")
        assert (await journal.history("root")).current == (receipt,)
        assert await journal.read_asset("root", receipt.delivery_id, receipt.assets[0].asset_id) == b"saved"
        prepared = journal.prepare(origin, "web", SEND, CAPS)
        with closing(sqlite3.connect(journal.db_path)) as db, db:
            db.execute("CREATE TABLE ngn_web_session_trash (id TEXT PRIMARY KEY)")
            db.execute("INSERT INTO ngn_web_session_trash VALUES ('root')")
        with pytest.raises(ChannelError):
            await prepared.commit()
        with pytest.raises(ChannelError):
            await journal.read_asset("root", receipt.delivery_id, receipt.assets[0].asset_id)

    asyncio.run(run())


@pytest.mark.parametrize("after_commit", [False, True])
def test_repeated_cancel_joins_owned_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after_commit: bool
) -> None:
    async def run() -> None:
        _, journal, origin = await setup(tmp_path / "sessions.db")
        entered, release = Event(), Event()
        original = journal._commit

        def blocked(prepared: PreparedDelivery) -> DeliveryReceipt:
            if after_commit:
                receipt = original(prepared)
            entered.set()
            assert release.wait(5)
            return receipt if after_commit else original(prepared)

        monkeypatch.setattr(journal, "_commit", blocked)
        prepared = journal.prepare(origin, "web", SEND, CAPS)
        task = asyncio.create_task(prepared.commit())
        assert await asyncio.to_thread(entered.wait, 5)
        try:
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert prepared.outcome == "committed"
        assert (await journal.history("root")).current == prepared.receipt

    asyncio.run(run())


def test_exact_anchor_inclusive_boundary_and_corruption(tmp_path: Path) -> None:
    async def run() -> None:
        manager, journal, origin = await setup(tmp_path / "sessions.db")
        first = await journal.prepare(origin, "web", SEND, CAPS).commit()
        await manager.set_compaction_boundary("root", origin.anchor_message_id)
        assert (await journal.history("root")).current == (first,)
        copied = await manager.add_message(
            "root", Message(role="assistant", tool_calls=[ToolCall(id="call", name="channel_send", arguments={})])
        )
        await manager.set_compaction_boundary("root", copied)
        history = await journal.history("root")
        assert history.earlier == (first,) and not history.current
        for bad in (
            replace(origin, call_position=1),
            replace(origin, call_id="wrong"),
            replace(origin, tool_name="wrong"),
        ):
            with pytest.raises(ChannelError):
                await journal.prepare(bad, "web", SEND, CAPS).commit()
        with closing(sqlite3.connect(journal.db_path)) as db, db:
            db.execute("DELETE FROM v2_messages WHERE id = ?", (origin.anchor_message_id,))
        with pytest.raises(ChannelError):
            await journal.history("root")

    asyncio.run(run())


@pytest.mark.parametrize("delete", [False, True])
@pytest.mark.parametrize("actor_only", [False, True])
def test_library_cleanup(tmp_path: Path, delete: bool, actor_only: bool) -> None:
    async def run() -> None:
        manager, journal, origin = await setup(tmp_path / "sessions.db")
        await journal.prepare(origin, "web", SEND, CAPS).commit()
        await manager.set_compaction_boundary("root", origin.anchor_message_id)
        if actor_only:
            with closing(sqlite3.connect(journal.db_path)) as db, db:
                db.execute("UPDATE ngn_local_deliveries SET root_session_id = 'other'")
        await (manager.delete_session("root") if delete else manager.clear_session("root"))
        with closing(sqlite3.connect(journal.db_path)) as db:
            for table in ("ngn_local_deliveries", "ngn_local_delivery_assets", "v2_messages"):
                assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
            if not delete:
                assert db.execute("SELECT compacted_at_message_id FROM v2_sessions").fetchone()[0] is None

    asyncio.run(run())
