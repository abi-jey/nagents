"""The browser rename action uses the target chat and preserves concurrent edits."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest

from nagents.types import Message
from tests.support.titles import title_model
from tests.support.web import client_app
from tests.web.sessions.test_web_deletion import quiet

if TYPE_CHECKING:
    from pathlib import Path

    from nagents.harness import Harness


def test_empty_rename_generates_from_target_users_without_navigating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = app.state.web
            await quiet(state)
            source = state.selected_session_id
            await state.history.add_message(source, Message(role="user", content="Fix tool approvals"))
            await state.history.add_message(source, Message(role="assistant", content="Do not send this agent answer"))
            created = await client.post("/api/sessions/new", headers=headers, json={})
            assert created.status_code == 200
            other = created.json()["session_id"]
            await state.history.add_message(other, Message(role="user", content="Unrelated conversation"))
            state.harness.config.demo = False
            stub = title_model(monkeypatch)
            response = await client.post(f"/api/sessions/{source}/rename", headers=headers, json={"title": ""})
            assert response.status_code == 200, response.text
            assert response.json()["session_id"] == other
            assert next(row for row in response.json()["sessions"] if row["id"] == source)["title"] == stub.reply
            assert len(stub.requests) == 1
            assert json.loads(str(stub.requests[0].messages[1].content)) == {"user_messages": ["Fix tool approvals"]}
            assert stub.requests[0].provider in stub.closed
            assert len(await state.history.get_history(source)) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize("manual_title", ["Keep explicit name", "Existing title"])
def test_generation_does_not_hold_host_mutation_guard_or_overwrite_manual_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, manual_title: str
) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = app.state.web
            await quiet(state)
            root = state.selected_session_id
            initial = await client.post(
                f"/api/sessions/{root}/rename", headers=headers, json={"title": "Existing title"}
            )
            assert initial.status_code == 200
            started, release = asyncio.Event(), asyncio.Event()

            async def generate(harness: Harness, session_id: str) -> str:
                assert session_id == root
                assert not state.mutating
                started.set()
                await release.wait()
                return "Late generated title"

            monkeypatch.setattr("nagents.web.sessions.actions.generate_title", generate)
            pending = asyncio.create_task(client.post(f"/api/sessions/{root}/rename", headers=headers, json={}))
            await asyncio.wait_for(started.wait(), 3)
            changed = await client.post(f"/api/sessions/{root}/rename", headers=headers, json={"title": manual_title})
            assert changed.status_code == 200
            release.set()
            assert (await pending).status_code == 409
            assert next(row for row in await state.harness.list_sessions() if row.id == root).title == manual_title

    asyncio.run(scenario())
