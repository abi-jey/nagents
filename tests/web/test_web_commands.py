"""Explicit browser commands stay separate from ordinary model input."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING
from typing import cast

import pytest

from nagents.extensions import CompactionRequest
from nagents.extensions import CompactionResult
from nagents.types import Message
from tests.support.channels import site

if TYPE_CHECKING:
    from pathlib import Path

    from nagents.web.service import Run
    from tests.support.channels import Site

pytestmark = pytest.mark.requires_posix


class LocalCompaction:
    async def should_compact(self, request: CompactionRequest) -> bool:
        return False

    async def compact(self, request: CompactionRequest) -> CompactionResult:
        assert request.force
        return CompactionResult([Message(role="compaction_summary", content="Summary")], "Summary")


def roles(app: Site, session: str) -> list[object]:
    return [row["role"] for row in cast("list[dict[str, object]]", app.history(session)["history"])]


def test_explicit_compact_runs_against_submitted_session_and_retries_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with site(tmp_path, monkeypatch) as app:
        app.state.harness.agent.compaction_strategy = LocalCompaction()
        app.submit("main history")
        app.idle()
        selected = app.client.post("/api/sessions/new", headers=app.headers, json={}).json()["session_id"]
        app.submit("other history", selected)
        app.idle()
        body = {"session_id": app.main, "message_id": str(uuid.uuid4()), "prompt": "/compact", "command": "compact"}
        assert app.client.post("/api/messages", headers=app.headers, json=body).status_code == 200
        app.idle()
        assert roles(app, app.main) == ["compaction_summary"]
        assert roles(app, selected) == ["user", "assistant"]
        assert app.state.selected_session_id == app.state.harness.session_id == selected
        assert app.client.post("/api/messages", headers=app.headers, json=body).status_code == 200
        app.idle()
        assert roles(app, app.main) == ["compaction_summary"]
        assert app.client.portal is not None

        async def status() -> str:
            return await app.state.channels.store._transaction(
                lambda db: str(
                    db.execute(
                        "SELECT status FROM ngn_web_inbox WHERE message_id = ?", (body["message_id"],)
                    ).fetchone()[0]
                )
            )

        assert app.client.portal.call(status) == "completed"


@pytest.mark.parametrize("first_command", ["", "compact"])
def test_command_is_part_of_message_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first_command: str
) -> None:
    with site(tmp_path, monkeypatch) as app:
        app.state.harness.agent.compaction_strategy = LocalCompaction()
        app.submit("history")
        app.idle()
        body = {"session_id": app.main, "message_id": str(uuid.uuid4()), "prompt": "/compact", "command": first_command}
        assert app.client.post("/api/messages", headers=app.headers, json=body).status_code == 200
        app.idle()
        before = roles(app, app.main)
        if not first_command:
            assert before == ["user", "assistant", "user", "assistant"]
        body["command"] = "" if first_command else "compact"
        assert app.client.post("/api/messages", headers=app.headers, json=body).status_code == 409
        app.idle()
        assert roles(app, app.main) == before


@pytest.mark.parametrize(
    "extra",
    [
        {"prompt": "/compact now"},
        {"prompt": "arbitrary instructions"},
        {"prompt": " /compact"},
        {"attachments": ["attachment"]},
        {"command": "login"},
    ],
)
def test_compact_rejects_arguments_attachments_and_unknown_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: dict[str, object]
) -> None:
    with site(tmp_path, monkeypatch) as app:
        body = {
            "session_id": app.main,
            "message_id": str(uuid.uuid4()),
            "prompt": "/compact",
            "command": "compact",
            **extra,
        }
        assert app.client.post("/api/messages", headers=app.headers, json=body).status_code == 422
        app.idle()
        assert roles(app, app.main) == []


def test_compact_failure_preserves_history_and_reports_safe_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FailingCompaction(LocalCompaction):
        async def compact(self, request: CompactionRequest) -> CompactionResult:
            raise RuntimeError("provider-private-token")

    with site(tmp_path, monkeypatch) as app:
        app.state.harness.agent.compaction_strategy = FailingCompaction()
        app.submit("history")
        app.idle()
        records: list[dict[str, object]] = []
        original = app.state.send

        async def observe(run: Run, record: dict[str, object]) -> None:
            records.append(record)
            await original(run, record)

        monkeypatch.setattr(app.state, "send", observe)
        body = {
            "session_id": app.main,
            "message_id": str(uuid.uuid4()),
            "prompt": "/compact",
            "command": "compact",
        }
        assert app.client.post("/api/messages", headers=app.headers, json=body).status_code == 200
        app.idle()
        assert roles(app, app.main) == ["user", "assistant"]
        assert any(record.get("event") == "error" for record in records)
        assert "provider-private-token" not in str(records)
        app.submit("continue after error")
        app.idle()
        assert roles(app, app.main) == ["user", "assistant", "user", "assistant"]
