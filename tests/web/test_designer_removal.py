"""Removed Designer entry points cannot activate saved definitions."""

from __future__ import annotations

import asyncio
from importlib.util import find_spec
from typing import TYPE_CHECKING

from nagents.harness.config import AgentProfile
from nagents.harness.config import HarnessConfig
from nagents.types import Message
from tests.support.web import client_app
from tests.support.web import no_guarded_workspace_io as no_guarded_workspace_io

if TYPE_CHECKING:
    import sqlite3
    from pathlib import Path


def test_designer_package_is_not_installed() -> None:
    assert find_spec("nagents.designer") is None


def test_removed_routes_and_saved_definitions_leave_normal_profiles_available(tmp_path: Path) -> None:
    async def check() -> None:
        definitions = tmp_path / ".ngn" / "designs"
        definitions.mkdir(parents=True)
        definition = definitions / "my-team.yaml"
        definition.write_text("Saved user definition, no longer executed.\n", encoding="utf-8")
        configured = HarnessConfig(
            workspace=tmp_path,
            data_dir=tmp_path / "data",
            demo=True,
            profiles={"worker": AgentProfile(model="worker-model")},
        )
        async with client_app(tmp_path, config=configured) as (app, client, headers, _):
            state = app.state.web
            root = state.selected_session_id
            await state.history.add_message(root, Message(role="user", content="Keep this existing conversation."))

            def saved_pin(db: sqlite3.Connection) -> None:
                db.execute("CREATE TABLE ngn_design_sessions (session_id TEXT PRIMARY KEY, agent TEXT, source TEXT)")
                db.execute("INSERT INTO ngn_design_sessions VALUES (?, 'removed-agent', 'saved definition')", (root,))

            await state.channels.store._transaction(saved_pin)
            trace = state.history.db_path.parent / "designer.db"
            trace.write_bytes(b"Existing user trace archive")
            for path in ("/api/designer", "/api/designer/designs/my-team", "/api/designer/channels"):
                response = await client.get(path, headers=headers)
                assert response.status_code == 404, response.text
            for path in ("/api/designer/save", "/api/designer/run", "/api/designer/validate"):
                response = await client.post(path, headers=headers, json={})
                assert response.status_code == 404, response.text

        async with client_app(tmp_path, config=configured) as (app, client, headers, _):
            selected = await client.get(f"/api/sessions/{root}/agent", headers=headers)
            assert selected.status_code == 200, selected.text
            selection = selected.json()
            assert selection["agent"] == "assistant"
            assert "locked_reason" not in selection
            assert {item["name"] for item in selection["profiles"]} == {"assistant", "worker"}
            changed = await client.post(
                f"/api/sessions/{root}/agent",
                headers=headers,
                json={"revision": selection["revision"], "agent": "worker"},
            )
            assert changed.status_code == 200, changed.text
            assert changed.json()["effective"]["model"] == "worker-model"
            saved_source = await app.state.web.channels.store._transaction(
                lambda db: db.execute(
                    "SELECT source FROM ngn_design_sessions WHERE session_id = ?", (root,)
                ).fetchone()[0]
            )
            assert saved_source == "saved definition"
            assert definition.read_text(encoding="utf-8") == "Saved user definition, no longer executed.\n"
            assert trace.read_bytes() == b"Existing user trace archive"

    asyncio.run(check())
