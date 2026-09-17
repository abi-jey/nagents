"""Durable soft deletion, generation/expiry guards, and cancellation-safe recovery."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from functools import partial
from threading import Event
from typing import TYPE_CHECKING
from typing import cast

import pytest
from fastapi import HTTPException

from nagents.channels.store import InboxStore
from nagents.channels.store import finish_on_cancel
from nagents.channels.types import ChannelCommand
from nagents.harness.subagents import TaskInfo
from nagents.types import Message
from nagents.web import deletion
from nagents.web.catalog import Connection
from nagents.web.service import Run
from nagents.web.service import WebState
from nagents.web.subscriptions import Subscriber
from nagents.web.trash import PURGE_BATCH
from nagents.web.trash import SessionTrash
from nagents.web.trash import TrashItem
from nagents.web.wakeups import Chain
from tests.hang_guard import HANG_GUARD
from tests.test_web import client_app
from tests.test_web_deletion import quiet
from tests.test_web_deletion import rows
from tests.test_web_subscription_lifecycle import Peer

if TYPE_CHECKING:
    from pathlib import Path

    from nagents.harness.types import SessionInfo

DAY = 86400


@dataclass
class Clock:
    value: float = 1_800_000_000.0

    def __call__(self) -> float:
        return self.value


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    value = Clock()
    monkeypatch.setattr("nagents.web.service.SessionTrash", partial(SessionTrash, clock=value))
    return value


@pytest.mark.parametrize("selection", ["selected", "other", "last"])
def test_soft_delete_preserves_all_history_and_restores_same_identity_without_selecting(
    tmp_path: Path, clock: Clock, selection: str
) -> None:
    async def run() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            await quiet(state)
            root = state.selected_session_id
            child = "ngn-child-history"
            await state.history.add_message(root, Message(role="user", content="exact root history"))
            await state.history.get_or_create_session(child, "harness")
            await state.history.add_message(child, Message(role="user", content="retained child"))
            await state.channels.store.web(root, "terminal-web-message", "journal prompt")
            legacy = InboxStore(state.history.db_path, root, 10)
            await legacy.initialize()
            await legacy.admit("fixture", "old-id", "retained envelope")

            def seed(db: sqlite3.Connection) -> None:
                db.execute("UPDATE harness_sessions SET title = 'Exact saved title' WHERE id = ?", (root,))
                db.execute("UPDATE ngn_web_inbox SET status = 'completed'")
                db.execute("UPDATE nagents_channel_inbox SET status = 'completed'")
                db.execute(
                    "INSERT INTO ngn_web_message_origins SELECT m.id, i.id FROM v2_messages m "
                    "JOIN ngn_web_inbox i ON m.session_id = i.session_id WHERE m.session_id = ?",
                    (root,),
                )

            await state.channels.store._transaction(seed)
            tables = ("v2_messages", "v2_sessions", "ngn_web_inbox", "ngn_web_message_origins", "nagents_channel_inbox")
            other = ""
            if selection != "last":
                other = (await client.post("/api/sessions/new", headers=headers, json={})).json()["session_id"]
                if selection == "selected":
                    await client.post("/api/sessions/resume", headers=headers, json={"session_id": root})
            before = {table: await rows(state, f"SELECT * FROM {table}") for table in tables}
            await state.settings.change(state.settings.revision, state.settings.values)
            settings = await rows(state, "SELECT * FROM ngn_web_settings")
            subscriber = Subscriber(session_id=root, ready=True)
            state.bus.subscribers.add(subscriber)
            response = await client.request("DELETE", f"/api/sessions/{root}", headers=headers, json={})
            assert response.status_code == 200, response.text
            item = response.json()["trash"]
            assert set(item) == {"id", "title", "deleted_at", "purge_at", "deletion_id"}
            assert item["id"] == root and item["title"] == "Exact saved title"
            assert item["deleted_at"] == clock.value and item["purge_at"] == clock.value + 30 * DAY
            assert item["deletion_id"]
            selected = response.json()["session_id"]
            assert selected != root and (not other or selected == other)
            assert subscriber.close_code == 1008 and not state.bus.listening(root)
            assert not await rows(state, "SELECT * FROM harness_sessions WHERE id = ?", root)
            for table in tables:
                after = await rows(state, f"SELECT * FROM {table}")
                assert after[: len(before[table])] == before[table]
                if table != "v2_sessions":
                    assert after == before[table]
            assert await rows(state, "SELECT * FROM ngn_web_settings") == settings
            assert (await client.get("/api/trash", headers=headers)).json()["items"] == [item]
            restored = await client.post(
                f"/api/trash/{root}/restore", headers=headers, json={"deletion_id": item["deletion_id"]}
            )
            assert restored.status_code == 200, restored.text
            assert set(restored.json()) == {"restored_session_id", "sessions"}
            assert restored.json()["restored_session_id"] == root
            assert state.selected_session_id == state.harness.session_id == selected
            assert await rows(state, "SELECT title FROM harness_sessions WHERE id = ?", root) == [
                ("Exact saved title",)
            ]
            assert not await rows(state, "SELECT * FROM ngn_web_session_trash")
            assert (await client.get(f"/api/sessions/{root}", headers=headers)).status_code == 200
            assert state.active is None and all(frame.get("type") != "event" for frame, _ in state.bus.ring)

    asyncio.run(run())


def test_duplicate_soft_delete_freezes_generation_deadline_and_replacement(tmp_path: Path, clock: Clock) -> None:
    async def run() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            await quiet(state)
            root = state.selected_session_id
            first = (await client.request("DELETE", f"/api/sessions/{root}", headers=headers, json={})).json()
            clock.value += 2 * DAY
            second = (
                await client.request("DELETE", f"/api/sessions/{root}", headers=headers, json={"permanent": False})
            ).json()
            assert first["trash"] == second["trash"]
            assert first["session_id"] == second["session_id"]
            assert len(await rows(state, "SELECT * FROM harness_sessions")) == 1
            assert len(await rows(state, "SELECT * FROM ngn_web_session_trash")) == 1
            assert (
                await client.request("DELETE", f"/api/sessions/{root}", headers=headers, json={"permanent": True})
            ).status_code == 404

    asyncio.run(run())


def test_policy_is_separate_revisioned_persistent_and_affects_only_future_deletions(
    tmp_path: Path, clock: Clock
) -> None:
    async def run() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            await quiet(state)
            original = (await client.get("/api/trash", headers=headers)).json()
            assert set(original) == {"revision", "retention_days", "items"}
            assert original["retention_days"] == 30 and original["items"] == []
            root = state.selected_session_id
            first = (await client.request("DELETE", f"/api/sessions/{root}", headers=headers, json={})).json()["trash"]
            settings = state.settings.snapshot()
            changed = await client.put(
                "/api/trash/settings", headers=headers, json={"revision": original["revision"], "retention_days": 1}
            )
            assert changed.status_code == 200
            policy = changed.json()
            assert policy["revision"] != original["revision"] and policy["items"] == [first]
            assert state.settings.snapshot() == settings
            assert (
                await client.put(
                    "/api/trash/settings",
                    headers=headers,
                    json={"revision": original["revision"], "retention_days": 365},
                )
            ).status_code == 409
            second_root = state.selected_session_id
            second = (await client.request("DELETE", f"/api/sessions/{second_root}", headers=headers, json={})).json()[
                "trash"
            ]
            assert second["purge_at"] == clock.value + DAY and first["purge_at"] == clock.value + 30 * DAY
        async with client_app(tmp_path) as (_, client, headers, _):
            restarted = (await client.get("/api/trash", headers=headers)).json()
            assert restarted["retention_days"] == 1 and restarted["revision"] == policy["revision"]
            assert {item["id"]: item for item in restarted["items"]} == {root: first, second_root: second}

    asyncio.run(run())


@pytest.mark.parametrize("value", [0, 366, -1, True, 1.5, "30", None])
def test_trash_policy_strict_bounds(tmp_path: Path, clock: Clock, value: object) -> None:
    async def run() -> None:
        async with client_app(tmp_path) as (_, client, headers, _):
            initial = (await client.get("/api/trash", headers=headers)).json()
            response = await client.put(
                "/api/trash/settings", headers=headers, json={"revision": initial["revision"], "retention_days": value}
            )
            assert response.status_code == 422
            assert (await client.get("/api/trash", headers=headers)).json() == initial

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["restore", "purge"])
def test_stale_generation_cannot_restore_or_purge_later_deletion(tmp_path: Path, clock: Clock, operation: str) -> None:
    async def run() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            await quiet(state)
            root = state.selected_session_id
            first = (await deletion.delete_session(state, root))["trash"]
            assert isinstance(first, dict)
            old = str(first["deletion_id"])
            await state.trash.restore(root, old)
            with pytest.raises(HTTPException) as error:
                await state.trash.restore(root, old)
            assert error.value.status_code == 409
            second = (await deletion.delete_session(state, root))["trash"]
            assert isinstance(second, dict) and second["deletion_id"] != old
            method, path = (
                ("POST", f"/api/trash/{root}/restore") if operation == "restore" else ("DELETE", f"/api/trash/{root}")
            )
            response = await client.request(method, path, headers=headers, json={"deletion_id": old})
            assert response.status_code == 409
            assert (await state.trash.snapshot())["items"] == [second]
            purged = await client.request(
                "DELETE", f"/api/trash/{root}", headers=headers, json={"deletion_id": second["deletion_id"]}
            )
            assert purged.status_code == 200 and purged.json() == {"purged_session_id": root}
            assert (
                await client.request(
                    "DELETE", f"/api/trash/{root}", headers=headers, json={"deletion_id": second["deletion_id"]}
                )
            ).status_code == 404
            assert not await rows(state, "SELECT * FROM v2_sessions WHERE id = ?", root)
            assert len(await rows(state, "SELECT * FROM harness_sessions")) == 1

    asyncio.run(run())


def test_expired_restore_rejected_before_idle_purge_and_expiry_survives_restart(tmp_path: Path, clock: Clock) -> None:
    async def run() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            await quiet(state)
            root = state.selected_session_id
            await state.history.add_message(root, Message(role="user", content="retained until expiry"))
            item = (await client.request("DELETE", f"/api/sessions/{root}", headers=headers, json={})).json()["trash"]
            clock.value = item["purge_at"]
            response = await client.post(
                f"/api/trash/{root}/restore", headers=headers, json={"deletion_id": item["deletion_id"]}
            )
            assert response.status_code == 410
            state.active = Run(state.selected_session_id)
            try:
                assert await state.trash.purge_expired() == []
                assert await rows(state, "SELECT * FROM v2_sessions WHERE id = ?", root)
            finally:
                state.active = None
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            assert (await client.get("/api/trash", headers=headers)).json()["items"] == []
            assert not await rows(state, "SELECT * FROM v2_sessions WHERE id = ?", root)
            assert not await rows(state, "SELECT * FROM v2_messages WHERE session_id = ?", root)
            assert state.active is None

    asyncio.run(run())


def test_trash_hides_membership_from_http_ws_harness_channels_and_admission(tmp_path: Path, clock: Clock) -> None:
    async def run() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            await quiet(state)
            root = state.selected_session_id
            peer = Peer()
            task = await peer.start(state.bus, state.snapshot, state.disconnected)
            peer.subscribe(root)
            await peer.frame()
            await deletion.delete_session(state, root)
            assert (await peer.message())["code"] == 1008
            await task
            assert root not in {item.id for item in await state.harness.list_sessions()}
            with pytest.raises(ValueError):
                await state.harness.resume(root)
            for path in (f"/api/sessions/{root}", f"/api/activity/{root}/0"):
                assert (await client.get(path, headers=headers)).status_code == 404
            assert (
                await client.post("/api/sessions/resume", headers=headers, json={"session_id": root})
            ).status_code == 404
            assert (
                await client.post(
                    "/api/messages",
                    headers=headers,
                    json={
                        "session_id": root,
                        "message_id": "12345678-1234-1234-1234-123456789abc",
                        "prompt": "must not run",
                    },
                )
            ).status_code == 404
            assert (
                await client.post("/api/run", headers=headers, json={"session_id": root, "prompt": "must not run"})
            ).status_code == 404
            peer = Peer()
            task = await peer.start(state.bus, state.snapshot, state.disconnected)
            peer.subscribe(root)
            assert (await peer.message())["code"] == 1008
            await task
            envelope = json.dumps(
                dict(message_id="list", conversation_id="conversation", thread_id="", reply_to="", text="/sessions")
            )
            await state.channels.store.receive(
                "fixture", envelope, state.selected_session_id, ChannelCommand("sessions")
            )
            assert root not in str(await rows(state, "SELECT acknowledgement FROM ngn_web_inbox"))

    asyncio.run(run())


def test_recovered_dangling_binding_or_queued_ack_cannot_replay_trash(tmp_path: Path, clock: Clock) -> None:
    async def run() -> None:
        async with client_app(tmp_path) as (app, _, _, _):
            state = cast("WebState", app.state.web)
            await quiet(state)
            root = state.selected_session_id
            result = await deletion.delete_session(state, root)
            item = result["trash"]
            assert isinstance(item, dict)

            def recover(db: sqlite3.Connection) -> None:
                db.execute("INSERT INTO ngn_web_bindings VALUES ('fixture', 'conversation', ?, ?)", (root, root))
                db.execute(
                    "INSERT INTO ngn_web_inbox(session_id, channel, message_id, prompt, acknowledgement) VALUES (?, 'fixture', 'queued', 'private', 'must not send')",
                    (root,),
                )

            await state.channels.store._transaction(recover)
            envelope = json.dumps(
                dict(message_id="new", conversation_id="conversation", thread_id="", reply_to="", text="new input")
            )
            with pytest.raises(HTTPException) as error:
                await state.trash.restore(root, str(item["deletion_id"]))
            assert error.value.status_code == 409
            await state.channels.store.receive("fixture", envelope, state.selected_session_id, None)
            work = await state.channels.store.claim_work()
            assert work is not None and work.session_id != root and work.message_id == "new"
            assert "please send your request again" in work.acknowledgement
            await state.channels.store.finish_work(work, "completed")
            assert not await state.channels.store.has_pending()
            assert await state.channels.store.claim_work() is None
            assert await rows(state, "SELECT prompt, status FROM ngn_web_inbox WHERE message_id = 'queued'") == [
                ("private", "failed")
            ]
            assert not await rows(state, "SELECT * FROM harness_sessions WHERE id = ?", root)
            assert await rows(state, "SELECT * FROM ngn_web_session_trash WHERE id = ?", root)

    asyncio.run(run())


@pytest.mark.parametrize(
    "guard",
    [
        "active",
        "busy",
        "retained",
        "worker",
        "wakeup",
        "main",
        "disabled-main",
        "binding",
        "default",
        "queued",
        "running",
    ],
)
def test_soft_delete_keeps_existing_work_and_routing_guards(tmp_path: Path, clock: Clock, guard: str) -> None:
    async def run() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            await quiet(state)
            root = state.selected_session_id
            worker = asyncio.create_task(asyncio.sleep(3600))
            if guard == "active":
                state.active = Run("ngn-other-active")
            elif guard == "busy":
                state.harness._busy = "compaction"
            elif guard == "retained":
                state.harness.tasks._infos["retained"] = TaskInfo(
                    "retained", "child", session_id=root, status="completed"
                )
            elif guard == "worker":
                state.harness.tasks._workers["worker"] = worker
            elif guard == "wakeup":
                state.wakeups.schedule(root, "run", Chain(), "", 86400, "later")
            elif guard in {"main", "disabled-main"}:
                state.channels.catalog.connections["fixture"] = Connection(
                    plugin="fixture", enabled=guard == "main", config={}, secrets={}, main_session_id=root
                )
            elif guard in {"binding", "default"}:

                def bind(db: sqlite3.Connection) -> None:
                    db.execute(
                        "INSERT INTO ngn_web_bindings VALUES ('fixture', 'conversation', ?, ?)",
                        (root if guard == "binding" else "other", root if guard == "default" else "other"),
                    )

                await state.channels.store._transaction(bind)
            else:
                await state.channels.store.web(root, "pending", "keep work")

                def pending(db: sqlite3.Connection) -> None:
                    db.execute("UPDATE ngn_web_inbox SET status = ?", (guard,))

                await state.channels.store._transaction(pending)
            try:
                response = await client.request("DELETE", f"/api/sessions/{root}", headers=headers, json={})
                assert response.status_code == 409, response.text
                assert await rows(state, "SELECT id FROM harness_sessions WHERE id = ?", root) == [(root,)]
                assert not await rows(state, "SELECT * FROM ngn_web_session_trash")
                assert state.selected_session_id == root
            finally:
                state.active = None
                state.harness._busy = ""
                state.harness.tasks._infos.clear()
                state.harness.tasks._workers.clear()
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["GET", "PUT", "POST", "DELETE"])
def test_all_trash_endpoints_retain_local_auth_guards(tmp_path: Path, clock: Clock, operation: str) -> None:
    async def run() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            root = state.selected_session_id
            path = {
                "GET": "/api/trash",
                "PUT": "/api/trash/settings",
                "POST": f"/api/trash/{root}/restore",
                "DELETE": f"/api/trash/{root}",
            }[operation]
            response = await client.request(operation, path, headers={**headers, "X-Ngn-Token": "wrong"}, json={})
            assert response.status_code == 403

    asyncio.run(run())


@pytest.mark.parametrize("body", [{"permanent": "true"}, {"permanent": 1}, {"extra": True}])
def test_delete_input_remains_strict(tmp_path: Path, clock: Clock, body: dict[str, object]) -> None:
    async def run() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            root = state.selected_session_id
            assert (
                await client.request("DELETE", f"/api/sessions/{root}", headers=headers, json=body)
            ).status_code == 422
            assert not await rows(state, "SELECT * FROM ngn_web_session_trash")

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["trash", "restore", "purge"])
@pytest.mark.parametrize("rollback", [False, True])
def test_cancelled_mutations_join_atomic_commit_or_rollback(
    tmp_path: Path, clock: Clock, monkeypatch: pytest.MonkeyPatch, operation: str, rollback: bool
) -> None:
    async def run() -> None:
        async with client_app(tmp_path) as (app, _, _, _):
            state = cast("WebState", app.state.web)
            await quiet(state)
            root = state.selected_session_id
            await state.history.add_message(root, Message(role="user", content="preserved"))
            generation = ""
            if operation != "trash":
                item = (await deletion.delete_session(state, root))["trash"]
                assert isinstance(item, dict)
                generation = str(item["deletion_id"])
            before_membership = await rows(state, "SELECT * FROM harness_sessions")
            before_trash = await rows(state, "SELECT * FROM ngn_web_session_trash")
            entered, release = Event(), Event()

            def block() -> None:
                entered.set()
                assert release.wait(HANG_GUARD)
                if rollback:
                    raise ValueError("injected transaction failure")

            remove_rows = state.trash.remove_rows
            restore_rows = state.trash._restore_rows
            purge_rows = state.trash._purge_rows

            def remove(db: sqlite3.Connection, id: str, selected: str) -> tuple[str, TrashItem]:
                result = remove_rows(db, id, selected)
                block()
                return result

            def restore(db: sqlite3.Connection, id: str, deletion_id: str) -> None:
                restore_rows(db, id, deletion_id)
                block()

            def purge(db: sqlite3.Connection, id: str, deletion_id: str, *, expired: bool = False) -> None:
                purge_rows(db, id, deletion_id, expired=expired)
                block()

            monkeypatch.setattr(state.trash, "remove_rows", remove)
            monkeypatch.setattr(state.trash, "_restore_rows", restore)
            monkeypatch.setattr(state.trash, "_purge_rows", purge)
            action = (
                deletion.delete_session(state, root)
                if operation == "trash"
                else state.trash.restore(root, generation)
                if operation == "restore"
                else state.trash.purge(root, generation)
            )
            task = asyncio.create_task(action)
            assert await asyncio.to_thread(entered.wait, 5)
            try:
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                assert not task.done() and state.mutating
                assert await rows(state, "SELECT * FROM harness_sessions") == before_membership
                assert await rows(state, "SELECT * FROM ngn_web_session_trash") == before_trash
            finally:
                release.set()
            with pytest.raises(ValueError if rollback else asyncio.CancelledError):
                await task
            assert not state.mutating and not state.harness._busy
            present = bool(await rows(state, "SELECT * FROM harness_sessions WHERE id = ?", root))
            assert present is ((operation == "trash") if rollback else (operation == "restore"))
            assert bool(await rows(state, "SELECT * FROM v2_sessions WHERE id = ?", root)) is (
                rollback or operation != "purge"
            )
            if rollback:
                assert await rows(state, "SELECT * FROM harness_sessions") == before_membership
                assert await rows(state, "SELECT * FROM ngn_web_session_trash") == before_trash

            def check_unlocked() -> None:
                with closing(sqlite3.connect(state.history.db_path, timeout=0.2)) as db:
                    db.execute("BEGIN EXCLUSIVE")
                    db.rollback()

            await finish_on_cancel(asyncio.to_thread(check_unlocked))

    asyncio.run(run())


def test_purge_keeps_content_free_channel_dedup_and_backup_restores_frozen_trash(tmp_path: Path, clock: Clock) -> None:
    def backup_database(source_path: Path, destination_path: Path) -> None:
        with closing(sqlite3.connect(source_path)) as source, closing(sqlite3.connect(destination_path)) as destination:
            source.backup(destination)

    async def run() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            await quiet(state)
            root = state.selected_session_id
            await state.history.add_message(root, Message(role="user", content="saved before backup"))

            def seed(db: sqlite3.Connection) -> None:
                db.execute(
                    "INSERT INTO ngn_web_inbox(session_id, channel, message_id, prompt, status) VALUES (?, 'fixture', 'late-redelivery', 'PRIVATE envelope', 'completed')",
                    (root,),
                )

            await state.channels.store._transaction(seed)
            item = (await client.request("DELETE", f"/api/sessions/{root}", headers=headers, json={})).json()["trash"]
            backup = tmp_path / "trash-backup.db"
            database = state.history.db_path
            await finish_on_cancel(asyncio.to_thread(backup_database, database, backup))
            await state.trash.restore(root, item["deletion_id"])
        await finish_on_cancel(asyncio.to_thread(backup_database, backup, database))
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            await quiet(state)
            assert (await client.get("/api/trash", headers=headers)).json()["items"] == [item]
            assert root not in {session.id for session in await state.harness.list_sessions()}
            clock.value = item["purge_at"]
            assert await state.trash.purge_expired() == [root]
            assert await rows(state, "SELECT * FROM ngn_web_deleted_messages") == [("fixture", "late-redelivery")]
            assert not await rows(state, "SELECT * FROM ngn_web_inbox")
            assert not await rows(state, "SELECT * FROM v2_messages WHERE session_id = ?", root)
            envelope = json.dumps(
                dict(
                    message_id="late-redelivery",
                    conversation_id="conversation",
                    thread_id="",
                    reply_to="",
                    text="PRIVATE envelope",
                )
            )
            before = await rows(state, "SELECT * FROM harness_sessions")
            await state.channels.store.receive("fixture", envelope, state.selected_session_id, None)
            assert await rows(state, "SELECT * FROM harness_sessions") == before
            assert not await rows(state, "SELECT * FROM ngn_web_inbox")

    asyncio.run(run())


def test_idle_purge_is_batch_bounded_and_never_prunes_recovered_active_membership(tmp_path: Path, clock: Clock) -> None:
    async def run() -> None:
        async with client_app(tmp_path) as (app, _, _, _):
            state = cast("WebState", app.state.web)
            await quiet(state)
            ids: list[str] = []
            for _ in range(PURGE_BATCH + 1):
                root = state.selected_session_id
                ids.append(root)
                await deletion.delete_session(state, root)
            protected = ids[-1]

            def recover(db: sqlite3.Connection) -> None:
                db.execute("INSERT INTO harness_sessions VALUES (?, 'Recovered active membership')", (protected,))

            await state.channels.store._transaction(recover)
            clock.value += 30 * DAY
            first = await state.trash.purge_expired()
            assert len(first) <= PURGE_BATCH and protected not in first
            second = await state.trash.purge_expired()
            assert len(first + second) == PURGE_BATCH
            assert await rows(state, "SELECT id FROM v2_sessions WHERE id = ?", protected) == [(protected,)]
            assert await rows(state, "SELECT title FROM harness_sessions WHERE id = ?", protected) == [
                ("Recovered active membership",)
            ]

    asyncio.run(run())


def test_periodic_purge_runs_at_idle_and_shutdown_joins_task(
    tmp_path: Path, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        async with client_app(tmp_path) as (app, _, _, _):
            state = cast("WebState", app.state.web)
            await quiet(state)
            root = state.selected_session_id
            await deletion.delete_session(state, root)
            original = state.trash.purge_expired
            cleaned = asyncio.Event()

            async def observed() -> list[str]:
                result = await original()
                if root in result:
                    cleaned.set()
                return result

            monkeypatch.setattr(state.trash, "purge_expired", observed)
            task = state.trash.task
            assert task is not None
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            state.trash.interval = 0.001
            state.trash.task = asyncio.create_task(state.trash._poll())
            clock.value += 30 * DAY
            await asyncio.wait_for(cleaned.wait(), HANG_GUARD)
            task = state.trash.task
            assert not task.done() and state.active is None
        assert task.done() and state.trash.closed and not state.mutating

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["trash", "restore"])
def test_joined_catalog_read_retries_after_membership_commit(
    tmp_path: Path, clock: Clock, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    async def run() -> None:
        async with client_app(tmp_path) as (app, _, _, _):
            state = cast("WebState", app.state.web)
            await quiet(state)
            root = state.selected_session_id
            generation = ""
            if operation == "restore":
                item = (await deletion.delete_session(state, root))["trash"]
                assert isinstance(item, dict)
                generation = str(item["deletion_id"])
            revision = state.session_revision
            entered, release = asyncio.Event(), asyncio.Event()
            original = state.harness.list_sessions
            reads = 0

            async def delayed() -> list[SessionInfo]:
                nonlocal reads
                result = await original()
                reads += 1
                if reads == 1:
                    assert (root in {item.id for item in result}) is (operation == "trash")
                    entered.set()
                    await release.wait()
                return result

            monkeypatch.setattr(state.harness, "list_sessions", delayed)
            reading = asyncio.create_task(state.list_sessions())
            await entered.wait()
            try:
                if operation == "trash":
                    await deletion.delete_session(state, root)
                else:
                    await state.trash.restore(root, generation)
                assert state.session_revision == revision + 1
            finally:
                release.set()
            assert (root in {item.id for item in await reading}) is (operation == "restore")
            assert reads >= 3  # stale reader, mutation response, and joined-reader retry

    asyncio.run(run())


def test_expiry_scan_moves_past_full_blocked_batch_and_revisits_repaired_rows(tmp_path: Path, clock: Clock) -> None:
    async def run() -> None:
        async with client_app(tmp_path) as (app, _, _, _):
            state = cast("WebState", app.state.web)
            await quiet(state)
            blocked: list[str] = []
            for _ in range(PURGE_BATCH):
                root = state.selected_session_id
                await state.history.add_message(root, Message(role="user", content="blocked history retained"))
                await deletion.delete_session(state, root)
                blocked.append(root)
                clock.value += 1
            healthy = state.selected_session_id
            await deletion.delete_session(state, healthy)

            def recover(db: sqlite3.Connection) -> None:
                db.executemany(
                    "INSERT INTO ngn_web_bindings VALUES ('fixture', ?, ?, ?)",
                    [(root, root, root) for root in blocked],
                )

            await state.channels.store._transaction(recover)
            clock.value += 30 * DAY
            assert await state.trash.purge_expired() == []
            assert await state.trash.purge_expired() == [healthy]
            assert len(await rows(state, "SELECT * FROM ngn_web_session_trash")) == PURGE_BATCH
            assert len(await rows(state, "SELECT * FROM ngn_web_bindings")) == PURGE_BATCH
            assert len(await rows(state, "SELECT * FROM v2_messages")) == PURGE_BATCH

            def repair(db: sqlite3.Connection) -> None:
                db.execute("DELETE FROM ngn_web_bindings")

            await state.channels.store._transaction(repair)
            assert set(await state.trash.purge_expired()) == set(blocked)
            assert not await rows(state, "SELECT * FROM ngn_web_session_trash")

    asyncio.run(run())


def test_expiry_rolls_back_one_root_failure_and_continues_other_roots(tmp_path: Path, clock: Clock) -> None:
    async def run() -> None:
        async with client_app(tmp_path) as (app, _, _, _):
            state = cast("WebState", app.state.web)
            await quiet(state)
            bad = state.selected_session_id
            await state.history.add_message(bad, Message(role="user", content="must survive failed purge"))
            await deletion.delete_session(state, bad)
            clock.value += 1
            healthy = state.selected_session_id
            await deletion.delete_session(state, healthy)

            def corrupt(db: sqlite3.Connection) -> None:
                db.execute("CREATE TABLE injected_trash_faults(id TEXT PRIMARY KEY)")
                db.execute("INSERT INTO injected_trash_faults VALUES (?)", (bad,))
                db.execute(
                    "CREATE TRIGGER fail_one_purge BEFORE DELETE ON v2_messages "
                    "WHEN EXISTS (SELECT 1 FROM injected_trash_faults WHERE id = OLD.session_id) "
                    "BEGIN SELECT RAISE(ABORT, 'injected root-specific purge failure'); END"
                )
                db.execute(
                    "INSERT INTO ngn_web_inbox(session_id, channel, message_id, prompt, status) "
                    "VALUES (?, 'fixture', 'retained-delivery', 'retained journal', 'completed')",
                    (bad,),
                )

            await state.channels.store._transaction(corrupt)
            before = await rows(state, "SELECT * FROM ngn_web_session_trash WHERE id = ?", bad)
            clock.value += 30 * DAY
            assert await state.trash.purge_expired() == [healthy]
            assert await rows(state, "SELECT * FROM ngn_web_session_trash WHERE id = ?", bad) == before
            assert await rows(state, "SELECT content FROM v2_messages WHERE session_id = ?", bad) == [
                ("must survive failed purge",)
            ]
            assert await rows(state, "SELECT prompt FROM ngn_web_inbox WHERE session_id = ?", bad) == [
                ("retained journal",)
            ]
            assert not await rows(state, "SELECT * FROM ngn_web_deleted_messages")
            assert not state.mutating and not state.harness._busy

    asyncio.run(run())
