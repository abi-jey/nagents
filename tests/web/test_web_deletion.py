"""Deterministic root deletion guards, routing races, and cancellation ownership."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from threading import Event
from typing import TYPE_CHECKING
from typing import cast

import pytest
from fastapi import HTTPException

from nagents.channels.store import InboxStore
from nagents.channels.store import finish_on_cancel
from nagents.harness.subagents import TaskInfo
from nagents.types import Message
from nagents.web import deletion
from nagents.web.catalog import Connection
from nagents.web.service import Run
from nagents.web.service import WebState
from nagents.web.subscriptions import Subscriber
from nagents.web.wakeups import Chain
from tests.support.hang_guard import HANG_GUARD
from tests.support.web import client_app
from tests.web.test_web_subscription_lifecycle import Peer

if TYPE_CHECKING:
    from pathlib import Path

    from nagents.harness.types import SessionInfo


async def quiet(state: WebState) -> None:
    """Control queue claims and catalog polling without timer-based races."""
    state.channels.closed = True
    for task in state.channels.tasks:
        task.cancel()
    await asyncio.gather(*state.channels.tasks, return_exceptions=True)


async def rows(state: WebState, sql: str, *parameters: str) -> list[tuple[object, ...]]:
    """Read off-loop so pending writers can finish event-loop-owned reader cleanup."""
    path = state.history.db_path

    def read() -> list[tuple[object, ...]]:
        with (
            closing(sqlite3.connect(path, timeout=0.2)) as db,
            closing(db.execute(sql, parameters)) as cursor,
        ):
            return cursor.fetchall()

    return await finish_on_cancel(asyncio.to_thread(read))


@pytest.mark.parametrize("selection", ["selected", "other", "last"])
def test_delete_only_root_rows_and_refresh_selection(tmp_path: Path, selection: str) -> None:
    async def run() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            await quiet(state)
            root = state.selected_session_id
            child = "ngn-hidden-child"
            await state.settings.change(state.settings.revision, state.settings.values)
            settings_before = await rows(state, "SELECT * FROM ngn_web_settings")
            await state.history.get_or_create_session(child, "harness")
            await state.history.add_message(child, Message(role="user", content="child kept"))
            await state.history.add_message(root, Message(role="user", content="root removed"))
            other = ""
            if selection != "last":
                response = await client.post("/api/sessions/new", headers=headers, json={})
                other = response.json()["session_id"]
                await state.history.add_message(other, Message(role="user", content="other kept"))
                if selection == "selected":
                    await client.post("/api/sessions/resume", headers=headers, json={"session_id": root})
            await state.channels.store.web(root, "test-message", "remove journal")

            def seed(db: sqlite3.Connection) -> None:
                db.execute("UPDATE ngn_web_inbox SET status = 'completed'")
                db.execute(
                    "INSERT INTO ngn_web_message_origins SELECT m.id, i.id FROM v2_messages m "
                    "JOIN ngn_web_inbox i ON i.session_id = m.session_id WHERE m.session_id = ?",
                    (root,),
                )
                db.execute("CREATE TABLE private_extension (value TEXT)")
                db.execute("INSERT INTO private_extension VALUES ('private configuration')")

            await state.channels.store._transaction(seed)
            subscriber = Subscriber(session_id=root, ready=True)
            state.bus.subscribers.add(subscriber)
            state.bus.event({"session_id": root, "event": "text_done", "text": "private old output"})
            state.wakeups.publish({"session_id": root, "event": "text_done", "text": "private old output"})
            protected = tmp_path / "credentials.json"
            protected.write_text("synthetic private credential")
            before = protected.read_bytes()
            response = await client.request(
                "DELETE", f"/api/sessions/{root}", headers=headers, json={"permanent": True}
            )
            assert response.status_code == 200, response.text
            result = response.json()
            selected = result["session_id"]
            assert selected != root and (not other or selected == other)
            assert result["deleted_session_id"] == root
            assert [item["id"] for item in result["sessions"]] == [selected]
            assert state.selected_session_id == state.harness.session_id == selected
            assert bool(result["history"]) == (selection != "last")
            for table, key in (
                ("v2_messages", "session_id"),
                ("v2_sessions", "id"),
                ("harness_sessions", "id"),
                ("ngn_web_inbox", "session_id"),
            ):
                assert not await rows(state, f"SELECT * FROM {table} WHERE {key} = ?", root)
            assert not await rows(state, "SELECT * FROM ngn_web_message_origins")
            assert await rows(state, "SELECT content FROM v2_messages WHERE session_id = ?", child) == [("child kept",)]
            assert await rows(state, "SELECT * FROM private_extension") == [("private configuration",)]
            assert await rows(state, "SELECT * FROM ngn_web_settings") == settings_before
            assert protected.read_bytes() == before
            assert subscriber.close_code == 1008 and not state.bus.listening(root)
            assert all(frame.get("session_id") != root for frame, _ in state.bus.ring)
            assert not state.wakeups.activity(root, 0, "")["events"]
            assert (await client.get("/api/sessions", headers=headers)).json()["session_id"] == selected
            for path in (f"sessions/{root}", f"activity/{root}/0"):
                assert (await client.get(f"/api/{path}", headers=headers)).status_code == 404
            assert (
                await client.post("/api/sessions/resume", headers=headers, json={"session_id": root})
            ).status_code == 404
            assert (
                await client.request("DELETE", f"/api/sessions/{root}", headers=headers, json={"permanent": True})
            ).status_code == 404
        async with client_app(tmp_path) as (_, client, headers, _):
            refreshed = (await client.get("/api/sessions", headers=headers)).json()
            assert root not in {item["id"] for item in refreshed["sessions"]}
            assert (
                await client.post("/api/sessions/resume", headers=headers, json={"session_id": selected})
            ).status_code == 200

    asyncio.run(run())


@pytest.mark.parametrize("guard", ["active", "busy", "descendant", "retained", "wakeup"])
def test_delete_rejects_process_owned_work(tmp_path: Path, guard: str) -> None:
    async def run() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            await quiet(state)
            root = state.selected_session_id
            if guard == "active":
                state.active = Run("ngn-other-active")
            elif guard == "busy":
                state.harness._busy = "compaction"
            elif guard in {"descendant", "retained"}:
                state.harness.tasks._infos["task"] = TaskInfo(
                    "task", "child", session_id=root, status="running" if guard == "descendant" else "completed"
                )
            else:
                state.wakeups.schedule(root, "run", Chain(), "", 86400, "later")
            try:
                response = await client.request(
                    "DELETE", f"/api/sessions/{root}", headers=headers, json={"permanent": True}
                )
                assert response.status_code == 409 and "private" not in response.text
                assert await rows(state, "SELECT id FROM harness_sessions WHERE id = ?", root)
                assert state.selected_session_id == root and not state.mutating
            finally:
                state.active = None
                state.harness._busy = ""
                state.harness.tasks._infos.clear()

    asyncio.run(run())


@pytest.mark.parametrize("enabled", [True, False])
def test_delete_repoints_configured_channel_main_and_keeps_connector(tmp_path: Path, enabled: bool) -> None:
    async def run() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            await quiet(state)
            root = state.selected_session_id
            await state.harness.new_session()
            state.channels.catalog.connections["fixture"] = Connection(
                plugin="fixture",
                enabled=enabled,
                config={},
                secrets={"key": "private"},
                main_session_id=root,
            )
            source = asyncio.create_task(asyncio.sleep(3600))
            state.channels.sources["fixture"] = source
            try:
                response = await client.request(
                    "DELETE", f"/api/sessions/{root}", headers=headers, json={"permanent": True}
                )
                assert response.status_code == 200, response.text
                connection = state.channels.catalog.connections["fixture"]
                assert connection.enabled is enabled
                assert connection.main_session_id == response.json()["session_id"]
                assert connection.main_session_id != root
                assert state.channels.sources.get("fixture") is source and not source.done()
            finally:
                source.cancel()
                await asyncio.gather(source, return_exceptions=True)
                state.channels.sources.pop("fixture", None)

    asyncio.run(run())


@pytest.mark.parametrize("table", ["ngn_web_inbox", "nagents_channel_inbox"])
@pytest.mark.parametrize("status", ["queued", "running"])
def test_delete_releases_durable_work(tmp_path: Path, table: str, status: str) -> None:
    async def run() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            await quiet(state)
            root = state.selected_session_id
            if table == "ngn_web_inbox":
                await state.channels.store.web(root, "queued-id", "keep work")
            else:
                store = InboxStore(state.history.db_path, root, 10)
                await store.initialize()
                await store.admit("fixture", "queued-id", "keep work")

            def seed(db: sqlite3.Connection) -> None:
                db.execute(f"UPDATE {table} SET status = ?", (status,))

            await state.channels.store._transaction(seed)
            response = await client.request(
                "DELETE", f"/api/sessions/{root}", headers=headers, json={"permanent": True}
            )
            assert response.status_code == 200, response.text
            assert not await rows(state, f"SELECT * FROM {table} WHERE session_id = ?", root)

    asyncio.run(run())


@pytest.mark.parametrize("case", ["token", "origin", "crosssite", "child", "missing", "malformed", "orphan", "foreign"])
def test_delete_auth_and_membership(tmp_path: Path, case: str) -> None:
    async def run() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            await quiet(state)
            root = state.selected_session_id
            target = root
            expected = 403
            if case == "token":
                headers = {**headers, "X-Ngn-Token": "wrong"}
            elif case == "origin":
                headers = {key: value for key, value in headers.items() if key != "Origin"}
            elif case == "crosssite":
                headers = {**headers, "Origin": "https://foreign.invalid"}
            else:
                target = "bad-id" if case == "malformed" else f"ngn-{case}"
                expected = 422 if case == "malformed" else 404
                if case == "child":
                    await state.history.get_or_create_session(target, "harness")
                elif case == "orphan":

                    def seed(db: sqlite3.Connection) -> None:
                        db.execute("INSERT INTO harness_sessions VALUES (?, '')", (target,))

                    await state.channels.store._transaction(seed)
                elif case == "foreign":
                    foreign = tmp_path / "foreign.db"
                    with closing(sqlite3.connect(foreign)) as db, db:
                        db.execute("CREATE TABLE harness_sessions(id TEXT)")
                        db.execute("INSERT INTO harness_sessions VALUES (?)", (target,))
            response = await client.request(
                "DELETE", f"/api/sessions/{target}", headers=headers, json={"permanent": True}
            )
            assert response.status_code == expected, response.text
            assert await rows(state, "SELECT id FROM harness_sessions WHERE id = ?", root)

    asyncio.run(run())


@pytest.mark.parametrize("rollback", [False, True])
def test_cancel_during_transaction_joins_atomic_outcome_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rollback: bool
) -> None:
    async def run() -> None:
        async with client_app(tmp_path) as (app, _, _, _):
            state = cast("WebState", app.state.web)
            await quiet(state)
            root = state.selected_session_id
            await state.history.add_message(root, Message(role="user", content="original"))
            entered, release = Event(), Event()
            original = deletion._delete_rows

            def blocked(db: sqlite3.Connection, id: str, selected: str) -> str:
                result = original(db, id, selected)
                entered.set()
                assert release.wait(HANG_GUARD)
                if rollback:
                    raise ValueError("injected transaction failure")
                return result

            monkeypatch.setattr(deletion, "_delete_rows", blocked)
            task = asyncio.create_task(deletion.delete_session(state, root, permanent=True))
            assert await asyncio.to_thread(entered.wait, 5)
            try:
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                assert not task.done() and state.mutating
                assert await rows(state, "SELECT content FROM v2_messages WHERE session_id = ?", root) == [
                    ("original",)
                ]
                assert await rows(state, "SELECT id FROM harness_sessions") == [(root,)]
            finally:
                release.set()
            with pytest.raises(ValueError if rollback else asyncio.CancelledError):
                await task
            assert not state.mutating and not state.harness._busy
            roots = await rows(state, "SELECT id FROM harness_sessions")
            assert len(roots) == 1
            assert (roots == [(root,)]) is rollback
            assert state.selected_session_id == state.harness.session_id == roots[0][0]

            def check_unlocked() -> None:
                with closing(sqlite3.connect(state.history.db_path, timeout=0.2)) as db:
                    db.execute("BEGIN EXCLUSIVE")  # no orphan connections/read cursors
                    db.rollback()

            await finish_on_cancel(asyncio.to_thread(check_unlocked))

    asyncio.run(run())


def test_admission_racing_delete_cannot_recreate_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        async with client_app(tmp_path) as (app, _, _, _):
            state = cast("WebState", app.state.web)
            await quiet(state)
            root = state.selected_session_id
            entered, release = Event(), Event()
            original = deletion._delete_rows

            def blocked(db: sqlite3.Connection, id: str, selected: str) -> str:
                entered.set()
                assert release.wait(HANG_GUARD)
                return original(db, id, selected)

            monkeypatch.setattr(deletion, "_delete_rows", blocked)
            task = asyncio.create_task(deletion.delete_session(state, root, permanent=True))
            assert await asyncio.to_thread(entered.wait, 5)
            admitted = asyncio.create_task(state.channels.store.web(root, "racing-message", "must not run"))
            release.set()
            await task
            with pytest.raises(HTTPException) as error:
                await admitted
            assert error.value.status_code == 404
            assert not await rows(state, "SELECT * FROM ngn_web_inbox WHERE session_id = ?", root)

    asyncio.run(run())


def test_binding_deletion_reroots_chat_and_deleted_redelivery_dedups(tmp_path: Path) -> None:
    async def run() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            await quiet(state)
            other = state.selected_session_id
            store = state.channels.store
            await store.assign_owner(other, "fixture", "conversation")

            def envelope(id: str, text: str = "private") -> str:
                return json.dumps(
                    dict(message_id=id, conversation_id="conversation", thread_id="", reply_to="", text=text)
                )

            await store.receive("fixture", envelope("original"), other, None)
            root = (await store.bindings())[0]["session_id"]

            async def complete() -> None:
                def finish(db: sqlite3.Connection) -> None:
                    db.execute("UPDATE ngn_web_inbox SET status = 'completed'")

                await store._transaction(finish)

            await complete()
            # Permanent deletion of the bound root wins: the chat is re-rooted,
            # not blocked, and its ownership evidence is retained.
            response = await client.request(
                "DELETE", f"/api/sessions/{root}", headers=headers, json={"permanent": True}
            )
            assert response.status_code == 200, response.text
            assert not await rows(
                state, "SELECT * FROM ngn_web_bindings WHERE channel = 'fixture' AND conversation_id = 'conversation'"
            )
            assert await rows(
                state, "SELECT owner.session_id FROM ngn_web_session_owners owner WHERE session_id = ?", root
            ) == [(root,)]
            # A late redelivery of the deleted message is deduplicated, not re-executed.
            await store.receive("fixture", envelope("original"), other, None)
            assert not await rows(state, "SELECT * FROM ngn_web_inbox WHERE message_id = 'original'")
            assert await rows(state, "SELECT * FROM ngn_web_deleted_messages") == [("fixture", "original")]
            # The next fresh inbound creates a new chat root and is admitted as normal input.
            await store.receive("fixture", envelope("fresh", "real request"), other, None)
            bindings = await store.bindings()
            assert len(bindings) == 1 and bindings[0]["session_id"] != root
            assert await rows(state, "SELECT acknowledgement FROM ngn_web_inbox WHERE message_id = 'fresh'") == [("",)]
            work = await store.claim_work()
            assert work is not None and work.message_id == "fresh" and work.session_id != root

    asyncio.run(run())


def test_deleting_only_a_binding_default_keeps_chat_on_surviving_root(tmp_path: Path) -> None:
    async def run() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            await quiet(state)
            root = state.selected_session_id
            default_root = await state.harness.new_session()

            def bind(db: sqlite3.Connection) -> None:
                db.execute(
                    "INSERT INTO ngn_web_bindings VALUES ('fixture', 'conversation', ?, ?)", (root, default_root)
                )

            await state.channels.store._transaction(bind)
            response = await client.request(
                "DELETE", f"/api/sessions/{default_root}", headers=headers, json={"permanent": True}
            )
            assert response.status_code == 200, response.text
            assert await rows(state, "SELECT * FROM ngn_web_bindings") == [("fixture", "conversation", root, root)]

    asyncio.run(run())


@pytest.mark.parametrize("permanent", [False, True])
def test_delete_succeeds_while_bound_and_quarantines_pending_work(tmp_path: Path, permanent: bool) -> None:
    async def run() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            await quiet(state)
            other = state.selected_session_id
            root = await state.harness.new_session()

            def bind(db: sqlite3.Connection) -> None:
                db.execute("INSERT INTO ngn_web_bindings VALUES ('fixture', 'conversation', ?, ?)", (root, root))

            await state.channels.store._transaction(bind)

            def pending(db: sqlite3.Connection) -> None:
                db.execute(
                    "INSERT INTO ngn_web_inbox(session_id, channel, message_id, prompt, status) "
                    "VALUES (?, 'fixture', 'late', 'PRIVATE envelope', 'queued')",
                    (root,),
                )

            await state.channels.store._transaction(pending)
            response = await client.request(
                "DELETE", f"/api/sessions/{root}", headers=headers, json={"permanent": permanent}
            )
            assert response.status_code == 200, response.text
            assert not await rows(
                state, "SELECT * FROM ngn_web_bindings WHERE session_id = ? OR default_session_id = ?", root, root
            )
            if permanent:
                assert not await rows(state, "SELECT * FROM ngn_web_inbox WHERE session_id = ?", root)
            else:
                # Soft delete keeps history rows but marks pending work terminal.
                assert await rows(state, "SELECT status FROM ngn_web_inbox WHERE session_id = ?", root) == [
                    ("interrupted",)
                ]
            assert await rows(state, "SELECT * FROM ngn_web_deleted_messages") == [("fixture", "late")]
            # A late connector redelivery must not re-execute the deleted work.
            envelope = json.dumps(
                dict(message_id="late", conversation_id="conversation", thread_id="", reply_to="", text="PRIVATE")
            )
            await state.channels.store.receive("fixture", envelope, other, None)
            assert not await rows(state, "SELECT * FROM ngn_web_inbox WHERE message_id = 'late' AND status = 'queued'")
            assert await state.channels.store.claim_work() is None

    asyncio.run(run())


def test_deleted_root_revokes_live_and_reconnecting_subscribers(tmp_path: Path) -> None:
    async def run() -> None:
        async with client_app(tmp_path) as (app, _, _, _):
            state = cast("WebState", app.state.web)
            await quiet(state)
            root = state.selected_session_id
            peer = Peer()
            task = await peer.start(state.bus, state.snapshot, state.disconnected)
            peer.subscribe(root)
            frame = await peer.frame()
            await deletion.delete_session(state, root, permanent=True)
            assert (await peer.message())["code"] == 1008
            await task
            peer = Peer()
            task = await peer.start(state.bus, state.snapshot, state.disconnected)
            peer.subscribe(root, after=frame["cursor"], epoch=frame["epoch"])
            assert (await peer.message())["code"] == 1008
            await task
            assert not state.bus.subscribers

    asyncio.run(run())


def test_crossroot_origin_corruption_rejects_without_removing_other_history(tmp_path: Path) -> None:
    async def run() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            await quiet(state)
            root = state.selected_session_id
            await state.history.get_or_create_session("ngn-child", "harness")
            message = await state.history.add_message("ngn-child", Message(role="user", content="unrelated"))
            await state.channels.store.web(root, "terminal", "old work")

            def corrupt(db: sqlite3.Connection) -> None:
                db.execute("UPDATE ngn_web_inbox SET status = 'completed'")
                db.execute("INSERT INTO ngn_web_message_origins SELECT ?, id FROM ngn_web_inbox", (message,))

            await state.channels.store._transaction(corrupt)
            before = await rows(state, "SELECT * FROM ngn_web_message_origins")
            response = await client.request(
                "DELETE", f"/api/sessions/{root}", headers=headers, json={"permanent": True}
            )
            assert response.status_code == 409 and "cross-session" in response.text
            assert await rows(state, "SELECT * FROM ngn_web_message_origins") == before
            assert await rows(state, "SELECT content FROM v2_messages") == [("unrelated",)]
            assert await rows(state, "SELECT id FROM harness_sessions") == [(root,)]

    asyncio.run(run())


def test_unrelated_pending_work_bindings_and_retained_tasks_are_preserved(tmp_path: Path) -> None:
    async def run() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            await quiet(state)
            root = state.selected_session_id
            other = await state.harness.new_session()
            state.harness.tasks._infos["other-task"] = TaskInfo(
                "other-task", "other child", session_id=other, status="completed"
            )
            state.wakeups.schedule(other, "other-run", Chain(), "", 86400, "unrelated wakeup")
            await state.channels.store.web(other, "other-message", "do not drop")

            def bind(db: sqlite3.Connection) -> None:
                db.execute("INSERT INTO ngn_web_bindings VALUES ('fixture', 'other', ?, ?)", (other, other))
                db.execute("INSERT INTO ngn_web_bindings VALUES ('fixture', 'root', ?, ?)", (root, root))

            await state.channels.store._transaction(bind)
            try:
                response = await client.request(
                    "DELETE", f"/api/sessions/{root}", headers=headers, json={"permanent": True}
                )
                assert response.status_code == 200
                assert await rows(state, "SELECT prompt, status FROM ngn_web_inbox") == [("do not drop", "queued")]
                bindings = await state.channels.store.bindings()
                assert [entry["conversation_id"] for entry in bindings] == ["other"]
                assert bindings[0]["session_id"] == other
                assert "other-task" in state.harness.tasks._infos
                assert len(state.wakeups.pending) == 1
            finally:
                state.harness.tasks._infos.clear()

    asyncio.run(run())


def test_catalog_read_racing_deletion_cannot_publish_deleted_membership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        async with client_app(tmp_path) as (app, _, _, _):
            state = cast("WebState", app.state.web)
            await quiet(state)
            root = state.selected_session_id
            entered, release = asyncio.Event(), asyncio.Event()
            original = state.harness.list_sessions
            reads = 0

            async def delayed() -> list[SessionInfo]:
                nonlocal reads
                result = await original()
                reads += 1
                if reads == 1:
                    entered.set()
                    await release.wait()
                return result

            monkeypatch.setattr(state.harness, "list_sessions", delayed)
            reading = asyncio.create_task(state.list_sessions())
            await entered.wait()
            try:
                await deletion.delete_session(state, root, permanent=True)
            finally:
                release.set()
            assert root not in {item.id for item in await reading}

    asyncio.run(run())


def test_delete_revokes_hydration_before_its_late_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def run() -> None:
        async with client_app(tmp_path) as (app, _, _, _):
            state = cast("WebState", app.state.web)
            await quiet(state)
            root = state.selected_session_id
            entered, release = asyncio.Event(), asyncio.Event()
            original = state.history.snapshot

            async def delayed(id: str) -> list[dict[str, object]]:
                result = await original(id)
                if id == root:
                    entered.set()
                    await release.wait()
                return result

            monkeypatch.setattr(state.history, "snapshot", delayed)
            peer = Peer()
            task = await peer.start(state.bus, state.snapshot, state.disconnected)
            peer.subscribe(root)
            await entered.wait()
            await deletion.delete_session(state, root, permanent=True)
            assert not state.bus.listening(root)
            release.set()
            assert (await peer.message())["code"] == 1008
            await task
            assert peer.outgoing.empty() and not state.bus.subscribers

    asyncio.run(run())
