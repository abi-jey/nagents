"""Speech captions persist and stream as observations without entering model input."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from typing import cast

import pytest
from aiohttp import web

from nagents.types import Message
from nagents.web.live_runtime import LiveService
from tests.support.web import client_app
from tests.support.web import no_guarded_workspace_io as no_guarded_workspace_io
from tests.test_web_live_login import ANSWER
from tests.test_web_live_login import OFFER
from tests.test_web_live_login import config
from tests.test_web_live_login import upstream

if TYPE_CHECKING:
    from pathlib import Path

    from nagents import Agent
    from nagents.web.service import WebState


def caption(sequence: int, text: str, speaker: str = "user") -> dict[str, object]:
    return {
        "type": "transcript",
        "seq": sequence,
        "text": text,
        "speaker": speaker,
        "start_ms": sequence * 10,
        "end_ms": sequence * 10 + 5,
    }


def caption_events(state: WebState) -> list[dict[str, object]]:
    return [
        record
        for frame, _ in state.bus.ring
        if isinstance(record := frame.get("record"), dict) and record.get("event") == "live_caption"
    ]


def test_captions_merge_at_history_anchors_replay_once_and_survive_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            root = state.selected_session_id
            sink = state.live_captions(root, "voice-call")
            await sink(caption(1, "Hello"))
            user = await state.history.add_message(root, Message(role="user", content="Typed request"))
            await sink(caption(2, "Let me check.", "assistant"))
            assistant = await state.history.add_message(root, Message(role="assistant", content="Full answer"))
            await sink(caption(3, "Spoken answer.", "assistant"))
            await sink(caption(3, "A duplicate callback must not replace text", "assistant"))
            history = (await client.get(f"/api/sessions/{root}", headers=headers)).json()["history"]
            assert [record["content"] for record in history] == [
                "Hello",
                "Typed request",
                "Let me check.",
                "Full answer",
                "Spoken answer.",
            ]
            saved = [record for record in history if record["role"] == "live_caption"]
            assert [record["anchor_history_id"] for record in saved] == ["", str(user), str(assistant)]
            assert all(
                record["source"] == "live_caption" and record["voice_session_id"] == "voice-call" for record in saved
            )
            assert len({record["history_id"] for record in saved}) == 3
            streamed = caption_events(state)
            assert [record["history_id"] for record in streamed] == [record["history_id"] for record in saved]
            assert all(record["session_id"] == root and record["text"] == record["content"] for record in streamed)
            assert [message.content for message in await state.history.get_history(root)] == [
                "Typed request",
                "Full answer",
            ]
        async with client_app(tmp_path) as (app, client, headers, _):
            assert (await client.get(f"/api/sessions/{root}", headers=headers)).json()["history"] == history
            assert not caption_events(cast("WebState", app.state.web))

    asyncio.run(scenario())


def test_caption_call_stays_in_original_root_after_selection_changes(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            original = state.selected_session_id
            sink = state.live_captions(original, "bound-call")
            await sink(caption(1, "First words"))
            selected = (await client.post("/api/sessions/new", headers=headers, json={})).json()["session_id"]
            assert selected != original
            await sink(caption(2, "More words"))
            history = cast("list[dict[str, object]]", (await state.snapshot(original))["history"])
            assert len(history) == 2
            assert (await state.snapshot(selected))["history"] == []
            with pytest.raises(ValueError, match="admitted chat"):
                await state.live_captions(selected, "bound-call")(caption(3, "Wrong chat"))
            assert all(event["session_id"] == original for event in caption_events(state))
            assert (await state.snapshot(selected))["history"] == []

    asyncio.run(scenario())


def test_durable_captions_outlive_poll_window_without_losing_repeated_words(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, _, _, _):
            state = cast("WebState", app.state.web)
            root = state.selected_session_id
            sink = state.live_captions(root, "long-call")
            for sequence in range(1, 271):
                await sink(caption(sequence, "Again"))
            await sink(caption(271, "x" * 5000))
            records = await state.history.snapshot(root)
            assert len(records) == 271 and records[0]["caption_seq"] == 1
            assert [record["content"] for record in records[:270]] == ["Again"] * 270
            assert records[-1]["content"] == "x" * 4096
            assert await state.history.get_history(root) == []

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["clear", "delete"])
def test_voice_only_history_is_removed_by_core_clear_and_delete(tmp_path: Path, operation: str) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, _, _, _):
            state = cast("WebState", app.state.web)
            root = state.selected_session_id
            await state.live_captions(root, "private-call")(caption(1, "Private speech"))
            assert await state.history.get_history(root) == []
            method = state.history.clear_session if operation == "clear" else state.history.delete_session
            await method(root)
            counts = await state.history.store._transaction(
                lambda db: (
                    db.execute("SELECT COUNT(*) FROM ngn_web_live_captions").fetchone()[0],
                    db.execute("SELECT COUNT(*) FROM ngn_web_live_calls WHERE accepting = 1").fetchone()[0],
                )
            )
            assert counts == (0, 0)

    asyncio.run(scenario())


def test_soft_delete_preserves_captions_but_rejects_late_observations_then_purge_removes_them(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            root = state.selected_session_id
            sink = state.live_captions(root, "trashed-call")
            await sink(caption(1, "Saved speech"))
            before = await state.history.snapshot(root)
            deleted = await client.request(
                "DELETE", f"/api/sessions/{root}", headers=headers, json={"permanent": False}
            )
            assert deleted.status_code == 200
            await sink(caption(2, "Late speech after deleting chat"))
            items = (await client.get("/api/trash", headers=headers)).json()["items"]
            generation = next(item["deletion_id"] for item in items if item["id"] == root)
            restored = await client.post(
                f"/api/trash/{root}/restore", headers=headers, json={"deletion_id": generation}
            )
            assert restored.status_code == 200
            assert await state.history.snapshot(root) == before
            purged = await client.request("DELETE", f"/api/sessions/{root}", headers=headers, json={"permanent": True})
            assert purged.status_code == 200
            assert (
                await state.history.store._transaction(
                    lambda db: db.execute("SELECT COUNT(*) FROM ngn_web_live_captions").fetchone()[0]
                )
                == 0
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("spoken", [True, False])
def test_clear_invalidates_the_bound_call_even_before_its_first_caption(tmp_path: Path, spoken: bool) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, _, _, _):
            state = cast("WebState", app.state.web)
            root = state.selected_session_id
            old = await state.bind_live_captions(root, "before-clear")
            if spoken:
                await old(caption(1, "Before clearing"))
            await state.history.clear_session(root)
            count = len(caption_events(state))
            await old(caption(2, "Late words must not resurrect the cleared conversation"))
            assert await state.history.snapshot(root) == [] and len(caption_events(state)) == count
            with pytest.raises(ValueError, match="invalidated"):
                await state.bind_live_captions(root, "before-clear")
            with pytest.raises(ValueError, match="invalidated"):
                async with state.history.voice_request(root, "late-run", "Old speech", voice_session_id="before-clear"):
                    pytest.fail("A cleared call must not repopulate model history through a new delegation")
            new = await state.bind_live_captions(root, "after-clear")
            await new(caption(1, "Fresh call"))
            rows = await state.history.snapshot(root)
            assert len(rows) == 1 and rows[0]["content"] == "Fresh call"
            await state.history.delete_session(root)
            assert (
                await state.history.store._transaction(
                    lambda db: db.execute("SELECT COUNT(*) FROM ngn_web_live_calls").fetchone()[0]
                )
                == 0
            )

    asyncio.run(scenario())


def test_actual_login_supervisor_publishes_and_persists_input_output_captions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async def handle(request: web.Request) -> web.StreamResponse:
            if request.method == "POST":
                return web.Response(status=201, text=ANSWER, headers={"Location": "/calls/rtc_captions"})
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            for kind, text, item, start in (
                ("input_transcript.added", "Hello", "caller", 10),
                ("output_transcript.added", "Hi", "reply", 20),
                ("output_transcript.added", "Hi", "reply", 20),
                ("output_transcript.added", "Hi", "second-reply", 30),
            ):
                await socket.send_json(
                    {"type": kind, "item": {"id": item, "text": text}, "start_ms": start, "end_ms": start + 5}
                )
            async for message in socket:
                if message.json()["type"] == "session.close":
                    await socket.send_json({"type": "session.closed", "reason": "client_request"})
                    break
            return socket

        def no_public_agent(voice: str) -> Agent:
            raise AssertionError("Login calls must not construct a public-API agent")

        async with client_app(tmp_path) as (app, _, _, _), upstream(monkeypatch, handle):
            state = cast("WebState", app.state.web)
            root = state.selected_session_id
            service = LiveService(
                no_public_agent,
                login_factory=lambda voice: config(),
                caption_factory=lambda identifier: state.bind_live_captions(root, identifier),
            )
            try:
                async with asyncio.timeout(5):
                    created = await service.create(OFFER)
                    identifier = str(created["session_id"])
                    while len(caption_events(state)) < 3:
                        await asyncio.sleep(0.005)
                    assert (await service.close(identifier))["status"] == "closed"
                rows = await state.history.snapshot(root)
                assert [row["content"] for row in rows] == ["Hello", "Hi", "Hi"]
                assert all(row["voice_session_id"] == identifier for row in rows)
                assert await state.history.get_history(root) == []
                events = cast("list[dict[str, object]]", (await service.snapshot(identifier))["events"])
                assert len([event for event in events if event["type"] == "transcript"]) == 3
                assert not service.active_session_id
            finally:
                await service.shutdown()

    asyncio.run(scenario())
