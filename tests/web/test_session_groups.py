"""Chat folders survive history lifecycle without owning or interrupting runs."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from typing import cast

import pytest

from nagents.types import Message
from tests.support.web import LiveStream
from tests.support.web import client_app

if TYPE_CHECKING:
    from pathlib import Path

    from nagents.web.service import WebState


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
