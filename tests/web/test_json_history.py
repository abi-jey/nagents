"""A plain JSON tool-name reply remains readable through web history routes."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from nagents.types import Message
from tests.support.web import client_app

if TYPE_CHECKING:
    from pathlib import Path


def test_json_array_reply_does_not_break_chat_snapshot_or_resume(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path) as (app, client, headers, _):
            state = app.state.web
            root = state.selected_session_id
            content = '["mcp__github__get_file_contents", "mcp__github__list_branches"]'
            await state.harness.agent.session.add_message(root, Message(role="user", content="List MCP tools."))
            await state.harness.agent.session.add_message(root, Message(role="assistant", content=content))
            for path in ("/api/sessions", f"/api/sessions/{root}"):
                response = await client.get(path, headers=headers)
                assert response.status_code == 200
                assert response.json()["history"][-1]["content"] == content
            response = await client.post("/api/sessions/resume", headers=headers, json={"session_id": root})
            assert response.status_code == 200
            assert response.json()["history"][-1]["content"] == content

    asyncio.run(scenario())
