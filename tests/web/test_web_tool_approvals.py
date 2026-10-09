"""Saved operator decisions retain exact workspace/tool scope and execution checks."""

from __future__ import annotations

import asyncio
import copy
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.extensions import AgentPlugin
from nagents.harness import Harness
from nagents.harness.config import HarnessConfig
from nagents.types import ToolCall
from nagents.web.tool_approvals import ToolApprovals
from tests.harness.test_harness import ScriptedProvider
from tests.support.config import connection
from tests.support.hang_guard import HANG_GUARD
from tests.support.web import LiveStream
from tests.support.web import client_app
from tests.support.web import no_guarded_workspace_io as no_guarded_workspace_io

if TYPE_CHECKING:
    from pathlib import Path

    import httpx
    from fastapi import FastAPI

    from nagents.extensions import RunContext


async def example(value: str = "") -> str:
    """A harmless extension used to exercise real approved tool execution."""
    return "executed:" + value


async def replacement(value: str = "") -> str:
    """A different implementation must never inherit example's saved grant."""
    return "replacement:" + value


def config_for(workspace: Path, *, data_dir: Path | None = None) -> HarnessConfig:
    return HarnessConfig(workspace, data_dir=data_dir or workspace / "data", providers=connection(auth="api-key"))


async def decide(
    client: httpx.AsyncClient, headers: dict[str, str], event: dict[str, object], decision: str
) -> httpx.Response:
    return await client.post(
        "/api/approval",
        headers=headers,
        json={
            "run_id": event["run_id"],
            "approval_id": event["approval_id"],
            "call_id": event["id"],
            "decision": decision,
        },
    )


def real_stream(app: FastAPI, headers: dict[str, str], harness: Harness, names: list[str]) -> LiveStream:
    harness.agent.provider = ScriptedProvider(
        [
            [
                ToolCallEvent(id=f"call-{index}", name=name, arguments={"value": str(index)})
                for index, name in enumerate(names)
            ],
            [TextDoneEvent(text="done")],
        ]
    )
    # client_app's portable harness only replaces discovery and scripted runs.
    # Execute the genuine Harness/Agent/ToolExecutor path for these assertions.
    harness.run = Harness.run.__get__(harness)  # type: ignore[method-assign]
    return LiveStream(app, headers, harness.session_id, "Use the requested test tools")


async def finish(stream: LiveStream) -> None:
    assert (await stream.event("run_finished"))["status"] == "completed"
    await asyncio.wait_for(stream.task, HANG_GUARD)


def test_allow_once_reasks_and_deny_never_saves_permission(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path, config=config_for(tmp_path)) as (app, client, headers, harnesses):
            harness = harnesses[0]
            harness.agent.register_tool(example)
            stream = real_stream(app, headers, harness, ["example", "example"])
            first = await stream.event("approval")
            assert first["allow_tool"] is True and first["allow_tool_persistent"] is True
            assert (await decide(client, headers, first, "allow")).status_code == 200
            second = await stream.event("approval")
            assert second["approval_id"] != first["approval_id"]
            assert (await decide(client, headers, second, "deny")).status_code == 200
            result = await stream.event("tool_result")
            if result["id"] == "call-0":
                assert result["result"] == "executed:0"
                result = await stream.event("tool_result")
            assert result["id"] == "call-1" and "Approval denied" in str(result["error"])
            await finish(stream)
            assert (await client.get("/api/tools", headers=headers)).json()["tool_approvals"] == []

    asyncio.run(scenario())


