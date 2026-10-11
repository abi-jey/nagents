"""Independent context copies and persistent explicit titles, without model work."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING

import pytest

from nagents.events import TextDoneEvent
from nagents.harness import Harness
from nagents.harness.config import HarnessConfig
from nagents.session.forks import SessionForkError
from nagents.session.forks import fork_in
from nagents.session.forks import transaction
from nagents.types import ImageContent
from nagents.types import Message
from nagents.types import TextContent
from nagents.types import ToolCall
from tests.support.web import no_guarded_workspace_io as no_guarded_workspace_io

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event


def test_fork_copies_raw_context_remaps_boundary_and_remains_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        harness = Harness(HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "state", demo=True))
        monkeypatch.setattr(harness, "load_project_instructions", lambda: None)
        try:
            await harness.initialize()
            source = harness.session_id
            await harness.rename_session("Original conversation")
            await harness.agent.session.add_message(source, Message(role="user", content="Older history stays stored"))
            context = [
                Message(role="user", content="Earlier work was already completed."),
                Message(
                    role="assistant",
                    tool_calls=[ToolCall(id="old-call", name="read_file", arguments={"path": "note.txt"})],
                ),
                Message(role="tool", tool_call_id="old-call", name="read_file", content="Already read"),
                Message(
                    role="user",
                    content=[
                        TextContent(text="Image context"),
                        ImageContent(base64_data="aW1hZ2U=", media_type="image/png"),
                    ],
                ),
                Message(role="assistant", content="Retained final answer"),
            ]
            await harness.agent.session.replace_context(source, context)
            source_context = await harness.history()
            target = await harness.fork_session("Alternative direction")
            assert target != source and harness.session_id == target
            assert await harness.history() == source_context
            sessions = {item.id: item for item in await harness.list_sessions()}
            assert sessions[target].forked_from == source and sessions[source].forked_from == ""
            assert sessions[target].title == "Alternative direction"
            with closing(sqlite3.connect(harness.agent.session.db_path)) as db:
                old = db.execute("SELECT id FROM v2_messages WHERE session_id = ? ORDER BY id", (source,)).fetchall()
                new = db.execute("SELECT id FROM v2_messages WHERE session_id = ? ORDER BY id", (target,)).fetchall()
                assert len(old) == len(new) == 6 and not set(old).intersection(new)
                assert (
                    db.execute("SELECT compacted_at_message_id FROM v2_sessions WHERE id = ?", (target,)).fetchone()
                    == new[1]
                )
            await harness.rename_session("An explicit name")

            async def answer(*args: object, **kwargs: object) -> AsyncIterator[Event]:
                yield TextDoneEvent(text="An independent reply")

            monkeypatch.setattr(harness.agent.provider, "generate", answer)
            assert [event async for event in harness.run("Hello from the independent branch")]
            assert (await harness.list_sessions())[0].title == "An explicit name"
            assert await harness.agent.session.get_history(source) == source_context
            await harness.resume(source)
            assert await harness.history() == source_context
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_failed_copy_rolls_back_root_metadata_and_selection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        harness = Harness(HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "state", demo=True))
        monkeypatch.setattr(harness, "load_project_instructions", lambda: None)
        try:
            await harness.initialize()
            source = harness.session_id
            await harness.agent.session.add_message(source, Message(role="user", content="keep"))
            with closing(sqlite3.connect(harness.agent.session.db_path)) as db, db:
                db.execute(
                    "CREATE TRIGGER fail_copy BEFORE INSERT ON v2_messages WHEN NEW.session_id != '"
                    + source
                    + "' BEGIN SELECT RAISE(ABORT, 'copy failed'); END"
                )
            with pytest.raises(sqlite3.IntegrityError, match="copy failed"):
                await harness.fork_session()
            assert harness.session_id == source
            assert len(await harness.list_sessions()) == 1
            with closing(sqlite3.connect(harness.agent.session.db_path)) as db:
                assert db.execute("SELECT * FROM ngn_session_forks").fetchall() == []
                assert db.execute("SELECT * FROM ngn_session_fork_counters").fetchall() == []
            with harness.operation("held"), pytest.raises(RuntimeError, match="busy"):
                await harness.fork_session()
            for invalid in ("", "  ", "line\nbreak", "x" * 81):
                with pytest.raises(ValueError, match="title"):
                    await harness.rename_session(invalid)

            async def schedule(task: str, delay: float, reason: str) -> dict[str, str]:
                raise AssertionError("Fork must never activate a host scheduler")

            harness.wakeup_handler = schedule
            with pytest.raises(SessionForkError, match="host's fork"):
                await harness.fork_session()
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_fork_numbers_are_atomic_and_per_source_even_with_custom_titles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        harness = Harness(HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "state", demo=True))
        monkeypatch.setattr(harness, "load_project_instructions", lambda: None)
        try:
            await harness.initialize()
            source = harness.session_id
            await harness.rename_session("Design")
            custom = await harness.fork_session("Alternative")
            nested = await harness.fork_session()
            assert (
                next(item for item in await harness.list_sessions() if item.id == nested).title
                == "Alternative · fork 1"
            )
            path = harness.agent.session.db_path
            forks = await asyncio.gather(*(transaction(path, lambda db: fork_in(db, source)) for _ in range(4)))
            sessions = {item.id: item for item in await harness.list_sessions()}
            assert {sessions[item].title for item in forks} == {f"Design · fork {number}" for number in range(2, 6)}
            assert all(sessions[item].forked_from == source for item in forks)
            await harness.resume(source)
            await harness.rename_session("Renamed design")
            following = await harness.fork_session("   ")
            assert (
                next(item for item in await harness.list_sessions() if item.id == following).title
                == "Renamed design · fork 6"
            )
            assert sessions[custom].title == "Alternative"
        finally:
            await harness.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("name", ["x" * 80, "🌳" * 80])
def test_automatic_fork_title_keeps_number_when_source_name_is_long(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    async def scenario() -> None:
        harness = Harness(HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "state", demo=True))
        monkeypatch.setattr(harness, "load_project_instructions", lambda: None)
        try:
            await harness.initialize()
            await harness.rename_session(name)
            target = await harness.fork_session()
            title = next(item for item in await harness.list_sessions() if item.id == target).title
            assert title.endswith(" · fork 1") and len(title) == 80
        finally:
            await harness.close()

    asyncio.run(scenario())
