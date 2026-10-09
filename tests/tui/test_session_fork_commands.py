"""TUI fork/rename slash commands use the real persistent Harness operations."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from nagents.harness import Harness
from nagents.harness.config import HarnessConfig
from nagents.tui import NagentsApp
from nagents.tui.widgets import Turn
from nagents.types import Message
from tests.support.tui import idle
from tests.support.tui import send
from tests.support.web import no_guarded_workspace_io as no_guarded_workspace_io

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_tui_fork_and_rename_keep_history_without_sending_commands_to_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        harness = Harness(HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "state", demo=True))
        monkeypatch.setattr(harness, "load_project_instructions", lambda: None)
        await harness.initialize()
        source = harness.session_id
        await harness.agent.session.add_message(source, Message(role="user", content="Saved question"))
        await harness.agent.session.add_message(source, Message(role="assistant", content="Saved answer"))
        app = NagentsApp(harness)
        async with app.run_test(size=(100, 32)) as pilot:
            await idle(app, pilot)
            commands = {command.name: command for command in harness.commands.list()}
            assert commands["fork"].argument_hint == "[title]" and commands["rename"].requires_arguments
            await send(app, pilot, "/fork Separate branch")
            await idle(app, pilot)
            target = harness.session_id
            assert target != source and len(app.query(Turn)) == 2
            assert [message.content for message in await harness.history()] == ["Saved question", "Saved answer"]
            await send(app, pilot, "/rename Chosen name")
            await idle(app, pilot)
            assert next(item for item in await harness.list_sessions() if item.id == target).title == "Chosen name"
            await send(app, pilot, "/rename")
            await idle(app, pilot)
            assert app._status_error and "title" in app._status_text
            assert len(await harness.history()) == 2
            assert [message.content for message in await harness.agent.session.get_history(source)] == [
                "Saved question",
                "Saved answer",
            ]

    asyncio.run(scenario())