def test_allow_tool_persists_only_exact_tool_and_can_be_revoked(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path, config=config_for(tmp_path)) as (app, client, headers, harnesses):
            harness = harnesses[0]
            harness.agent.register_tool(example)
            harness.agent.register_tool(example, name="other")
            stream = real_stream(app, headers, harness, ["example", "example", "other"])
            first = await stream.event("approval")
            # Every component of the request binding is checked before storage.
            for key in ("run_id", "approval_id", "id"):
                assert (await decide(client, headers, {**first, key: "wrong"}, "allow_tool")).status_code == 409
            assert (await client.get("/api/tools", headers=headers)).json()["tool_approvals"] == []
            assert (await decide(client, headers, first, "allow_tool")).status_code == 200
            closed = await stream.event("approval_closed")
            assert closed["decision"] == "allow_tool"
            notice = await stream.event("notice")
            assert notice["policy"] == "workspace_tool_allow" and notice["call_id"] == "call-1"
            other = await stream.event("approval")
            assert other["tool"] == "other"
            assert (await decide(client, headers, first, "allow_tool")).status_code == 409
            assert (await decide(client, headers, other, "deny")).status_code == 200
            await finish(stream)
            grants = (await client.get("/api/tools", headers=headers)).json()["tool_approvals"]
            assert [grant["tool"] for grant in grants] == ["example"]
            assert grants[0]["persistent"] is True

        # A new web process/harness in the same workspace inherits the rule.
        async with client_app(tmp_path, config=config_for(tmp_path)) as (app, client, headers, harnesses):
            harness = harnesses[0]
            harness.agent.register_tool(example)
            stream = real_stream(app, headers, harness, ["example"])
            assert (await stream.event("notice"))["policy"] == "workspace_tool_allow"
            assert (await stream.event("tool_result"))["result"] == "executed:0"
            await finish(stream)
            revoked = await client.post(
                "/api/tools/approvals/revoke",
                headers=headers,
                json={"tool": "example", "revision": grants[0]["revision"]},
            )
            assert revoked.status_code == 200 and revoked.json()["tool_approvals"] == []
            assert (
                await client.post(
                    "/api/tools/approvals/revoke",
                    headers=headers,
                    json={"tool": "example", "revision": grants[0]["revision"]},
                )
            ).status_code == 409
            stream = real_stream(app, headers, harness, ["example"])
            pending = await stream.event("approval")
            assert (await decide(client, headers, pending, "deny")).status_code == 200
            await finish(stream)

    asyncio.run(scenario())


def test_changed_definition_or_permissions_cannot_receive_pending_grant(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path, config=config_for(tmp_path)) as (app, client, headers, harnesses):
            harness = harnesses[0]
            harness.agent.register_tool(example)
            stream = real_stream(app, headers, harness, ["example"])
            pending = await stream.event("approval")
            harness.agent.register_tool(replacement, name="example")
            assert (await decide(client, headers, pending, "allow_tool")).status_code == 409
            assert (await client.get("/api/tools", headers=headers)).json()["tool_approvals"] == []
            assert (await decide(client, headers, pending, "deny")).status_code == 200
            await finish(stream)
            harness.agent.register_tool(example)
            stream = real_stream(app, headers, harness, ["example"])
            pending = await stream.event("approval")
            harness.config.read_only = True
            assert (await decide(client, headers, pending, "allow_tool")).status_code == 409
            assert (await decide(client, headers, pending, "allow")).status_code == 200
            result = await stream.event("tool_result")
            assert "no longer allows" in str(result["error"])
            await finish(stream)
            assert (await client.get("/api/tools", headers=headers)).json()["tool_approvals"] == []

    asyncio.run(scenario())


def test_saved_grant_never_overrides_reviewer_disabled_tools_or_other_workspace(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path, config=config_for(tmp_path)) as (app, client, headers, harnesses):
            harness = harnesses[0]
            harness.agent.register_tool(example)
            stream = real_stream(app, headers, harness, ["example"])
            pending = await stream.event("approval")
            assert (await decide(client, headers, pending, "allow_tool")).status_code == 200
            await finish(stream)
            harness.config.read_only = True
            result = await harness.agent.tool_executor.execute(ToolCall(id="reviewer", name="example", arguments={}))
            assert result.error and "Read-only" in result.error
            harness.config.read_only = False
            harness.tool_settings.save({harness.config.agent: {"example": False}}, "")
            result = await harness.agent.tool_executor.execute(ToolCall(id="disabled", name="example", arguments={}))
            assert result.error and "disabled" in result.error
            other_workspace = tmp_path / "other-workspace"
            other_workspace.mkdir()
            # Even explicitly sharing the grants DB does not share workspace authority.
            store = ToolApprovals(app.state.web.tool_approvals.path, other_workspace)
            binding = store.binding(harness, "example")
            assert binding is not None and not store.allowed(binding)
            assert store.snapshot() == []

    asyncio.run(scenario())


