"""The explicit /compact channel command compacts the chat's bound session."""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import cast

import pytest

from nagents.extensions import CompactionRequest
from nagents.extensions import CompactionResult
from nagents.types import Message
from tests.test_web_channels import site

if TYPE_CHECKING:
    from pathlib import Path

    from pytest import MonkeyPatch

    from tests.test_web_channels import Site

pytestmark = pytest.mark.requires_posix


class LocalCompaction:
    """Force-capable strategy, so the test never calls a provider."""

    async def should_compact(self, request: CompactionRequest) -> bool:
        return False

    async def compact(self, request: CompactionRequest) -> CompactionResult:
        assert request.force is True
        return CompactionResult([Message(role="compaction_summary", content="Summary")], "Summary")


def history_roles(app: Site, session: str) -> list[object]:
    history = cast("list[dict[str, object]]", app.history(session)["history"])
    return [message["role"] for message in history]


def test_compact_command_compacts_the_bound_session(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    with site(tmp_path, monkeypatch) as app:
        app.configure()
        app.state.harness.agent.compaction_strategy = LocalCompaction()
        app.emit("first")
        app.idle()
        session = app.bindings()["chat-a"]
        assert history_roles(app, session) == ["user", "assistant"]
        app.emit("/compact")
        app.idle()
        assert app.channels[0].deliveries[-1].text == "Context compacted: 2 messages summarized into 1."
        assert history_roles(app, session) == ["compaction_summary"]
        assert app.bindings()["chat-a"] == session
        assert app.state.harness.session_id == app.main


def test_compact_command_rejects_arguments_and_keeps_session(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    with site(tmp_path, monkeypatch) as app:
        app.configure()
        app.emit("first")
        app.idle()
        session = app.bindings()["chat-a"]
        app.emit("/compact now")
        app.idle()
        assert "Unknown session command" in app.channels[0].deliveries[-1].text
        assert history_roles(app, session) == ["user", "assistant"]
        assert app.bindings()["chat-a"] == session
