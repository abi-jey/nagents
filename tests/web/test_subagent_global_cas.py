"""Cross-process revision checks for inherited web subagent limits."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING
from typing import Literal

import pytest

from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.harness.agents.subagent_settings import SubagentLimits
from tests.support.hang_guard import HANG_GUARD
from tests.support.web import LiveStream
from tests.web.execution.test_web_wakeups import scheduled_app
from tests.web.test_web_tool_approvals import decide

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.types import Message
    from tests.support.providers import FakeProvider


pytestmark = pytest.mark.usefixtures("workspace_discovery_only")


def _external_global_write(path: Path, limits: SubagentLimits, revision: str) -> None:
    """Emulate another web process updating the shared defaults database."""
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS ngn_web_global_settings ("
            "id INTEGER PRIMARY KEY CHECK(id = 1), version INTEGER NOT NULL, "
            "values_json TEXT NOT NULL, revision TEXT NOT NULL)"
        )
        row = db.execute("SELECT values_json FROM ngn_web_global_settings WHERE id = 1").fetchone()
        values = json.loads(row[0]) if row else {}
        values.update(limits.values())
        db.execute(
            "INSERT INTO ngn_web_global_settings (id, version, values_json, revision) VALUES (1, 1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET values_json=excluded.values_json, revision=excluded.revision",
            (json.dumps(values, separators=(",", ":")), revision),
        )


def test_external_global_revision_invalidates_workspace_proposal_and_refreshes_inheritance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async def idle_script(provider: FakeProvider, _messages: list[Message]) -> AsyncIterator[Event]:
            from nagents.events import TextDoneEvent

            yield TextDoneEvent(text="idle")

        async with scheduled_app(tmp_path, monkeypatch, idle_script) as (_, _, _, state, _, _):
            previous_workspace = await state.subagent_settings.read("workspace")
            previous_overrides = dict(state.settings.workspace_overrides)
            external_limits = SubagentLimits(4, 32, 3)
            external_revision = "a" * 64
            _external_global_write(state.settings.global_path, external_limits, external_revision)

            # A proposal based on the old inherited global revision must not
            # materialize stale defaults as a workspace override.
            try:
                await state.subagent_settings.save("workspace", previous_workspace.revision, SubagentLimits(6, 40, 5))
            except ValueError as error:
                assert "changed" in str(error).lower()
            else:
                raise AssertionError("workspace save accepted a stale global revision")
            assert state.settings.workspace_overrides == previous_overrides

            # The next read observes the other process's committed global
            # values and revision; a newly based workspace proposal can save.
            fresh_workspace = await state.subagent_settings.read("workspace")
            assert fresh_workspace.revision != previous_workspace.revision
            assert fresh_workspace.effective == external_limits
            assert fresh_workspace.limits == external_limits
            assert state.settings.global_revision == external_revision

            # Model already-admitted work in this root run; refreshing shared
            # defaults must not start a new budget window.
            state.harness.tasks._used = 2
            used_before = state.harness.tasks._used
            inherited = await state.subagent_settings.read("global")
            await state.harness.tasks.refresh_limits()
            assert inherited.limits == external_limits
            assert state.harness.tasks._used == used_before

            # Override only the execution budget. The saved workspace cache
            # must keep inheriting the other two fields from global defaults.
            workspace_limits = SubagentLimits(4, 15, 3)
            saved = await state.subagent_settings.save("workspace", fresh_workspace.revision, workspace_limits)
            assert saved.limits == workspace_limits
            assert saved.effective == workspace_limits
            assert {
                name
                for name in ("max_concurrent_subagents", "max_subagent_executions", "max_subagent_depth")
                if name in state.settings.workspace_overrides
            } == {"max_subagent_executions"}
            assert state.settings.workspace_overrides["max_subagent_executions"] == 15
            refreshed_workspace = await state.subagent_settings.read("workspace")
            assert refreshed_workspace.effective == SubagentLimits(4, 15, 3)
            global_after = await state.subagent_settings.read("global")
            assert global_after.limits == external_limits
            assert {
                name
                for name in ("max_concurrent_subagents", "max_subagent_executions", "max_subagent_depth")
                if name in state.settings.workspace_overrides
            } == {"max_subagent_executions"}
            assert state.settings.workspace_overrides["max_subagent_executions"] == 15

    asyncio.run(scenario())


def test_idle_root_usage_remains_visible_without_relabeling_another_root_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async def idle_script(provider: FakeProvider, _messages: list[Message]) -> AsyncIterator[Event]:
            yield TextDoneEvent(text="idle")

        async with scheduled_app(tmp_path, monkeypatch, idle_script) as (app, client, headers, state, providers, _):
            initial = await state.subagent_settings.read("workspace")
            limits = SubagentLimits(3, 15, 2)

            async def proposal(provider: FakeProvider, _messages: list[Message]) -> AsyncIterator[Event]:
                if len(provider.requests) == 1:
                    yield ToolCallEvent(
                        id="read-limits", name="subagent_configuration", arguments={"scope": "workspace"}
                    )
                elif len(provider.requests) == 2:
                    yield ToolCallEvent(
                        id="configure-limits",
                        name="subagent_configure",
                        arguments={"revision": initial.revision, "scope": "workspace", **limits.values()},
                    )
                else:
                    yield TextDoneEvent(text="Limits updated")

            providers[0].script = proposal
            stream = LiveStream(app, headers, state.harness.session_id, "Update subagent limits")
            pending = await stream.event("approval")
            assert pending["tool"] == "subagent_configure"
            assert (await decide(client, headers, pending, "allow")).status_code == 200
            await stream.event("run_finished")
            await asyncio.wait_for(stream.task, HANG_GUARD)

            root = state.harness.session_id
            state.harness.tasks._used = 0
            state.harness.tasks._session_id = root
            response = await client.get(f"/api/sessions/{root}/tasks", headers=headers)
            assert response.status_code == 200
            usage = response.json()["subagent_usage"]
            assert usage["used_executions"] == 0
            assert usage["limits"]["max_subagent_executions"] == 15

            # A later, different root's retained counter must not be reported
            # as usage for this session when its owner has already been released.
            state.harness.tasks._used = 9
            state.harness.tasks._session_id = "ngn-different-root"
            response = await client.get(f"/api/sessions/{root}/tasks", headers=headers)
            assert response.status_code == 200
            assert "subagent_usage" not in response.json()

    asyncio.run(scenario())


@pytest.mark.parametrize("scope", ["global", "workspace"])
def test_unrelated_settings_edit_preserves_omitted_approved_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: Literal["global", "workspace"]
) -> None:
    async def scenario() -> None:
        async def idle_script(provider: FakeProvider, _messages: list[Message]) -> AsyncIterator[Event]:
            yield TextDoneEvent(text="idle")

        async with scheduled_app(tmp_path, monkeypatch, idle_script) as (_, client, headers, state, _, _):
            configured = SubagentLimits(5, 25, 4)
            initial = await state.subagent_settings.read(scope)
            await state.subagent_settings.save(scope, initial.revision, configured)

            path = "/api/settings/global" if scope == "global" else "/api/settings"
            before = (await client.get(path, headers=headers)).json()
            values = dict(before["values"])
            for name in ("max_concurrent_subagents", "max_subagent_executions"):
                values.pop(name)
            values["max_tool_rounds"] += 1
            response = await client.post(
                path,
                headers=headers,
                json={
                    "revision": before["revision"],
                    "values": values,
                    "model_binding": before["model_binding"],
                },
            )
            assert response.status_code == 200, response.text
            after = (await client.get(path, headers=headers)).json()
            assert (
                SubagentLimits(
                    after["values"]["max_concurrent_subagents"],
                    after["values"]["max_subagent_executions"],
                    after["values"]["max_subagent_depth"],
                )
                == configured
            )

    asyncio.run(scenario())