def test_dynamic_registration_does_not_inherit_grant_after_restart_or_replacement(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path, config=config_for(tmp_path)) as (app, client, headers, harnesses):
            harness = harnesses[0]
            destination = "one"

            async def dynamic(value: str = "") -> str:
                return destination + value

            harness.agent.register_tool(dynamic, name="example")
            stream = real_stream(app, headers, harness, ["example", "example"])
            pending = await stream.event("approval")
            assert pending["allow_tool"] is True and pending["allow_tool_persistent"] is False
            assert (await decide(client, headers, pending, "allow_tool")).status_code == 200
            await finish(stream)
            original = app.state.web.tool_approvals
            binding = original.binding(harness, "example")
            assert binding is not None and original.allowed(binding)
            restarted = ToolApprovals(original.path, harness.workspace)
            fresh = restarted.binding(harness, "example")
            assert fresh is not None and not restarted.allowed(fresh)
            harness.agent.register_tool(dynamic, name="example")
            fresh = original.binding(harness, "example")
            assert fresh is not None and not original.allowed(fresh)

    asyncio.run(scenario())


def test_builtin_permission_survives_restart_but_not_same_name_plugin(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path, config=config_for(tmp_path)) as (app, _, _, harnesses):
            harness = harnesses[0]
            store = app.state.web.tool_approvals
            builtin = store.binding(harness, "shell")
            assert builtin is not None and builtin.persistent
            store.grant(builtin)
            recreated = ToolApprovals(store.path, harness.workspace)
            binding = recreated.binding(harness, "shell")
            assert binding is not None and binding == builtin and recreated.allowed(binding)
            harness.agent.register_tool(example, name="shell")
            custom = store.binding(harness, "shell")
            assert custom is not None and not store.allowed(custom)
            original = harness.agent.tool_registry.get("shell")
            assert original is not None
            changed = copy.deepcopy(original.parameters)
            changed["required"] = ["value"]
            original.parameters = changed
            assert store.binding(harness, "shell") != custom

    asyncio.run(scenario())


def test_failed_storage_does_not_approve_call_and_once_remains_available(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path, config=config_for(tmp_path)) as (app, client, headers, harnesses):
            harness = harnesses[0]
            harness.agent.register_tool(example)
            stream = real_stream(app, headers, harness, ["example"])
            pending = await stream.event("approval")
            import sqlite3

            with patch.object(app.state.web.tool_approvals, "grant", side_effect=sqlite3.OperationalError("locked")):
                assert (await decide(client, headers, pending, "allow_tool")).status_code == 503
            assert app.state.web.active.pending is not None
            assert not app.state.web.active.pending.answer.done()
            assert (await decide(client, headers, pending, "allow")).status_code == 200
            assert (await stream.event("tool_result"))["result"] == "executed:0"
            await finish(stream)
            assert (await client.get("/api/tools", headers=headers)).json()["tool_approvals"] == []

    asyncio.run(scenario())


@pytest.mark.parametrize("terminal", ["cancelled", "expired"])
def test_cancelled_or_expired_approval_cannot_save_a_grant(tmp_path: Path, terminal: str) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path, config=config_for(tmp_path)) as (app, client, headers, harnesses):
            harness = harnesses[0]
            harness.agent.register_tool(example)
            if terminal == "expired":
                app.state.web.approval_timeout = lambda: 0.1
            stream = real_stream(app, headers, harness, ["example"])
            pending = await stream.event("approval")
            if terminal == "cancelled":
                response = await client.post("/api/cancel", headers=headers, json={"run_id": pending["run_id"]})
                assert response.status_code == 200
            else:
                closed = await stream.event("approval_closed")
                assert closed["expired"] is True and closed["decision"] == "deny"
            await stream.event("run_finished")
            await asyncio.wait_for(stream.task, HANG_GUARD)
            assert (await decide(client, headers, pending, "allow_tool")).status_code == 409
            assert (await client.get("/api/tools", headers=headers)).json()["tool_approvals"] == []

    asyncio.run(scenario())


