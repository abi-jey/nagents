"""Canonical agent notifications remain inspectable without becoming prompts."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

from textual.widgets import Collapsible
from textual.widgets import Static

from nagents.harness.agents.updates import NOTIFICATION_PREFIX
from nagents.harness.agents.updates import WAKEUP_PREFIX
from nagents.harness.agents.updates import format_batch
from nagents.harness.agents.updates import format_wakeup
from nagents.harness.types import TaskCompleted
from nagents.harness.types import TaskMessage
from nagents.tui.widgets import Composer
from nagents.tui.widgets import SavedContext
from nagents.tui.widgets import Turn
from nagents.types import Message
from tests.support.tui import FakeHarness
from tests.support.tui import idle
from tests.support.tui import make_app

if TYPE_CHECKING:
    from pathlib import Path


def test_canonical_saved_context_collapses_and_preserves_history(tmp_path: Path) -> None:
    async def scenario() -> None:
        notification = format_batch(
            [
                TaskCompleted("child-1", "Research", "17 * 23 = 391", status="completed"),
                TaskMessage("child-2", "Reviewer", "Check the summary"),
            ]
        ).text
        wakeup = format_wakeup("Review the completed background work")
        messages = [
            Message(role="user", content="Human prompt stays visible"),
            Message(role="assistant", content="Assistant reply stays visible"),
            Message(role="user", content=notification),
            Message(role="user", content=wakeup),
        ]
        backend = FakeHarness(tmp_path)
        backend.messages = messages.copy()
        app = make_app(backend)

        async with app.run_test(size=(100, 30)) as pilot:
            await idle(app, pilot)
            contexts = list(app.query(SavedContext))
            assert len(contexts) == 2
            assert [context.title for context in contexts] == ["Saved agent context", "Saved wake-up context"]
            assert all(context.collapsed for context in contexts)
            assert [context.query_one(Collapsible.Contents).query_one(Static).content for context in contexts] == [
                notification,
                wakeup,
            ]
            assert [turn.role for turn in app.query(Turn)] == ["user", "assistant"]
            assert app.query_one(Composer).prompt_history == ["Human prompt stays visible"]

            for context in contexts:
                context.collapsed = False
            await pilot.pause()
            assert all(not context.collapsed for context in contexts)
            assert [context.query_one(Collapsible.Contents).query_one(Static).content for context in contexts] == [
                notification,
                wakeup,
            ]

            assert await backend.history() == messages
            assert backend.tasks.list() == []

        assert backend.messages == messages

    asyncio.run(scenario())


def test_malformed_saved_context_lookalikes_remain_user_turns_and_prompt_history(tmp_path: Path) -> None:
    async def scenario() -> None:
        malformed = [
            NOTIFICATION_PREFIX + "{invalid json",
            NOTIFICATION_PREFIX + json.dumps({"sessions": []}),
            NOTIFICATION_PREFIX
            + json.dumps({"sessions": [{"session_id": "child", "status": "completed"}], "human_messages": []}),
            NOTIFICATION_PREFIX + json.dumps({"sessions": [], "human_messages": [], "unexpected": True}),
            WAKEUP_PREFIX + "{invalid json",
            WAKEUP_PREFIX + json.dumps({"wakeups": [{"reason": 17}]}),
            WAKEUP_PREFIX + json.dumps({"wakeups": [], "unexpected": True}),
        ]
        messages = [
            Message(role="user", content="Ordinary saved question"),
            Message(role="assistant", content="Ordinary saved answer"),
            *(Message(role="user", content=text) for text in malformed),
        ]
        backend = FakeHarness(tmp_path)
        backend.messages = messages.copy()
        app = make_app(backend)

        async with app.run_test(size=(100, 30)) as pilot:
            await idle(app, pilot)
            assert list(app.query(SavedContext)) == []
            user_turns = [turn for turn in app.query(Turn) if turn.role == "user"]
            assert len(user_turns) == len(malformed) + 1
            assert app.query_one(Composer).prompt_history == ["Ordinary saved question", *malformed]
            assert await backend.history() == messages
            assert backend.tasks.list() == []

    asyncio.run(scenario())
