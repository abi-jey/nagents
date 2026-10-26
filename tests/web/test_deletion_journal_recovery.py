"""A failed deletion journal write cannot orphan a child's hidden history."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from typing import cast

import pytest

from nagents.harness.agents.subagents import TaskInfo
from nagents.harness.session_identity import SessionIdentityStore
from nagents.types import Message
from nagents.web.deletion import delete_session
from tests.support.web import client_app
from tests.web.test_web_deletion import quiet
from tests.web.test_web_deletion import rows

if TYPE_CHECKING:
    from pathlib import Path

    from nagents.web.service import WebState


@pytest.mark.parametrize("failed_write", [1, 2])
@pytest.mark.parametrize("permanent", [False, True])
def test_retry_keeps_child_ownership_when_journal_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_write: int, permanent: bool
) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            await quiet(state)
            root = state.selected_session_id
            identity = await SessionIdentityStore(state.history.db_path).reserve(root, "", "Hidden child")
            child = identity.storage_id
            await state.history.get_or_create_session(child, "harness")
            await state.history.add_message(child, Message(role="user", content="Keep ownership through retry"))
            # Cancellation won before the startup event reached the web journal.
            state.harness.tasks._infos[identity.session_id] = TaskInfo(
                identity.session_id, "Hidden child", root_session_id=root, _storage_id=child
            )
            save = state.tasks.store.save
            writes = 0

            async def failing_save(session_id: str, data: dict[str, object], storage_id: str) -> None:
                nonlocal writes
                writes += 1
                if writes == failed_write:
                    raise OSError("Injected journal storage failure")
                await save(session_id, data, storage_id)

            monkeypatch.setattr(state.tasks.store, "save", failing_save)
            with pytest.raises(OSError, match="Injected journal"):
                await delete_session(state, root, permanent=permanent)
            assert not state.mutating
            assert await rows(state, "SELECT id FROM harness_sessions WHERE id = ?", root) == [(root,)]
            assert await state.history.get_history(child)
            assert (identity.session_id in state.harness.tasks._infos) == (failed_write == 1)
            if failed_write == 2:
                assert await rows(state, "SELECT storage_id FROM ngn_web_agent_sessions WHERE root = ?", root) == [
                    (child,)
                ]

            result = await delete_session(state, root, permanent=permanent)
            assert not state.harness.tasks._infos
            if not permanent:
                trash = result["trash"]
                assert isinstance(trash, dict)
                await state.trash.restore(root, str(trash["deletion_id"]))
                listed = await client.get("/api/sessions", headers=headers)
                assert listed.status_code == 200, listed.text
                assert root in {session["id"] for session in listed.json()["sessions"]}
                assert await state.history.get_history(child)
                index = await state.tasks.index(root)
                saved = index["tasks"]
                assert isinstance(saved, list) and len(saved) == 1
                assert saved[0]["status"] == "cancelled" and not saved[0]["can_message"]
                await delete_session(state, root, permanent=True)
            assert not await rows(state, "SELECT id FROM v2_sessions WHERE id = ?", child)
            assert not await rows(state, "SELECT storage_id FROM ngn_web_agent_sessions WHERE root = ?", root)
            assert await state.history.get_history(child) == []

    asyncio.run(scenario())