def test_designer_grant_uses_actual_designed_harness_and_survives_new_run(tmp_path: Path) -> None:
    from nagents.designer.runtime import DesignedHarness
    from nagents.designer.schema import STARTER

    initialize = DesignedHarness.initialize

    async def designed_initialize(self: DesignedHarness, *, create_session: bool = True) -> None:
        await initialize(self, create_session=create_session)
        self.agent.register_tool(example)
        self.agent.provider = ScriptedProvider(
            [
                [ToolCallEvent(id="designed", name="example", arguments={"value": "designed"})],
                [TextDoneEvent(text="done")],
            ]
        )

    async def scenario() -> None:
        async with client_app(tmp_path, config=config_for(tmp_path)) as (app, client, headers, harnesses):
            main = harnesses[0]
            main.agent.register_tool(replacement, name="example")
            with patch.object(DesignedHarness, "initialize", designed_initialize):
                for repeated in (False, True):
                    response = await client.post(
                        "/api/designer/run", headers=headers, json={"source": STARTER, "prompt": "Use the tool"}
                    )
                    assert response.status_code == 200, response.text
                    run_id = response.json()["run_id"]
                    active = app.state.web.active
                    assert active is not None
                    if not repeated:
                        async with asyncio.timeout(HANG_GUARD):
                            while active.pending is None:
                                await asyncio.sleep(0.001)
                        pending = active.pending
                        assert isinstance(pending.harness, DesignedHarness)
                        assert pending.record["allow_tool"] is True
                        assert (await decide(client, headers, pending.record, "allow_tool")).status_code == 200
                    await asyncio.wait_for(active.task, HANG_GUARD)
                    trace = (await client.get(f"/api/designer/runs/{run_id}/0", headers=headers)).json()
                    assert trace["status"] == "completed"
                    assert any(
                        event["kind"] == ("approval_automatic" if repeated else "approval_closed")
                        and event["data"]["decision"] == "allow_tool"
                        for event in trace["events"]
                    )
                    main_binding = app.state.web.tool_approvals.binding(main, "example")
                    assert main_binding is not None and not app.state.web.tool_approvals.allowed(main_binding)

    asyncio.run(scenario())


def test_designer_setup_without_registered_tool_is_explicitly_once_only(tmp_path: Path) -> None:
    from nagents.designer.runtime import DesignedHarness
    from nagents.designer.schema import STARTER

    initialize = DesignedHarness.initialize

    async def designed_initialize(self: DesignedHarness, *, create_session: bool = True) -> None:
        await initialize(self, create_session=create_session)
        await self.approve("mcp_connect", {"server": "fixture"}, "Start configured MCP subprocess")
        self.agent.provider = ScriptedProvider([[TextDoneEvent(text="done")]])

    async def scenario() -> None:
        async with client_app(tmp_path, config=config_for(tmp_path)) as (app, client, headers, _):
            with patch.object(DesignedHarness, "initialize", designed_initialize):
                response = await client.post(
                    "/api/designer/run", headers=headers, json={"source": STARTER, "prompt": "Connect the fixture"}
                )
                assert response.status_code == 200
                active = app.state.web.active
                assert active is not None
                async with asyncio.timeout(HANG_GUARD):
                    while active.pending is None:
                        await asyncio.sleep(0.001)
                pending = active.pending
                assert pending.record["allow_tool"] is False
                assert "no registered tool definition" in pending.record["allow_tool_reason"]
                assert (await decide(client, headers, pending.record, "allow_tool")).status_code == 409
                assert (await decide(client, headers, pending.record, "allow")).status_code == 200
                await asyncio.wait_for(active.task, HANG_GUARD)
                assert (await client.get("/api/tools", headers=headers)).json()["tool_approvals"] == []

    asyncio.run(scenario())


