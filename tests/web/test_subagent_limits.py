"""Approval-backed scoped subagent limits and shared usage in the web host."""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING

import pytest

from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.harness.subagent_settings import SubagentLimits
from tests.support.hang_guard import HANG_GUARD
from tests.support.web import LiveStream
from tests.web.test_web_tool_approvals import decide
from tests.web.test_web_wakeups import scheduled_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from collections.abc import Callable
    from pathlib import Path

    from nagents.events import Event
    from nagents.types import Message
    from nagents.web.service import Run
    from tests.support.providers import FakeProvider


pytestmark = pytest.mark.usefixtures("workspace_discovery_only")


def proposal_script(
    revision: str, limits: SubagentLimits, *, delegate_first: bool = False
) -> Callable[[FakeProvider, list[Message]], AsyncIterator[Event]]:
    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        if provider.index:
            yield TextDoneEvent(text="Child result")
        elif delegate_first and len(provider.requests) == 1:
            yield ToolCallEvent(id="child", name="to_agent", arguments={"prompt": "Count one execution"})
        elif len(provider.requests) == (2 if delegate_first else 1):
            yield ToolCallEvent(id="read-limits", name="subagent_configuration", arguments={"scope": "workspace"})
        elif len(provider.requests) == (3 if delegate_first else 2):
            if delegate_first:
                assert any(message.role == "tool" and message.name == "to_agent" for message in messages)
            yield ToolCallEvent(
                id="configure-limits",
                name="subagent_configure",
                arguments={"revision": revision, "scope": "workspace", **limits.values()},
            )
        elif delegate_first and len(provider.requests) == 4:
            yield ToolCallEvent(
                id="read-updated-limits", name="subagent_configuration", arguments={"scope": "workspace"}
            )
        else:
            yield TextDoneEvent(text="Settings proposal handled")

    return script


def test_web_subagent_settings_persist_scoped_cas_and_preserve_other_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, _messages: list[Message]) -> AsyncIterator[Event]:
            yield TextDoneEvent(text="idle")

        async with scheduled_app(tmp_path, monkeypatch, script) as (_, client, headers, state, _, _):
            before = (await client.get("/api/settings", headers=headers)).json()
            updated_values = {**before["values"], "model": "preserved-model", "max_tool_rounds": 77}
            binding = before["model_binding"]
            response = await client.post(
                "/api/settings",
                headers=headers,
                json={
                    "revision": before["revision"],
                    "values": updated_values,
                    "model_binding": {"provider": binding["provider"], "id": "preserved-model"},
                },
            )
            assert response.status_code == 200, response.text

            global_before = await state.subagent_settings.read("global")
            global_limits = SubagentLimits(4, 20, 3)
            global_saved = await state.subagent_settings.save("global", global_before.revision, global_limits)
            assert global_saved.limits == global_limits
            assert global_saved.effective == global_limits

            workspace_before = await state.subagent_settings.read("workspace")
            workspace_limits = SubagentLimits(6, 40, 5)
            workspace_saved = await state.subagent_settings.save(
                "workspace", workspace_before.revision, workspace_limits
            )
            assert workspace_saved.limits == workspace_limits
            assert workspace_saved.effective == workspace_limits
            assert (await state.subagent_settings.read("global")).limits == global_limits

            with pytest.raises(ValueError, match="changed"):
                await state.subagent_settings.save("workspace", workspace_before.revision, SubagentLimits(7, 50, 6))

            workspace_ui = (await client.get("/api/settings", headers=headers)).json()
            global_ui = (await client.get("/api/settings/global", headers=headers)).json()
            assert workspace_ui["values"]["max_concurrent_subagents"] == 6
            assert workspace_ui["values"]["max_subagent_executions"] == 40
            assert workspace_ui["values"]["max_subagent_depth"] == 5
            assert workspace_ui["values"]["model"] == "preserved-model"
            assert workspace_ui["values"]["max_tool_rounds"] == 77
            assert global_ui["values"]["max_concurrent_subagents"] == 4
            assert global_ui["values"]["max_subagent_executions"] == 20
            assert global_ui["values"]["max_subagent_depth"] == 3

            await state.settings.load()
            reloaded = state.settings.values.model_dump(mode="json")
            assert reloaded["model"] == "preserved-model"
            assert reloaded["max_tool_rounds"] == 77
            assert reloaded["max_concurrent_subagents"] == 6
            assert reloaded["max_subagent_executions"] == 40
            assert reloaded["max_subagent_depth"] == 5

    asyncio.run(scenario())


