"""Recover child input receipts before the web host starts accepting requests."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import aiosqlite

from tests.support.web import client_app
from tests.support.web import no_guarded_workspace_io as no_guarded_workspace_io

if TYPE_CHECKING:
    from pathlib import Path


def test_restart_recovers_unread_child_input_before_first_snapshot(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, _, _, _):
            state = app.state.web
            root = state.selected_session_id
            await state.tasks.store.validate(root)
            receipt = await state.tasks.store.insert(root, "previous-child", "unread-input", "do not replay")
            assert receipt["status"] == "queued"
            database = state.history.db_path

        async with client_app(tmp_path) as (app, client, headers, _):
            # Bootstrap has returned, but no client has requested task history
            # or subscribed. Recovery must already be durable at this boundary.
            async with (
                aiosqlite.connect(database) as db,
                db.execute(
                    "SELECT status, reason FROM ngn_web_agent_inputs WHERE message_id = ?", ("unread-input",)
                ) as cursor,
            ):
                row = await cursor.fetchone()
            assert row is not None and row[0] == "error" and "restart" in row[1]
            assert not app.state.web.executions.runs
            assert (await client.post("/api/sessions/new", json={}, headers=headers)).status_code == 200

    asyncio.run(scenario())
