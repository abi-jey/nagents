"""Root forks preserve context/media while dropping ingress and execution authority."""

from __future__ import annotations

import asyncio
import base64
from typing import TYPE_CHECKING
from uuid import uuid4

from nagents.channels.delivery_types import DeliveryOrigin
from nagents.channels.types import ChannelFile
from nagents.channels.types import ChannelSend
from nagents.types import ImageContent
from nagents.types import Message
from nagents.types import TextContent
from nagents.types import ToolCall
from nagents.web.local_delivery import SEND
from tests.support.web import client_app
from tests.web.test_uploads import PNG
from tests.web.test_web_deletion import quiet
from tests.web.test_web_deletion import rows
from tests.web.test_web_live_captions import caption

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path


def test_fork_preserves_voice_upload_and_delivery_bytes_independently_of_parent(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = app.state.web
            await quiet(state)
            root = state.selected_session_id
            state.harness.config.demo = False
            upload = str(uuid4())
            source_path = f"/api/sessions/{root}/uploads/{upload}"
            sent = await client.post(
                source_path,
                headers={**headers, "Content-Type": "image/png", "X-Ngn-Filename": "image.png"},
                content=PNG,
            )
            assert sent.status_code == 200
            await state.channels.store.web(
                root, "original-web-message", "Look at this image", (upload,), ("image/png",)
            )
            image_row = await state.history.add_message(
                root,
                Message(
                    role="user",
                    content=[
                        TextContent(text="Look at this image"),
                        ImageContent(base64_data=base64.b64encode(PNG).decode(), media_type="image/png"),
                    ],
                ),
            )

            def finish_upload(db: sqlite3.Connection) -> None:
                inbox = db.execute("SELECT id FROM ngn_web_inbox WHERE message_id = 'original-web-message'").fetchone()[
                    0
                ]
                db.execute("UPDATE ngn_web_inbox SET status = 'completed' WHERE id = ?", (inbox,))
                db.execute("INSERT INTO ngn_web_message_origins VALUES (?, ?)", (image_row, inbox))

            await state.channels.store._transaction(finish_upload)
            anchor = await state.history.add_message(
                root, Message(role="assistant", tool_calls=[ToolCall(id="outbound", name="channel_send", arguments={})])
            )
            original_delivery = await state.history.deliveries.prepare(
                DeliveryOrigin(
                    root, root, "old-run", "old-turn", "", 0, "old-invocation", anchor, 0, "outbound", "channel_send"
                ),
                "builtin.web",
                ChannelSend(root, "Saved image result", files=(ChannelFile("result.png", "image/png", PNG),)),
                SEND,
            ).commit()
            await state.history.add_message(
                root,
                Message(
                    role="tool", tool_call_id="outbound", name="channel_send", content="Delivery already completed"
                ),
            )
            voice_row = await state.history.add_message(
                root, Message(role="user", content="Internal voice handoff model context")
            )
            await state.history.register_live_call(root, "old-voice")
            await state.channels.store._transaction(
                lambda db: (
                    db.execute("INSERT INTO ngn_web_voice_messages VALUES (?, 'My spoken request')", (voice_row,)),
                    db.execute("INSERT INTO ngn_web_voice_origins VALUES (?, 'old-voice')", (voice_row,)),
                )
            )
            await state.history.add_live_caption(root, "old-voice", caption(1, "My spoken request"))
            await state.history.add_live_caption(root, "old-voice", caption(2, "Spoken answer", "assistant"))
            group = (await client.get("/api/session-groups", headers=headers)).json()
            created = (
                await client.post(
                    "/api/session-groups/create",
                    headers=headers,
                    json={"revision": group["revision"], "name": "Research"},
                )
            ).json()
            folder = created["groups"][0]["id"]
            moved = await client.post(
                "/api/session-groups/move",
                headers=headers,
                json={"revision": created["revision"], "session_id": root, "group_id": folder},
            )
            assert moved.status_code == 200
            before_context = await state.history.get_history(root)
            response = await client.post(
                f"/api/sessions/{root}/fork", headers=headers, json={"title": "Different approach"}
            )
            assert response.status_code == 200, response.text
            snapshot = response.json()
            target = snapshot["session_id"]
            assert target != root and state.selected_session_id == target
            assert await state.history.get_history(target) == before_context
            assert next(item for item in snapshot["sessions"] if item["id"] == target)["forked_from"] == root
            assert await rows(state, "SELECT group_id FROM ngn_web_group_members WHERE session_id = ?", target) == [
                (folder,)
            ]
            assert not await rows(state, "SELECT id FROM ngn_web_inbox WHERE session_id = ?", target)
            assert not await rows(state, "SELECT session_id FROM ngn_web_session_owners WHERE session_id = ?", target)
            assert not await rows(
                state,
                "SELECT o.history_id FROM ngn_web_message_origins o JOIN v2_messages m ON m.id=o.history_id WHERE m.session_id = ?",
                target,
            )
            copied = snapshot["history"]
            image = next(row for row in copied if row.get("uploads"))
            copied_upload = image["uploads"][0]["upload_id"]
            assert copied_upload != upload and image["content"] == "Look at this image" and image["parts"] == []
            assert not image["source_verified"] and not image["ingress_id"] and not image["message_id"]
            assert (
                await client.get(f"/api/sessions/{target}/uploads/{copied_upload}/preview", headers=headers)
            ).content == PNG
            assert (
                await client.get(f"/api/sessions/{target}/uploads/{upload}/preview", headers=headers)
            ).status_code == 404
            captions = [row for row in copied if row["role"] == "live_caption"]
            assert [row["content"] for row in captions] == ["My spoken request", "Spoken answer"]
            copied_voice = captions[0]["voice_session_id"]
            assert copied_voice != "old-voice" and all(row["voice_session_id"] == copied_voice for row in captions)
            assert not await state.history.add_live_caption(target, copied_voice, caption(3, "A stale callback"))
            assert next(row for row in copied if row.get("voice_verified"))["content"] == "My spoken request"
            receipt = (await state.history.deliveries.history(target)).current[0]
            assert receipt.delivery_id != original_delivery.delivery_id and not receipt.origin.host_run_id
            assert receipt.origin.root_session_id == receipt.origin.actor_session_id == target
            assert (
                await state.history.deliveries.read_asset(target, receipt.delivery_id, receipt.assets[0].asset_id)
                == PNG
            )
            renamed = await client.post(
                f"/api/sessions/{root}/rename", headers=headers, json={"title": "Original renamed"}
            )
            assert renamed.status_code == 200 and renamed.json()["session_id"] == target
            assert (
                next(item for item in renamed.json()["sessions"] if item["id"] == root)["title"] == "Original renamed"
            )
            trashed = await client.request("DELETE", f"/api/sessions/{root}", headers=headers, json={})
            assert trashed.status_code == 200, trashed.text
            assert next(item for item in trashed.json()["sessions"] if item["id"] == target)["forked_from"] == root
            restored = await client.post(
                f"/api/trash/{root}/restore",
                headers=headers,
                json={"deletion_id": trashed.json()["trash"]["deletion_id"]},
            )
            assert restored.status_code == 200
            removed = await client.request("DELETE", f"/api/sessions/{root}", headers=headers, json={"permanent": True})
            assert removed.status_code == 200
            assert next(item for item in removed.json()["sessions"] if item["id"] == target)["forked_from"] == ""
            assert await state.history.get_history(target) == before_context
            assert (
                await client.get(f"/api/sessions/{target}/uploads/{copied_upload}/preview", headers=headers)
            ).content == PNG
            assert (
                await state.history.deliveries.read_asset(target, receipt.delivery_id, receipt.assets[0].asset_id)
                == PNG
            )
            await state.history.clear_session(target)
            assert not await rows(state, "SELECT upload_id FROM ngn_fork_message_uploads")
            assert not await rows(state, "SELECT history_id FROM ngn_fork_message_display")

    asyncio.run(scenario())


def test_fork_and_rename_reject_invalid_roots_and_keep_explicit_empty_sessions(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = app.state.web
            await quiet(state)
            root = state.selected_session_id
            for title in ("", " ", "x" * 81, "line\nbreak"):
                assert (
                    await client.post(f"/api/sessions/{root}/rename", headers=headers, json={"title": title})
                ).status_code == 422
            assert (await client.post(f"/api/sessions/{root}/fork", json={})).status_code == 403
            hidden = "ngn-hidden-child"
            await state.history.get_or_create_session(hidden, "harness")
            for action in ("fork", "rename"):
                assert (
                    await client.post(
                        f"/api/sessions/{hidden}/{action}", headers=headers, json={"title": "No authority"}
                    )
                ).status_code == 404
            fork = await client.post(f"/api/sessions/{root}/fork", headers=headers, json={})
            assert fork.status_code == 200
            child = fork.json()["session_id"]
            assert (
                await client.post(
                    f"/api/sessions/{root}/rename", headers=headers, json={"title": "Explicit blank parent"}
                )
            ).status_code == 200
            listing = (await client.get("/api/sessions", headers=headers)).json()
            assert {root, child}.issubset({item["id"] for item in listing["sessions"]})
            await state.channels.store.web(root, "queued-parent", "Pending work")
            rejected = await client.post(f"/api/sessions/{root}/fork", headers=headers, json={})
            assert rejected.status_code == 409 and state.selected_session_id == child
            assert len(await state.harness.list_sessions()) == 2

    asyncio.run(scenario())