def test_mandatory_limit_approval_ignores_saved_allow_tool_grant_and_deny_keeps_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        limits = SubagentLimits(5, 24, 3)

        async def idle_script(provider: FakeProvider, _messages: list[Message]) -> AsyncIterator[Event]:
            yield TextDoneEvent(text="idle")

        async with scheduled_app(tmp_path, monkeypatch, idle_script) as (
            app,
            client,
            headers,
            state,
            providers,
            _,
        ):
            before = await state.subagent_settings.read("workspace")
            binding = state.tool_approvals.binding(state.harness, "subagent_configure")
            assert binding is not None and binding.persistent
            state.tool_approvals.grant(binding)
            assert state.tool_approvals.allowed(binding)

            async def live_script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
                if len(provider.requests) == 1:
                    yield ToolCallEvent(id="read", name="subagent_configuration", arguments={"scope": "workspace"})
                elif len(provider.requests) == 2:
                    assert any(
                        message.role == "tool" and message.name == "subagent_configuration" for message in messages
                    )
                    yield ToolCallEvent(
                        id="configure",
                        name="subagent_configure",
                        arguments={"revision": before.revision, "scope": "workspace", **limits.values()},
                    )
                else:
                    yield TextDoneEvent(text="Denied as requested")

            providers[0].script = live_script
            run = LiveStream(app, headers, state.harness.session_id, "Raise this run's scoped limits")
            pending = await run.event("approval")
            assert pending["tool"] == "subagent_configure" and pending["mandatory"] is True
            assert pending["allow_tool"] is False and pending["allow_tool_persistent"] is False
            assert (await decide(client, headers, pending, "allow_tool")).status_code == 409
            assert (await decide(client, headers, pending, "deny")).status_code == 200
            await run.event("run_finished")
            await asyncio.wait_for(run.task, HANG_GUARD)
            assert (await state.subagent_settings.read("workspace")).limits == before.limits
            assert state.tool_approvals.allowed(binding), (
                "The existing grant remains saved but cannot bypass mandatory approval"
            )

    asyncio.run(scenario())


def test_cancelled_limit_approval_does_not_save(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async def idle_script(provider: FakeProvider, _messages: list[Message]) -> AsyncIterator[Event]:
            yield TextDoneEvent(text="idle")

        async with scheduled_app(tmp_path, monkeypatch, idle_script) as (app, client, headers, state, providers, _):
            before = await state.subagent_settings.read("workspace")
            providers[0].script = proposal_script(before.revision, SubagentLimits(5, 24, 3))
            stream = LiveStream(app, headers, state.harness.session_id, "Raise this run's scoped limits")
            pending = await stream.event("approval")
            response = await client.post("/api/cancel", headers=headers, json={"run_id": pending["run_id"]})
            assert response.status_code == 200
            finished = await stream.event("run_finished")
            assert finished["status"] == "cancelled"
            await asyncio.wait_for(stream.task, HANG_GUARD)
            assert (await state.subagent_settings.read("workspace")).limits == before.limits

    asyncio.run(scenario())


@pytest.mark.parametrize("reorder_read_result", [False, True], ids=["normal", "approval-before-read-result"])
def test_stale_approved_limit_proposal_does_not_overwrite_newer_scope_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reorder_read_result: bool
) -> None:
    async def scenario() -> None:
        proposed = SubagentLimits(5, 24, 3)

        async def idle_script(provider: FakeProvider, _messages: list[Message]) -> AsyncIterator[Event]:
            yield TextDoneEvent(text="idle")

        async with scheduled_app(tmp_path, monkeypatch, idle_script) as (
            app,
            client,
            headers,
            state,
            providers,
            _,
        ):
            approval_published = asyncio.Event()
            if reorder_read_result:
                original_send = state.send

                async def send(run: Run, record: dict[str, object]) -> None:
                    # Harness events are queued; approval callbacks publish
                    # directly and may overtake an earlier tool result.
                    if record.get("event") == "tool_result" and record.get("id") == "read-limits":
                        await approval_published.wait()
                    await original_send(run, record)
                    if record.get("event") == "approval":
                        approval_published.set()

                monkeypatch.setattr(state, "send", send)
            before = await state.subagent_settings.read("workspace")
            providers[0].script = proposal_script(before.revision, proposed)
            stream = LiveStream(app, headers, state.harness.session_id, "Raise this run's scoped limits")
            pending = await stream.event("approval")
            intervening = SubagentLimits(6, 32, 4)
            await state.subagent_settings.save("workspace", before.revision, intervening)
            assert (await decide(client, headers, pending, "allow")).status_code == 200
            saw_read_result = False
            while True:
                result = await stream.event("tool_result")
                if result.get("id") == "configure-limits":
                    assert result.get("name") == "subagent_configure"
                    break
                assert result.get("id") == "read-limits"
                assert result.get("name") == "subagent_configuration"
                assert not result.get("error")
                saw_read_result = True
            if reorder_read_result:
                assert saw_read_result
            assert result.get("error") or result.get("is_error")
            await stream.event("run_finished")
            await asyncio.wait_for(stream.task, HANG_GUARD)
            assert (await state.subagent_settings.read("workspace")).limits == intervening
            values = (await client.get("/api/settings", headers=headers)).json()["values"]
            assert values["max_concurrent_subagents"] == 6
            assert values["max_subagent_executions"] == 32
            assert values["max_subagent_depth"] == 4

    asyncio.run(scenario())