def test_reloaded_plugin_approval_binds_advertised_generation_not_new_source(tmp_path: Path) -> None:
    extension = tmp_path / "reloadable.py"

    def write(version: str) -> None:
        extension.write_text(
            f"VERSION = {version!r}\n"
            'async def versioned(value: str = "") -> str:\n'
            '    return VERSION + ":" + value\n'
            "def setup(harness):\n"
            "    harness.agent.register_tool(versioned)\n"
        )

    class ReplaceAfterModel(AgentPlugin):
        async def after_model(self, context: RunContext) -> None:
            write("two")

    async def scenario() -> None:
        async with client_app(tmp_path, config=config_for(tmp_path)) as (app, client, headers, harnesses):
            harness = harnesses[0]
            write("one")
            await harness.load_plugin(str(extension) + ":setup")
            # Run before the stable resources dispatcher, as though the source
            # changed while the provider response was being streamed.
            harness.agent.plugins.insert(0, ReplaceAfterModel())
            stream = real_stream(app, headers, harness, ["versioned", "versioned"])
            pending = await stream.event("approval")
            advertised = harness.resources.definition("versioned")
            replacement = harness.agent.tool_registry.get("versioned")
            assert advertised is not None and advertised.func is not None
            assert replacement is not None and replacement.func is not None
            assert advertised is not replacement
            assert await advertised.func(value="probe") == "one:probe"
            assert await replacement.func(value="probe") == "two:probe"
            store = app.state.web.tool_approvals
            binding = store.binding(harness, "versioned")
            assert binding is not None and binding.persistent
            assert pending["allow_tool"] is True and pending["allow_tool_persistent"] is True
            assert (await decide(client, headers, pending, "allow_tool")).status_code == 200
            results: list[object] = []
            automatic = False
            async with asyncio.timeout(HANG_GUARD):
                while len(results) < 2 or not automatic:
                    event = await stream.output.get()
                    if event["event"] == "tool_result":
                        results.append(event.get("result"))
                    if event["event"] == "notice" and event.get("policy") == "workspace_tool_allow":
                        automatic = True
            assert results == ["one:0", "one:1"]
            await finish(stream)
            current = store.binding(harness, "versioned")
            assert current is not None and current != binding
            assert not store.allowed(current), "Approval of the old function must not approve replacement code"
            assert store.allowed(binding), "Both calls advertised together share their exact approved generation"
            # Explicitly grant the changed source, then verify unconditional
            # reloads of those same bytes keep the workspace permission.
            stream = real_stream(app, headers, harness, ["versioned"])
            pending = await stream.event("approval")
            assert (await decide(client, headers, pending, "allow_tool")).status_code == 200
            assert (await stream.event("tool_result"))["result"] == "two:0"
            await finish(stream)
            changed_binding = store.binding(harness, "versioned")
            assert changed_binding is not None and store.allowed(changed_binding)
            stream = real_stream(app, headers, harness, ["versioned"])
            automatic = False
            results.clear()
            async with asyncio.timeout(HANG_GUARD):
                while not automatic or not results:
                    event = await stream.output.get()
                    assert event["event"] != "approval", "An unchanged reload must retain Always allow"
                    if event["event"] == "notice" and event.get("policy") == "workspace_tool_allow":
                        automatic = True
                    if event["event"] == "tool_result":
                        results.append(event.get("result"))
            assert results == ["two:0"]
            await finish(stream)
            assert store.binding(harness, "versioned") == changed_binding
            # Actual advertised schemas participate even when callable/source
            # identity is unchanged; source/config capture cannot mask a change.
            definition = harness.agent.tool_registry.get("versioned")
            assert definition is not None
            schema = copy.deepcopy(definition.parameters)
            definition.parameters = {**schema, "required": ["value"]}
            changed_schema = store.binding(harness, "versioned")
            assert changed_schema is not None and changed_schema != changed_binding
            assert not store.allowed(changed_schema)
            definition.parameters = schema
            assert store.binding(harness, "versioned") == changed_binding
            harness.config.shell_timeout += 1
            await harness.resources.reload()
            changed_config = store.binding(harness, "versioned")
            assert changed_config is not None and changed_config != changed_binding
            assert not store.allowed(changed_config), "Changed setup configuration needs a fresh grant"

    asyncio.run(scenario())
