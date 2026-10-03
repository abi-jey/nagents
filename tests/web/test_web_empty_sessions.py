"""Unused web drafts are reused or discarded without removing meaningful roots."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from typing import cast
from unittest.mock import PropertyMock
from unittest.mock import patch

import pytest

from nagents.channels.store import InboxStore
from nagents.harness.config import HarnessConfig
from nagents.harness.subagents import TaskInfo
from nagents.types import Message
from nagents.web import empty_sessions
from nagents.web.app import create_app
from nagents.web.catalog import Connection
from nagents.web.routing import RoutingStore
from nagents.web.service import Run
from nagents.web.service import WebState
from nagents.web.subscriptions import Subscriber
from nagents.web.trash import SessionTrash
from nagents.web.wakeups import Chain
from tests.support.web import ControlledHarness
from tests.support.web import client_app
from tests.web.test_web_deletion import quiet

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path


async def roots(state: WebState) -> set[str]:
    return await state.channels.store._transaction(
        lambda db: {str(row[0]) for row in db.execute("SELECT id FROM harness_sessions")}
    )


async def draft(state: WebState, title: str = "") -> str:
    return await state.channels.store._transaction(lambda db: RoutingStore.new_root(db, title))


def test_repeated_new_reuses_selected_empty_draft(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            selected = state.selected_session_id
            for _ in range(4):
                response = await client.post("/api/sessions/new", headers=headers, json={})
                assert response.status_code == 200, response.text
                assert response.json()["session_id"] == selected
                assert [item["id"] for item in response.json()["sessions"]] == [selected]
            assert await roots(state) == {selected}
            assert state.harness.session_id == selected

    asyncio.run(scenario())


def test_leaving_empty_draft_discards_it_and_keeps_used_conversation(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            used = state.selected_session_id
            await state.history.add_message(used, Message(role="user", content="Keep this conversation"))
            response = await client.post("/api/sessions/new", headers=headers, json={})
            empty = response.json()["session_id"]
            assert empty != used
            resumed = await client.post("/api/sessions/resume", headers=headers, json={"session_id": used})
            assert resumed.status_code == 200, resumed.text
            assert [item["id"] for item in resumed.json()["sessions"]] == [used]
            assert (await client.get(f"/api/sessions/{empty}", headers=headers)).status_code == 404
            assert await roots(state) == {used}
            assert (await state.history.get_history(used))[0].content == "Keep this conversation"

    asyncio.run(scenario())


def test_restarts_prune_abandoned_drafts_but_preserve_named_and_used_roots(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, _, _, _):
            state = cast("WebState", app.state.web)
            used = state.selected_session_id
            await state.history.add_message(used, Message(role="user", content="Saved before restart"))
            named = await draft(state, "An intentional empty project")
            old_drafts = {await draft(state) for _ in range(3)}
        for _ in range(3):
            async with client_app(tmp_path) as (app, client, headers, _):
                state = cast("WebState", app.state.web)
                response = await client.get("/api/sessions", headers=headers)
                selected = response.json()["session_id"]
                assert await roots(state) == {used, named, selected}
                assert not (old_drafts & await roots(state))
                old_drafts.add(selected)

    asyncio.run(scenario())


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("caption_only", [False, True])
def test_continue_skips_unused_draft_but_explicit_resume_keeps_it(
    tmp_path: Path, explicit: bool, caption_only: bool
) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, _, _, _):
            state = cast("WebState", app.state.web)
            used = state.selected_session_id
            if caption_only:
                observe = await state.bind_live_captions(used, "used-call")
                await observe(
                    {
                        "type": "transcript",
                        "seq": 1,
                        "speaker": "user",
                        "text": "Remember this",
                        "start_ms": 0,
                        "end_ms": 1,
                    }
                )
            else:
                await state.history.add_message(used, Message(role="user", content="Remember this"))
            unused = await draft(state)
            assert (await state.harness.list_sessions())[0].id == unused
        app = create_app(
            HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "data", demo=True),
            assets=tmp_path / "static",
            harness_factory=ControlledHarness,
            continue_session=not explicit,
            resume_session=unused if explicit else "",
        )
        async with app.router.lifespan_context(app):
            state = cast("WebState", app.state.web)
            assert state.selected_session_id == (unused if explicit else used)
            assert await roots(state) == ({used, unused} if explicit else {used})

    asyncio.run(scenario())


@pytest.mark.parametrize("existing", [False, True])
def test_continue_without_meaningful_history_keeps_exactly_one_draft(tmp_path: Path, existing: bool) -> None:
    async def scenario() -> None:
        config = HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "data", demo=True)
        latest = ""
        if existing:
            async with client_app(tmp_path) as (app, _, _, _):
                state = cast("WebState", app.state.web)
                await draft(state)
                latest = await draft(state)
        assets = tmp_path / "static"
        (assets / "assets").mkdir(parents=True, exist_ok=True)
        app = create_app(config, assets=assets, harness_factory=ControlledHarness, continue_session=True)
        async with app.router.lifespan_context(app):
            state = cast("WebState", app.state.web)
            selected = state.selected_session_id
            assert selected and (not latest or selected == latest)
            assert await roots(state) == {selected}
            assert await state.history.snapshot(selected) == []

    asyncio.run(scenario())


def test_caption_only_and_system_message_conversations_are_not_empty(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            spoken = state.selected_session_id
            observe = await state.bind_live_captions(spoken, "caption-call")
            await observe(
                {"type": "transcript", "seq": 1, "speaker": "user", "text": "Hello", "start_ms": 0, "end_ms": 1}
            )
            system = await draft(state)
            await state.history.add_message(system, Message(role="system", content="Retain hidden context too"))
            blank = await draft(state)
            response = await client.post("/api/sessions/new", headers=headers, json={})
            selected = response.json()["session_id"]
            assert selected != spoken
            assert await roots(state) == {selected, spoken, system}
            assert blank not in await roots(state)
            assert (await state.history.snapshot(spoken))[0]["content"] == "Hello"

    asyncio.run(scenario())


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("status", ["queued", "running", "completed"])
def test_inbox_work_is_preserved_before_model_history_is_written(tmp_path: Path, legacy: bool, status: str) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            await quiet(state)
            selected = state.selected_session_id
            if legacy:
                inbox = InboxStore(state.history.db_path, selected, 10)
                await inbox.initialize()
                await inbox.admit("fixture", "pending-input", "Unprocessed caller request")
                table = "nagents_channel_inbox"
            else:
                await state.channels.store.web(selected, "pending-input", "Unprocessed caller request")
                table = "ngn_web_inbox"
            await state.channels.store._transaction(lambda db: db.execute(f"UPDATE {table} SET status = ?", (status,)))
            response = await client.post("/api/sessions/new", headers=headers, json={})
            assert response.status_code == 200, response.text
            assert response.json()["session_id"] != selected
            assert selected in await roots(state)
            assert (
                await state.channels.store._transaction(
                    lambda db: db.execute(f"SELECT COUNT(*) FROM {table} WHERE session_id = ?", (selected,)).fetchone()[
                        0
                    ]
                )
                == 1
            )

    asyncio.run(scenario())


def test_active_voice_protects_an_empty_root_but_closed_empty_voice_does_not(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            spoken = state.selected_session_id
            await state.bind_live_captions(spoken, "active-empty-call")
            # Voice admission pins even a main assistant that has no custom design.
            await state.designed_channels.pin(spoken)
            with patch.object(
                type(app.state.live), "active_session_id", new_callable=PropertyMock, return_value="active-empty-call"
            ):
                response = await client.post("/api/sessions/new", headers=headers, json={})
                selected = response.json()["session_id"]
                assert selected != spoken
                assert await roots(state) == {selected, spoken}
                assert (await client.get("/api/sessions", headers=headers)).status_code == 200
                assert spoken in await roots(state)
            response = await client.get("/api/sessions", headers=headers)
            assert [item["id"] for item in response.json()["sessions"]] == [selected]
            assert (
                await state.channels.store._transaction(
                    lambda db: db.execute(
                        "SELECT COUNT(*) FROM ngn_web_live_calls WHERE voice_session_id = 'active-empty-call'"
                    ).fetchone()[0]
                )
                == 0
            )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "reference", ["task", "wakeup", "subscriber", "main", "owner", "binding", "design", "upload", "delivery"]
)
def test_referenced_empty_roots_are_preserved(tmp_path: Path, reference: str) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            await quiet(state)
            protected = await draft(state)
            unused = await draft(state)
            if reference == "task":
                state.harness.tasks._infos["retained"] = TaskInfo(
                    "retained", "child", session_id=protected, status="completed"
                )
            elif reference == "wakeup":
                state.wakeups.schedule(protected, "run", Chain(), "", 86400, "later")
            elif reference == "subscriber":
                state.bus.subscribers.add(Subscriber(session_id=protected, ready=True))
            elif reference == "main":
                state.channels.catalog.connections["fixture"] = Connection(
                    plugin="fixture", enabled=False, config={}, secrets={}, main_session_id=protected
                )
            else:

                def seed(db: sqlite3.Connection) -> None:
                    if reference == "owner":
                        db.execute("INSERT INTO ngn_web_session_owners VALUES (?, 'fixture', 'chat', 0)", (protected,))
                    elif reference == "binding":
                        db.execute(
                            "INSERT INTO ngn_web_bindings VALUES ('fixture', 'chat', ?, ?)", (protected, protected)
                        )
                    elif reference == "design":
                        db.execute(
                            "INSERT INTO ngn_design_sessions VALUES (?, 'custom', 'saved definition')", (protected,)
                        )
                    elif reference == "upload":
                        db.execute(
                            "INSERT INTO ngn_web_uploads VALUES ('upload', ?, 'draft.txt', 'text/plain', 1, ?, 1e12, 0, 0)",
                            (protected, b"x"),
                        )
                    else:
                        db.execute(
                            "INSERT INTO ngn_local_deliveries (delivery_id, channel, root_session_id, actor_session_id, host_run_id, turn_id, task_id, activation, invocation_id, anchor_message_id, call_position, call_id, tool_name, text, created_at) "
                            "VALUES ('delivery', 'builtin.web', ?, ?, 'run', 'turn', '', 0, 'invocation', 1, 0, 'call', 'channel_send', 'Delivered content', 'now')",
                            (protected, protected),
                        )

                await state.channels.store._transaction(seed)
            response = await client.get("/api/sessions", headers=headers)
            assert response.status_code == 200, response.text
            assert protected in await roots(state)
            assert unused not in await roots(state)
            state.harness.tasks._infos.clear()

    asyncio.run(scenario())


def test_pruning_never_deletes_raw_children_foreign_users_non_ngn_or_trashed_roots(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            child = "ngn-raw-child"
            await state.history.get_or_create_session(child, "harness")
            foreign = await draft(state)
            other = "private-session"
            await state.history.get_or_create_session(other, "harness")

            def seed(db: sqlite3.Connection) -> None:
                db.execute("UPDATE v2_sessions SET user_id = 'another-application' WHERE id = ?", (foreign,))
                db.execute("INSERT INTO harness_sessions VALUES (?, '')", (other,))

            await state.channels.store._transaction(seed)
            trashed = await draft(state)
            deleted = await client.request("DELETE", f"/api/sessions/{trashed}", headers=headers, json={})
            assert deleted.status_code == 200
            unused = await draft(state)
            response = await client.get("/api/sessions", headers=headers)
            assert response.status_code == 200
            assert {foreign, other}.issubset(await roots(state))
            for identifier in (child, foreign, other, trashed):
                assert await state.history.session_exists(identifier)
            assert not await state.history.session_exists(unused)
            trash_items = cast("list[dict[str, object]]", (await state.trash.snapshot())["items"])
            assert any(item["id"] == trashed for item in trash_items)

    asyncio.run(scenario())


def test_pruning_defers_during_active_work(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, _, _, _):
            state = cast("WebState", app.state.web)
            unused = await draft(state)
            state.active = Run(unused, server_owned=True)
            try:
                with state.idle(allow_running=True):
                    assert await empty_sessions.prune(state) == []
                assert unused in await roots(state)
            finally:
                state.active = None
            with state.idle():
                assert await empty_sessions.prune(state) == [unused]
                assert await empty_sessions.prune(state) == []

    asyncio.run(scenario())


def test_background_child_protects_its_root_but_does_not_prevent_other_draft_reuse(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            await quiet(state)
            parent = state.selected_session_id

            async def pending_child() -> None:
                await asyncio.Event().wait()

            worker = asyncio.create_task(pending_child())
            state.harness.tasks._infos["child"] = TaskInfo("child", "child", session_id=parent, status="running")
            state.harness.tasks._workers["child"] = worker
            try:
                first = await client.post("/api/sessions/new", headers=headers, json={})
                assert first.status_code == 200
                selected = first.json()["session_id"]
                assert selected != parent, "The task's own empty root must not be reused"
                second = await client.post("/api/sessions/new", headers=headers, json={})
                assert second.status_code == 200
                assert second.json()["session_id"] == selected
                unused = await draft(state)
                await client.get("/api/sessions", headers=headers)
                assert await roots(state) == {parent, selected, unused}, "Pruning waits until the child stops"
            finally:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
                state.harness.tasks._workers.clear()
                state.harness.tasks._infos.clear()

    asyncio.run(scenario())


def test_startup_cleanup_defers_when_recovered_work_already_owns_a_mutation(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, _, _, _):
            original = cast("WebState", app.state.web).selected_session_id
        start = SessionTrash.start

        async def recovered_work(self: SessionTrash) -> None:
            await start(self)
            self.state.mutating = True

        with patch.object(SessionTrash, "start", recovered_work):
            async with client_app(tmp_path) as (app, client, headers, _):
                state = cast("WebState", app.state.web)
                assert state.mutating and original in await roots(state)
                state.mutating = False
                response = await client.get("/api/sessions", headers=headers)
                assert response.status_code == 200
                assert await roots(state) == {state.selected_session_id}
                assert original != state.selected_session_id

    asyncio.run(scenario())
