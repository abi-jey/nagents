"""Chat folders survive history lifecycle without owning or interrupting runs."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING
from typing import cast

import pytest

from nagents.types import Message
from tests.support.web import LiveStream
from tests.support.web import client_app

if TYPE_CHECKING:
    from pathlib import Path

    import httpx

    from nagents.web.service import WebState


async def create_folder(client: httpx.AsyncClient, headers: dict[str, str], name: str, parent: str = "") -> str:
    before = (await client.get("/api/session-groups", headers=headers)).json()
    response = await client.post(
        "/api/session-groups/create",
        headers=headers,
        json={"revision": before["revision"], "name": name, "parent_id": parent},
    )
    assert response.status_code == 200, response.text
    return str(
        next(
            group["id"] for group in response.json()["groups"] if group["name"] == name and group["parent_id"] == parent
        )
    )


def test_nested_folders_reparent_rename_and_sibling_uniqueness(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (_, client, headers, _):
            first = await create_folder(client, headers, "First")
            second = await create_folder(client, headers, "Second")
            notes = await create_folder(client, headers, "Notes", first)
            other_notes = await create_folder(client, headers, "Notes", second)
            current = (await client.get("/api/session-groups", headers=headers)).json()
            for endpoint, body in (
                ("create", {"name": "NOTES", "parent_id": first}),
                ("reparent", {"group_id": notes, "parent_id": second}),
            ):
                response = await client.post(
                    f"/api/session-groups/{endpoint}", headers=headers, json={"revision": current["revision"], **body}
                )
                assert response.status_code == 409
                assert (await client.get("/api/session-groups", headers=headers)).json() == current
            renamed = await client.post(
                "/api/session-groups/update",
                headers=headers,
                json={"revision": current["revision"], "group_id": notes, "name": "Research", "collapsed": True},
            )
            assert renamed.status_code == 200
            changed = next(group for group in renamed.json()["groups"] if group["id"] == notes)
            assert changed == {"id": notes, "name": "Research", "collapsed": True, "parent_id": first}
            moved = await client.post(
                "/api/session-groups/reparent",
                headers=headers,
                json={"revision": renamed.json()["revision"], "group_id": notes, "parent_id": second},
            )
            assert moved.status_code == 200
            assert {group["id"] for group in moved.json()["groups"] if group["parent_id"] == second} == {
                notes,
                other_notes,
            }
            before_restart = moved.json()
        async with client_app(tmp_path) as (_, client, headers, _):
            assert (await client.get("/api/session-groups", headers=headers)).json() == before_restart

    asyncio.run(scenario())


def test_folder_tree_rejects_cycles_missing_parents_and_deep_subtrees_atomically(tmp_path: Path) -> None:
    from nagents.web.session_groups import MAX_GROUP_DEPTH

    async def scenario() -> None:
        async with client_app(tmp_path) as (_, client, headers, _):
            chain: list[str] = []
            for depth in range(MAX_GROUP_DEPTH):
                chain.append(await create_folder(client, headers, f"Level {depth}", chain[-1] if chain else ""))
            branch = await create_folder(client, headers, "Branch")
            await create_folder(client, headers, "Leaf", branch)
            current = (await client.get("/api/session-groups", headers=headers)).json()
            cases = [
                ("reparent", {"group_id": chain[0], "parent_id": chain[-1]}, 422),
                ("reparent", {"group_id": branch, "parent_id": branch}, 422),
                ("reparent", {"group_id": branch, "parent_id": chain[-2]}, 422),
                ("reparent", {"group_id": branch, "parent_id": "group-" + "f" * 32}, 404),
                ("create", {"name": "Too deep", "parent_id": chain[-1]}, 422),
                ("create", {"name": "Missing", "parent_id": "group-" + "f" * 32}, 404),
            ]
            for endpoint, body, status in cases:
                response = await client.post(
                    f"/api/session-groups/{endpoint}", headers=headers, json={"revision": current["revision"], **body}
                )
                assert response.status_code == status, response.text
                assert (await client.get("/api/session-groups", headers=headers)).json() == current

    asyncio.run(scenario())


def test_removing_nested_folder_promotes_children_and_trashed_chat_memberships(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            root = state.selected_session_id
            await state.history.add_message(root, Message(role="user", content="Keep nested conversation"))
            parent = await create_folder(client, headers, "Parent")
            middle = await create_folder(client, headers, "Middle", parent)
            child = await create_folder(client, headers, "Leaf", middle)
            current = (await client.get("/api/session-groups", headers=headers)).json()
            moved = await client.post(
                "/api/session-groups/move",
                headers=headers,
                json={"revision": current["revision"], "group_id": middle, "session_id": root},
            )
            assert moved.status_code == 200
            deleted = await client.request("DELETE", f"/api/sessions/{root}", headers=headers, json={})
            assert deleted.status_code == 200
            removed = await client.post(
                "/api/session-groups/remove",
                headers=headers,
                json={"revision": moved.json()["revision"], "group_id": middle},
            )
            assert removed.status_code == 200
            assert next(group for group in removed.json()["groups"] if group["id"] == child)["parent_id"] == parent
            restored = await client.post(
                f"/api/trash/{root}/restore",
                headers=headers,
                json={"deletion_id": deleted.json()["trash"]["deletion_id"]},
            )
            assert restored.status_code == 200
            current = (await client.get("/api/session-groups", headers=headers)).json()
            assert current["memberships"] == {root: parent}
            removed_root = await client.post(
                "/api/session-groups/remove",
                headers=headers,
                json={"revision": current["revision"], "group_id": parent},
            )
            assert removed_root.status_code == 200
            assert removed_root.json()["memberships"] == {}
            assert removed_root.json()["groups"][0]["parent_id"] == ""
            assert (await client.get(f"/api/sessions/{root}", headers=headers)).json()["history"][0][
                "content"
            ] == "Keep nested conversation"

    asyncio.run(scenario())


def test_removal_name_collision_rolls_back_tree_and_revision(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (_, client, headers, _):
            parent = await create_folder(client, headers, "Parent")
            await create_folder(client, headers, "Notes")
            await create_folder(client, headers, "Notes", parent)
            current = (await client.get("/api/session-groups", headers=headers)).json()
            response = await client.post(
                "/api/session-groups/remove",
                headers=headers,
                json={"revision": current["revision"], "group_id": parent},
            )
            assert response.status_code == 409
            assert (await client.get("/api/session-groups", headers=headers)).json() == current

    asyncio.run(scenario())


def test_legacy_flat_folders_migrate_without_losing_membership_or_collapse(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            root = state.selected_session_id
            await state.history.add_message(root, Message(role="user", content="Keep legacy conversation"))
            folder = await create_folder(client, headers, "Legacy")
            current = (await client.get("/api/session-groups", headers=headers)).json()
            moved = await client.post(
                "/api/session-groups/move",
                headers=headers,
                json={"revision": current["revision"], "group_id": folder, "session_id": root},
            )
            assert moved.status_code == 200
            path = state.channels.store.db_path
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("ALTER TABLE ngn_web_session_groups RENAME TO migration_source")
            db.execute(
                "CREATE TABLE ngn_web_session_groups (id TEXT PRIMARY KEY, name TEXT NOT NULL, name_key TEXT NOT NULL UNIQUE, collapsed INTEGER NOT NULL DEFAULT 0)"
            )
            db.execute("INSERT INTO ngn_web_session_groups SELECT id, name, name_key, 1 FROM migration_source")
            db.execute("DROP TABLE migration_source")
        async with client_app(tmp_path) as (_, client, headers, _):
            current = (await client.get("/api/session-groups", headers=headers)).json()
            assert current["groups"] == [{"id": folder, "name": "Legacy", "collapsed": True, "parent_id": ""}]
            assert current["memberships"] == {root: folder}
            nested = await create_folder(client, headers, "Legacy", folder)
            assert nested != folder
            current = (await client.get("/api/session-groups", headers=headers)).json()
            removed = await client.post(
                "/api/session-groups/remove",
                headers=headers,
                json={"revision": current["revision"], "group_id": folder},
            )
            assert removed.status_code == 200
            assert removed.json()["groups"] == [{"id": nested, "name": "Legacy", "collapsed": False, "parent_id": ""}]

    asyncio.run(scenario())


def test_folder_membership_survives_restart_and_trash_restore_but_not_permanent_deletion(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            root = state.selected_session_id
            await state.history.add_message(root, Message(role="user", content="Keep this conversation"))
            initial = (await client.get("/api/session-groups", headers=headers)).json()
            created = await client.post(
                "/api/session-groups/create",
                headers=headers,
                json={"revision": initial["revision"], "name": "Research"},
            )
            assert created.status_code == 200
            folder = created.json()["groups"][0]
            updated = await client.post(
                "/api/session-groups/update",
                headers=headers,
                json={
                    "revision": created.json()["revision"],
                    "group_id": folder["id"],
                    "name": "Project Alpha",
                    "collapsed": True,
                },
            )
            assert updated.status_code == 200
            moved = await client.post(
                "/api/session-groups/move",
                headers=headers,
                json={"revision": updated.json()["revision"], "group_id": folder["id"], "session_id": root},
            )
            assert moved.status_code == 200 and moved.json()["memberships"] == {root: folder["id"]}
            before = moved.json()

        async with client_app(tmp_path) as (_, client, headers, _):
            assert (await client.get("/api/session-groups", headers=headers)).json() == before
            deleted = await client.request("DELETE", f"/api/sessions/{root}", headers=headers, json={})
            assert deleted.status_code == 200
            trashed = (await client.get("/api/session-groups", headers=headers)).json()
            assert trashed["groups"] == before["groups"] and root not in trashed["memberships"]
            restored = await client.post(
                f"/api/trash/{root}/restore",
                headers=headers,
                json={"deletion_id": deleted.json()["trash"]["deletion_id"]},
            )
            assert restored.status_code == 200
            assert (await client.get("/api/session-groups", headers=headers)).json() == before
            permanent = await client.request(
                "DELETE", f"/api/sessions/{root}", headers=headers, json={"permanent": True}
            )
            assert permanent.status_code == 200
            assert (await client.get("/api/session-groups", headers=headers)).json()["memberships"] == {}
            assert (await client.get("/api/session-groups", headers=headers)).json()["groups"] == before["groups"]

    asyncio.run(scenario())


def test_folder_edits_during_stream_do_not_change_execution_or_selection(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            root = state.selected_session_id
            initial = (await client.get("/api/session-groups", headers=headers)).json()
            stream = LiveStream(app, headers, root, "wait")
            await stream.event("text_chunk")
            active = state.active
            assert active is not None
            try:
                created = (
                    await client.post(
                        "/api/session-groups/create",
                        headers=headers,
                        json={"revision": initial["revision"], "name": "Working"},
                    )
                ).json()
                moved = await client.post(
                    "/api/session-groups/move",
                    headers=headers,
                    json={"revision": created["revision"], "group_id": created["groups"][0]["id"], "session_id": root},
                )
                assert moved.status_code == 200
                assert state.active is active and not active.task.done() and not active.task.cancelling()
                assert state.selected_session_id == root
                assert state.harness.session_id == root
            finally:
                await client.post("/api/cancel", headers=headers, json={"run_id": active.id})
                await stream.task

    asyncio.run(scenario())


def test_explicitly_organized_draft_is_preserved_while_unused_new_draft_is_reused(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            root = state.selected_session_id
            initial = (await client.get("/api/session-groups", headers=headers)).json()
            created = (
                await client.post(
                    "/api/session-groups/create",
                    headers=headers,
                    json={"revision": initial["revision"], "name": "Planned work"},
                )
            ).json()
            moved = await client.post(
                "/api/session-groups/move",
                headers=headers,
                json={"revision": created["revision"], "group_id": created["groups"][0]["id"], "session_id": root},
            )
            assert moved.status_code == 200
            fresh = (await client.post("/api/sessions/new", headers=headers, json={})).json()
            assert fresh["session_id"] != root
            assert root in {item["id"] for item in fresh["sessions"]}
            repeated = (await client.post("/api/sessions/new", headers=headers, json={})).json()
            assert repeated["session_id"] == fresh["session_id"]
            assert len(repeated["sessions"]) == 2

    asyncio.run(scenario())


def test_folder_creation_is_revision_guarded_and_failed_edits_roll_back(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (_, client, headers, _):
            initial = (await client.get("/api/session-groups", headers=headers)).json()
            responses = await asyncio.gather(
                *(
                    client.post(
                        "/api/session-groups/create",
                        headers=headers,
                        json={"revision": initial["revision"], "name": name},
                    )
                    for name in ("Project", "Other")
                )
            )
            assert sorted(response.status_code for response in responses) == [200, 409]
            current = (await client.get("/api/session-groups", headers=headers)).json()
            assert len(current["groups"]) == 1
            duplicate = await client.post(
                "/api/session-groups/create",
                headers=headers,
                json={"revision": current["revision"], "name": current["groups"][0]["name"].upper()},
            )
            assert duplicate.status_code == 409
            assert (await client.get("/api/session-groups", headers=headers)).json() == current

    asyncio.run(scenario())


@pytest.mark.parametrize("name", ["   ", "bad\nname", "bad\0name", "x" * 81])
def test_folder_names_are_bounded_and_safe_for_navigation(tmp_path: Path, name: str) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (_, client, headers, _):
            current = (await client.get("/api/session-groups", headers=headers)).json()
            response = await client.post(
                "/api/session-groups/create", headers=headers, json={"revision": current["revision"], "name": name}
            )
            assert response.status_code == 422
            assert (await client.get("/api/session-groups", headers=headers)).json() == current

    asyncio.run(scenario())


def test_folder_removal_never_deletes_chats_and_children_cannot_be_grouped(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            root = state.selected_session_id
            await state.history.add_message(root, Message(role="user", content="Keep me"))
            child = "ngn-child-only"
            await state.history.get_or_create_session(child, "harness")
            initial = (await client.get("/api/session-groups", headers=headers)).json()
            made = (
                await client.post(
                    "/api/session-groups/create",
                    headers=headers,
                    json={"revision": initial["revision"], "name": "Folder"},
                )
            ).json()
            group_id = made["groups"][0]["id"]
            for session_id in (child, "ngn-foreign-root"):
                denied = await client.post(
                    "/api/session-groups/move",
                    headers=headers,
                    json={"revision": made["revision"], "group_id": group_id, "session_id": session_id},
                )
                assert denied.status_code == 404
            moved = (
                await client.post(
                    "/api/session-groups/move",
                    headers=headers,
                    json={"revision": made["revision"], "group_id": group_id, "session_id": root},
                )
            ).json()
            removed = await client.post(
                "/api/session-groups/remove",
                headers=headers,
                json={"revision": moved["revision"], "group_id": group_id},
            )
            assert removed.status_code == 200 and removed.json()["memberships"] == {} and removed.json()["groups"] == []
            snapshot = (await client.get(f"/api/sessions/{root}", headers=headers)).json()
            assert snapshot["history"][0]["content"] == "Keep me"
            assert (await client.get("/api/session-groups")).status_code == 403
            assert (
                await client.post(
                    "/api/session-groups/create",
                    headers=headers,
                    json={
                        "revision": removed.json()["revision"],
                        "name": "extra",
                        "configuration": {},
                    },
                )
            ).status_code == 422

    asyncio.run(scenario())
