"""Voice startup receives bounded saved text without executing previous requests."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING
from typing import cast

import pytest
from fastapi import HTTPException

from nagents.types import ImageContent
from nagents.types import Message
from nagents.types import TextContent
from nagents.web.live_context import MAX_SEED_BYTES
from nagents.web.live_context import MAX_SOURCE_BYTES
from nagents.web.live_context import MAX_SOURCE_MESSAGES
from nagents.web.live_context import LiveSeed
from nagents.web.live_context import read_seed
from nagents.web.live_context import with_summary
from nagents.web.live_runtime import LiveService
from tests.support.web import client_app
from tests.support.web import no_guarded_workspace_io as no_guarded_workspace_io

if TYPE_CHECKING:
    from pathlib import Path

    from nagents import Agent


def texts(seed: LiveSeed) -> str:
    return json.dumps(seed.history, ensure_ascii=False)


def caption(sequence: int, text: str, speaker: str = "user") -> dict[str, object]:
    return {
        "type": "transcript",
        "seq": sequence,
        "speaker": speaker,
        "text": text,
        "start_ms": sequence * 10,
        "end_ms": sequence * 10 + 5,
    }


def test_seed_reads_only_selected_root_and_excludes_privileged_and_nontext_content(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, _, _, _):
            state = app.state.web
            root, session = state.selected_session_id, state.harness.agent.session
            for message in (
                Message(role="system", content="Private system instructions"),
                Message(role="developer", content="Private developer instructions"),
                Message(role="user", content="Discuss the release schedule"),
                Message(role="assistant", content="The release is planned for Friday"),
                Message(role="tool", content="Large tool output must remain in the main assistant"),
                Message(
                    role="user",
                    content=[
                        TextContent(text="What about this picture?"),
                        ImageContent(base64_data="private-image", media_type="image/png"),
                    ],
                ),
            ):
                await session.add_message(root, message)
            await session.add_message("ngn-other", Message(role="user", content="Another root's private request"))
            before = await session.get_history(root)
            seed = await read_seed(session.db_path, root, "recent")
            payload = texts(seed)
            assert "Discuss the release schedule" in payload and "planned for Friday" in payload
            assert "What about this picture?" in payload
            assert all(
                value not in payload + seed.summary_source
                for value in (
                    "Private system",
                    "Private developer",
                    "Large tool output",
                    "private-image",
                    "Another root",
                )
            )
            assert all(item["role"] in {"user", "assistant"} for item in seed.history)
            assert cast("int", seed.report()["bytes"]) <= MAX_SEED_BYTES and len(seed.history) <= 14
            assert seed.omitted_content and seed.source_messages == 3
            assert "Friday" not in str(seed.report()) and "summary_source" not in seed.report()
            assert state.selected_session_id == root and await session.get_history(root) == before
            assert state.active is None
            with pytest.raises(HTTPException):
                await read_seed(session.db_path, "ngn-missing", "recent")

    asyncio.run(scenario())


def test_seed_reuses_existing_compaction_and_clean_voice_projection_not_internal_prompt(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, _, _, _):
            state = app.state.web
            root, session = state.selected_session_id, state.harness.agent.session
            await session.add_message(root, Message(role="user", content="Old raw message before compaction"))
            summary_id = await session.add_message(
                root, Message(role="compaction_summary", content="We chose a Friday release.")
            )
            await session.set_compaction_boundary(root, summary_id)
            voice_id = await session.add_message(
                root, Message(role="user", content="Internal voice prompt wrapper containing prior fragments")
            )
            await state.history.store._transaction(
                lambda db: db.execute(
                    "INSERT INTO ngn_web_voice_messages VALUES (?, ?)", (voice_id, "Check the release plan")
                )
            )
            seed = await read_seed(session.db_path, root, "recent", task_state="The assistant is already working.")
            assert "We chose a Friday release" in texts(seed)
            assert "Check the release plan" in texts(seed)
            assert "Internal voice prompt" not in texts(seed) + seed.summary_source
            assert "Old raw message" not in texts(seed) + seed.summary_source
            assert seed.summary_included and seed.method == "recent"
            assert seed.source_messages == 1
            prepared = with_summary(seed, "The release plan is being checked.")
            assert prepared.method == "generated_summary" and prepared.summary_included
            assert "assistant is already working" in texts(prepared)
            assert prepared.fingerprint == seed.fingerprint
            assert cast("int", prepared.report()["bytes"]) <= MAX_SEED_BYTES

    asyncio.run(scenario())


def test_seed_bounds_unicode_and_source_size_prioritizes_newest_and_invalidates_fingerprint(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, _, _, _):
            state = app.state.web
            root, session = state.selected_session_id, state.harness.agent.session
            await session.add_message(root, Message(role="compaction_summary", content="Summary " + "🎤" * 5000))
            for index in range(40):
                await session.add_message(root, Message(role="user", content=f"Request {index}: " + "漢🎤" * 4000))
            seed = await read_seed(session.db_path, root, "summary")
            assert "Request 39" in texts(seed) and "Request 0:" not in texts(seed)
            assert cast("int", seed.report()["bytes"]) <= MAX_SEED_BYTES and len(seed.history) <= 14
            assert len(seed.summary_source.encode("utf-8")) <= MAX_SOURCE_BYTES
            source = json.loads(seed.summary_source)
            assert source["messages"] and source["partial"]
            assert source["omissions"]["source_byte_budget"]
            assert seed.omitted_messages > 0 and seed.omitted_content
            assert "Remaining text omitted" in texts(seed)
            same = await read_seed(session.db_path, root, "summary")
            assert seed.fingerprint == same.fingerprint
            await session.add_message(root, Message(role="assistant", content="Newest verified result"))
            changed = await read_seed(session.db_path, root, "summary")
            assert changed.fingerprint != seed.fingerprint
            prepared = with_summary(changed, "🎤" * 3000)
            assert cast("int", prepared.report()["bytes"]) <= MAX_SEED_BYTES and len(prepared.history) <= 6
            assert "Newest verified result" in texts(prepared) and prepared.omitted_content

    asyncio.run(scenario())


def test_none_mode_omits_context_and_arbitrary_json_text_cannot_break_selection(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, _, _, _):
            state = app.state.web
            root, session = state.selected_session_id, state.harness.agent.session
            for text in (
                '["plain user array", 1, null]',
                '{"encrypted_content":"private-native-reasoning"}',
                '[{"type":"reasoning","text":"Private reasoning"}]',
                "Normal caller text",
            ):
                await session.add_message(root, Message(role="user", content=text))
            seed = await read_seed(session.db_path, root, "recent")
            assert "Normal caller text" in texts(seed)
            assert "plain user array" in texts(seed), "ordinary JSON text is not a typed multimedia message"
            assert "private-native-reasoning" in texts(seed), "a user's literal JSON field is still user text"
            assert "Private reasoning" in texts(seed), (
                "unrecognized objects are literal user data, not native reasoning"
            )
            none = await read_seed(session.db_path, root, "none")
            assert none.history == () and none.summary_source == ""
            assert none.report()["bytes"] == 0 and none.method == "none"
            assert "Normal caller text" not in str(none.report())

    asyncio.run(scenario())


def test_saved_voice_only_context_preserves_calls_scope_and_caption_cache_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = app.state.web
            root, session = state.selected_session_id, state.harness.agent.session
            first = state.live_captions(root, "old-call")
            await first(caption(1, "Book "))
            await first(caption(2, "Friday."))
            await first(caption(3, "Which restaurant?", "assistant"))
            before = await read_seed(session.db_path, root, "recent")
            assert "Book Friday." in texts(before) and "Which restaurant?" in texts(before)
            assert before.source_messages == 2, "adjacent partial fragments form one historical utterance"
            assert await session.get_history(root) == [], "captions never become canonical model messages"
            second = state.live_captions(root, "later-call")
            await second(caption(1, "Actually Saturday."))
            after = await read_seed(session.db_path, root, "recent")
            assert "Actually Saturday." in texts(after) and after.fingerprint != before.fingerprint
            source = json.loads(after.summary_source)["messages"]
            assert [item["voice_session_id"] for item in source] == ["old-call", "old-call", "later-call"]
            monkeypatch.setattr("nagents.web.live_context.MAX_SOURCE_CAPTIONS", 2)
            partial = json.loads((await read_seed(session.db_path, root, "summary")).summary_source)
            assert partial["partial"] and partial["omissions"]["older_caption_fragments"] == 2
            selected = (await client.post("/api/sessions/new", headers=headers, json={})).json()["session_id"]
            await state.live_captions(selected, "other-root-call")(caption(1, "Other root private speech"))
            old = await read_seed(session.db_path, root, "recent")
            assert "Other root private speech" not in texts(old) + old.summary_source
            new = await read_seed(session.db_path, selected, "recent")
            assert "Book Friday" not in texts(new) + new.summary_source
            assert state.selected_session_id == selected

    asyncio.run(scenario())


def test_caption_compaction_boundary_and_owned_voice_request_dedup_do_not_hide_typed_text(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, _, _, _):
            state = app.state.web
            root, session = state.selected_session_id, state.harness.agent.session
            await state.live_captions(root, "before-compaction")(caption(1, "Earlier private voice details"))
            summary = await session.add_message(
                root, Message(role="compaction_summary", content="Previously chose Friday.")
            )
            await session.set_compaction_boundary(root, summary)
            await state.live_captions(root, "after-compaction")(caption(1, "Check Saturday"))
            request = await session.add_message(
                root, Message(role="user", content="Internal duplicate request wrapper")
            )
            await state.history.store._transaction(
                lambda db: db.execute("INSERT INTO ngn_web_voice_origins VALUES (?, ?)", (request, "after-compaction"))
            )
            await session.add_message(root, Message(role="user", content="Check Saturday"))
            await session.add_message(root, Message(role="assistant", content="Saturday confirmed."))
            seed = await read_seed(session.db_path, root, "recent")
            assert "Earlier private voice details" not in texts(seed) + seed.summary_source
            assert "Internal duplicate request wrapper" not in texts(seed) + seed.summary_source
            assert texts(seed).count("Check Saturday") == 2, "typed text remains even if it resembles a caption"
            assert "Saturday confirmed" in texts(seed) and "Previously chose Friday" in texts(seed)
            assert seed.source_messages == 3
            assert json.loads(seed.summary_source)["omissions"]["compacted_history"] is True

    asyncio.run(scenario())


def test_summary_source_explicitly_marks_message_window_omissions_and_unclipped_text(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, _, _, _):
            state = app.state.web
            root, session = state.selected_session_id, state.harness.agent.session
            await session.add_message(root, Message(role="user", content="A complete short text request"))
            complete = await read_seed(session.db_path, root, "summary")
            source = json.loads(complete.summary_source)
            assert source["partial"] is False
            assert source["omissions"] == {
                "compacted_history": False,
                "older_text_messages": 0,
                "older_caption_fragments": 0,
                "source_byte_budget": False,
                "content_excerpts": False,
            }
            assert "Tool output" in source["scope"], "partial=False only describes eligible visible text"
            for index in range(MAX_SOURCE_MESSAGES + 3):
                await session.add_message(root, Message(role="user", content=f"Later message {index}"))
            limited = await read_seed(session.db_path, root, "summary")
            source = json.loads(limited.summary_source)
            assert source["partial"] is True
            assert source["omissions"]["older_text_messages"] == 4
            assert source["omissions"]["source_byte_budget"] is False
            assert source["omissions"]["compacted_history"] is False
            assert len(source["messages"]) == MAX_SOURCE_MESSAGES
            assert len(limited.summary_source.encode("utf-8")) <= MAX_SOURCE_BYTES
            assert complete.fingerprint != limited.fingerprint

    asyncio.run(scenario())


def test_ending_during_context_preparation_never_creates_a_late_provider_session() -> None:
    async def scenario() -> None:
        preparing, release = asyncio.Event(), asyncio.Event()

        def agent(voice: str) -> Agent:
            raise AssertionError("Cancelled startup must not create a provider")

        async def context(identifier: str) -> LiveSeed:
            preparing.set()
            await release.wait()
            return LiveSeed()

        service = LiveService(agent, context_factory=context)
        opening = asyncio.create_task(service.create_stream())
        await preparing.wait()
        closing = asyncio.create_task(service.close(service.active_session_id))
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(HTTPException, match="stopped"):
            await opening
        result = await closing
        assert result["status"] == "closed" and service.active_session_id == ""
        await service.shutdown()

    asyncio.run(scenario())