def test_approved_limits_are_read_on_next_model_turn_without_resetting_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        proposed = SubagentLimits(5, 24, 3)

        async def idle_script(provider: FakeProvider, _messages: list[Message]) -> AsyncIterator[Event]:
            yield TextDoneEvent(text="idle")

        async with scheduled_app(tmp_path, monkeypatch, idle_script) as (
            app,
            client,
            headers,
            state,
            providers,
            _,
        ):
            before = await state.subagent_settings.read("workspace")

            async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
                if provider.index:
                    yield TextDoneEvent(text="Completed one charged child")
                elif len(provider.requests) == 1:
                    yield ToolCallEvent(id="child", name="to_agent", arguments={"prompt": "Count one execution"})
                elif len(provider.requests) == 2:
                    yield ToolCallEvent(
                        id="read-initial", name="subagent_configuration", arguments={"scope": "workspace"}
                    )
                elif len(provider.requests) == 3:
                    assert any(
                        message.role == "tool" and message.name == "subagent_configuration" for message in messages
                    )
                    yield ToolCallEvent(
                        id="configure",
                        name="subagent_configure",
                        arguments={"revision": before.revision, "scope": "workspace", **proposed.values()},
                    )
                elif len(provider.requests) == 4:
                    yield ToolCallEvent(
                        id="read-next-turn", name="subagent_configuration", arguments={"scope": "workspace"}
                    )
                else:
                    config_results = [
                        str(message.content)
                        for message in messages
                        if message.role == "tool" and message.name == "subagent_configuration"
                    ]
                    assert len(config_results) >= 2
                    current = config_results[-1]
                    assert re.search(r"max_concurrent_subagents['\"]?\s*:\s*5", current)
                    assert re.search(r"max_subagent_executions['\"]?\s*:\s*24", current)
                    assert re.search(r"used_executions['\"]?\s*:\s*1", current)
                    assert re.search(r"remaining_executions['\"]?\s*:\s*23", current)
                    yield TextDoneEvent(text="Configured limits are in effect.")

            providers[0].script = script
            stream = LiveStream(app, headers, state.harness.session_id, "Raise limits and verify the next turn")
            pending = await stream.event("approval")
            assert pending["tool"] == "subagent_configure"
            assert (await decide(client, headers, pending, "allow")).status_code == 200
            await stream.event("run_finished")
            await asyncio.wait_for(stream.task, HANG_GUARD)

            assert state.harness.tasks._used == 1, "The approved settings reload retains the already-used execution"
            assert {"to_agent", "subagent_configuration", "subagent_configure"} <= set(providers[0].schemas[-1])
            limits = await state.subagent_settings.read("workspace")
            assert limits.limits == proposed
            values = (await client.get("/api/settings", headers=headers)).json()["values"]
            assert values["max_concurrent_subagents"] == 5
            assert values["max_subagent_executions"] == 24

            task_index = (await client.get(f"/api/sessions/{state.harness.session_id}/tasks", headers=headers)).json()
            usage = task_index["subagent_usage"]
            assert usage["used_executions"] == 1
            assert usage["limits"]["max_subagent_executions"] == 24
            assert usage["remaining_executions"] == 23

    asyncio.run(scenario())
