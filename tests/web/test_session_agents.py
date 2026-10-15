"""Conversation profiles persist independently and switch only at run boundaries."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from typing import cast
from uuid import uuid4

import pytest

from nagents.events import TextDoneEvent
from nagents.harness.config import AgentProfile
from nagents.harness.config import HarnessConfig
from nagents.types import Message
from nagents.web.live_bridge import MainAgentBridge
from tests.support.hang_guard import HANG_GUARD
from tests.support.web import client_app
from tests.support.web import submit_and_wait
from tests.web.test_web_subscription_lifecycle import until
from tests.web.test_web_wakeups import scheduled_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    import httpx

    from nagents.events import Event
    from tests.support.providers import FakeProvider


async def choose(client: httpx.AsyncClient, headers: dict[str, str], root: str, agent: str) -> dict[str, object]:
    current = await client.get(f"/api/sessions/{root}/agent", headers=headers)
    assert current.status_code == 200
    changed = await client.post(
        f"/api/sessions/{root}/agent",
        headers=headers,
        json={"revision": current.json()["revision"], "agent": agent},
    )
    assert changed.status_code == 200, changed.text
    return cast("dict[str, object]", changed.json())


def config(tmp_path: Path) -> HarnessConfig:
    return HarnessConfig(
        workspace=tmp_path,
        data_dir=tmp_path / "data",
        demo=True,
        profiles={
            "reviewer": AgentProfile(mode="reviewer", model="review-model"),
            "hidden": AgentProfile(user_invocable=False),
        },
    )


def test_conversation_profile_survives_restart_resume_and_fork_and_freezes_default(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path, config=config(tmp_path)) as (app, client, headers, _):
            state = app.state.web
            first = state.selected_session_id
            await state.history.add_message(first, Message(role="user", content="Keep the first conversation"))
            second = (await client.post("/api/sessions/new", headers=headers, json={})).json()["session_id"]
            await state.history.add_message(second, Message(role="user", content="Keep the second conversation"))
            selected = await choose(client, headers, first, "reviewer")
            assert selected["effective"] == {
                "agent": "reviewer",
                "model": "review-model",
                "reasoning_effort": "",
                "provider": "openai",
                "provider_kind": "openai",
                "mode": "reviewer",
            }
            assert not selected["pending"] and selected["active"] is None
            workspace = (await client.get("/api/settings", headers=headers)).json()
            assert workspace["values"]["agent"] == "assistant"
            assert (await client.get(f"/api/sessions/{second}/agent", headers=headers)).json()["agent"] == "assistant"
            saved = await client.post(
                "/api/settings",
                headers=headers,
                json={
                    "revision": workspace["revision"],
                    "values": {**workspace["values"], "agent": "reviewer"},
                },
            )
            assert saved.status_code == 200
            assert (await client.get(f"/api/sessions/{second}/agent", headers=headers)).json()["agent"] == "assistant"
            new = (await client.post("/api/sessions/new", headers=headers, json={})).json()
            assert new["agent_selection"]["agent"] == "reviewer"
            forked = await client.post(f"/api/sessions/{first}/fork", headers=headers, json={})
            assert forked.status_code == 200, forked.text
            fork_id = forked.json()["session_id"]
            assert forked.json()["agent_selection"]["agent"] == "reviewer"
            assert forked.json()["agent_selection"]["revision"] != selected["revision"]
        async with client_app(tmp_path, config=config(tmp_path)) as (_, client, headers, _):
            for root, agent in ((first, "reviewer"), (second, "assistant"), (fork_id, "reviewer")):
                resumed = await client.post("/api/sessions/resume", headers=headers, json={"session_id": root})
                assert resumed.status_code == 200, resumed.text
                assert resumed.json()["agent_selection"]["agent"] == agent

    asyncio.run(scenario())


def test_conversation_selection_revision_and_invocation_constraints(tmp_path: Path) -> None:
    async def scenario() -> None:
        configured = config(tmp_path)
        configured.read_only = True
        async with client_app(tmp_path, config=configured) as (app, client, headers, _):
            root = app.state.web.selected_session_id
            initial = (await client.get(f"/api/sessions/{root}/agent", headers=headers)).json()
            assert initial["effective"]["mode"] == "reviewer"
            assert next(item for item in initial["profiles"] if item["name"] == "hidden")["disabled_reason"]
            hidden = await client.post(
                f"/api/sessions/{root}/agent",
                headers=headers,
                json={
                    "revision": initial["revision"],
                    "agent": "hidden",
                },
            )
            assert hidden.status_code == 422
            updated = await choose(client, headers, root, "reviewer")
            stale = await client.post(
                f"/api/sessions/{root}/agent",
                headers=headers,
                json={
                    "revision": initial["revision"],
                    "agent": "assistant",
                },
            )
            assert stale.status_code == 409
            effective = updated["effective"]
            assert isinstance(effective, dict) and effective["mode"] == "reviewer"
            unknown = await client.post(
                f"/api/sessions/{root}/agent",
                headers=headers,
                json={
                    "revision": updated["revision"],
                    "agent": "missing",
                },
            )
            assert unknown.status_code == 422

    asyncio.run(scenario())


@pytest.mark.requires_posix
def test_pending_profile_keeps_running_identity_and_queues_next_message_for_new_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        calls: list[tuple[str, str, str]] = []

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            text = str(messages[-1].content)
            calls.append((text, provider.harness_config.agent, provider.model))
            if text == "first":
                entered.set()
                await release.wait()
            yield TextDoneEvent(text=f"Done {text}")

        async with scheduled_app(tmp_path, monkeypatch, script) as (app, client, headers, state, _, _):
            root = state.selected_session_id
            state.harness.config.profiles["reviewer"].model = "review-model"
            first = asyncio.create_task(submit_and_wait(app, headers, root, "first"))
            try:
                async with asyncio.timeout(HANG_GUARD):
                    await entered.wait()
                run = state.run_for(root)
                assert run is not None and run.harness is not None
                original_provider = run.harness.agent.provider
                changed = await choose(client, headers, root, "reviewer")
                assert changed["pending"]
                assert changed["active"] == {
                    "agent": "assistant",
                    "model": "fake-model",
                    "reasoning_effort": "",
                    "provider": "fixture",
                    "provider_kind": "openai",
                    "mode": "reviewer",
                }
                assert run.harness.config.agent == "assistant"
                assert run.harness.agent.provider is original_provider
                assert run.harness.mode == "reviewer"
                assert state.harness.config.agent == "assistant" and state.harness.mode == "build"
                assert MainAgentBridge(state, root)._target()["agent"] == "Main assistant"
                second_id = str(uuid4())
                response = await client.post(
                    "/api/messages",
                    headers=headers,
                    json={
                        "session_id": root,
                        "message_id": second_id,
                        "prompt": "second",
                    },
                )
                assert response.status_code == 200
                assert await state.queued_inputs.pull(run) is None
                release.set()
                await first
                await until(lambda: len(calls) == 2 and state.run_for(root) is None)
                assert calls == [("first", "assistant", "fake-model"), ("second", "reviewer", "review-model")]
                snapshot = (await client.get(f"/api/sessions/{root}", headers=headers)).json()
                assert snapshot["agent_selection"]["effective"]["agent"] == "reviewer"
                assert not snapshot["agent_selection"]["pending"]
                markers = [row for row in snapshot["history"] if row["role"] == "agent_changed"]
                assert len(markers) == 1 and markers[0]["agent"] == "reviewer"
                assert all(message.content != "Agent: reviewer" for message in await state.history.get_history(root))
                assert MainAgentBridge(state, root)._target()["model"] == "review-model"
            finally:
                release.set()
                await first

    asyncio.run(scenario())


@pytest.mark.requires_posix
def test_two_conversations_run_different_profiles_and_context_without_workspace_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        entered = {name: asyncio.Event() for name in ("A", "B")}
        release = asyncio.Event()
        seen: dict[str, str] = {}

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            text = str(messages[-1].content)
            seen[text] = provider.harness_config.agent
            entered[text].set()
            await release.wait()
            yield TextDoneEvent(text=f"Done {text}")

        async with scheduled_app(tmp_path, monkeypatch, script) as (app, client, headers, state, _, _):
            first = state.selected_session_id
            await state.history.add_message(first, Message(role="user", content="retain"))
            second = (await client.post("/api/sessions/new", headers=headers, json={})).json()["session_id"]
            state.harness.config.profiles["reviewer"].model = "review-model"
            await choose(client, headers, first, "reviewer")
            one = asyncio.create_task(submit_and_wait(app, headers, first, "A"))
            two = asyncio.create_task(submit_and_wait(app, headers, second, "B"))
            try:
                async with asyncio.timeout(HANG_GUARD):
                    await asyncio.gather(*(event.wait() for event in entered.values()))
                assert seen == {"A": "reviewer", "B": "assistant"}
                first_run, second_run = state.run_for(first), state.run_for(second)
                assert first_run is not None and second_run is not None
                assert first_run.harness is not second_run.harness
                assert state.settings.values.agent == "assistant" and state.harness.config.agent == "assistant"
                context = (await client.get(f"/api/sessions/{first}/context", headers=headers)).json()
                assert context["model"] == "review-model"
            finally:
                release.set()
                await asyncio.gather(one, two)

    asyncio.run(scenario())


@pytest.mark.requires_posix
def test_profile_change_markers_are_display_only_and_inherited_on_fork(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            assert not any("Agent: reviewer" in str(message.content) for message in messages)
            yield TextDoneEvent(text="reviewed")

        async with scheduled_app(tmp_path, monkeypatch, script) as (app, client, headers, state, _, _):
            root = state.selected_session_id
            await choose(client, headers, root, "reviewer")
            events = await submit_and_wait(app, headers, root, "Review this")
            assert len([event for event in events if event.get("event") == "agent_changed"]) == 1
            history = await state.history.snapshot(root)
            assert history[0]["role"] == "agent_changed"
            forked = await client.post(f"/api/sessions/{root}/fork", headers=headers, json={})
            assert forked.status_code == 200, forked.text
            copied = [row for row in forked.json()["history"] if row["role"] == "agent_changed"]
            assert len(copied) == 1 and copied[0]["agent"] == "reviewer"
            assert copied[0]["history_id"] != history[0]["history_id"]
            assert forked.json()["agent_selection"]["agent"] == "reviewer"

    asyncio.run(scenario())


def test_read_only_selection_denies_pending_approval_and_future_custom_tools(tmp_path: Path) -> None:
    from tests.web.test_web_tool_approvals import config_for
    from tests.web.test_web_tool_approvals import decide
    from tests.web.test_web_tool_approvals import example
    from tests.web.test_web_tool_approvals import finish
    from tests.web.test_web_tool_approvals import real_stream

    async def scenario() -> None:
        configured = config_for(tmp_path)
        configured.profiles["reviewer"] = AgentProfile(mode="reviewer")
        async with client_app(tmp_path, config=configured) as (app, client, headers, harnesses):
            harness = harnesses[0]
            root = harness.session_id
            harness.agent.register_tool(example)
            stream = real_stream(app, headers, harness, ["example", "example"])
            pending = await stream.event("approval")
            changed = await choose(client, headers, root, "reviewer")
            effective = changed["effective"]
            assert isinstance(effective, dict) and effective["mode"] == "reviewer"
            assert harness.config.agent == "assistant" and harness.mode == "reviewer"
            closed = await stream.event("approval_closed")
            assert closed["decision"] == "deny"
            assert (await decide(client, headers, pending, "allow_tool")).status_code == 409
            await finish(stream)
            assert (await client.get("/api/tools", headers=headers)).json()["tool_approvals"] == []
            history = await app.state.web.history.get_history(root)
            outputs = [str(message.content) for message in history if message.role == "tool"]
            assert outputs and not any("executed:" in output for output in outputs)
            await stream.disconnect()

    asyncio.run(scenario())


def test_removed_profile_keeps_history_and_picker_accessible(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path, config=config(tmp_path)) as (app, client, headers, _):
            root = app.state.web.selected_session_id
            await choose(client, headers, root, "reviewer")
            app.state.web.harness.config.profiles.pop("reviewer")
            response = await client.get(f"/api/sessions/{root}", headers=headers)
            assert response.status_code == 200, response.text
            assert response.json()["agent_selection"]["agent"] == "reviewer"
            changed = await choose(client, headers, root, "assistant")
            assert changed["agent"] == "assistant"

    asyncio.run(scenario())


@pytest.mark.requires_posix
def test_idle_profile_context_includes_workspace_instructions_and_skills_without_loading_plugins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        (tmp_path / "AGENTS.md").write_text("Use the workspace-specific verification procedure.\n")
        skill = tmp_path / ".agents" / "skills" / "verification"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nname: verification\ndescription: Workspace verification steps\n---\nCheck the result.\n"
        )

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            yield TextDoneEvent(text="done")

        async with scheduled_app(tmp_path, monkeypatch, script) as (_, client, headers, state, _, _):
            root = state.selected_session_id
            state.harness.config.profiles["writer"] = AgentProfile(mode="build", model="writer-model")
            await choose(client, headers, root, "writer")
            async with state.session_agents.view(root) as preview:
                assert not preview._initialized
                assert "workspace-specific verification procedure" in str(preview.agent.system_prompt)
                assert "verification" in preview.agent.skills
                before = await preview.agent.context_stats(root)
                await preview.initialize(create_session=False)
                after = await preview.agent.context_stats(root)
                keys = {"system_prompt", "skills"}
                assert all(part.tokens > 0 for part in before.components if part.key in keys)
                assert {part.key: part.tokens for part in before.components if part.key in keys} == {
                    part.key: part.tokens for part in after.components if part.key in keys
                }
                assert "schedule_wakeup" in preview.agent.tool_registry.names()
            assert state.settings.values.agent == "assistant"

    asyncio.run(scenario())


@pytest.mark.requires_posix
def test_read_only_switch_restricts_pending_child_and_does_not_loosen_it_on_later_parent_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nagents.events import ToolCallEvent
    from tests.support.web import LiveStream

    async def scenario() -> None:
        second_ready, release = asyncio.Event(), asyncio.Event()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index == 0 and len(provider.requests) == 1:
                yield ToolCallEvent(id="delegate-child", name="delegate", arguments={"prompt": "Create child output"})
            elif provider.index == 1 and len(provider.requests) == 1:
                yield ToolCallEvent(id="child-write", name="write", arguments={"path": "child.txt", "content": "no"})
            elif provider.index == 1 and len(provider.requests) == 2:
                second_ready.set()
                await release.wait()
                yield ToolCallEvent(
                    id="child-write-again", name="write", arguments={"path": "child.txt", "content": "no"}
                )
            else:
                yield TextDoneEvent(text="done")

        async with scheduled_app(tmp_path, monkeypatch, script) as (app, client, headers, state, _, _):
            root = state.selected_session_id
            stream = LiveStream(app, headers, root, "Delegate this")
            pending = await stream.event("approval")
            original = state.harness_for(root)
            child = original.tasks._children[str(pending["task_id"])]
            try:
                await choose(client, headers, root, "reviewer")
                closed = await stream.event("approval_closed")
                assert closed["decision"] == "deny"
                async with asyncio.timeout(HANG_GUARD):
                    await second_ready.wait()
                await choose(client, headers, root, "assistant")
                assert original.mode == child.mode == "reviewer"
            finally:
                release.set()
            await stream.event("run_finished")
            await stream.task
            await stream.disconnect()
            assert not (tmp_path / "child.txt").exists()
            await submit_and_wait(app, headers, root, "Next parent run")
            assert original.mode == "build"
            assert child._permission_ceiling == child.mode == "reviewer"

    asyncio.run(scenario())


def test_new_session_resets_reused_empty_draft_to_workspace_default_without_accumulation(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with client_app(tmp_path, config=config(tmp_path)) as (app, client, headers, _):
            state = app.state.web
            root = state.selected_session_id
            for _ in range(3):
                previous = await choose(client, headers, root, "reviewer")
                fresh = await client.post("/api/sessions/new", headers=headers, json={})
                assert fresh.status_code == 200, fresh.text
                body = fresh.json()
                assert body["session_id"] == root
                assert body["agent_selection"]["agent"] == "assistant"
                assert body["agent_selection"]["revision"] != previous["revision"]
                assert state.session_agents.selected(root) == "assistant"
                assert body["history"] == []
                assert [item["id"] for item in body["sessions"]] == [root]
                stale = await client.post(
                    f"/api/sessions/{root}/agent",
                    headers=headers,
                    json={
                        "revision": previous["revision"],
                        "agent": "reviewer",
                    },
                )
                assert stale.status_code == 409
            workspace = (await client.get("/api/settings", headers=headers)).json()
            saved = await client.post(
                "/api/settings",
                headers=headers,
                json={
                    "revision": workspace["revision"],
                    "values": {**workspace["values"], "agent": "reviewer"},
                },
            )
            assert saved.status_code == 200
            assert state.session_agents.selected(root) == "assistant"
            fresh = (await client.post("/api/sessions/new", headers=headers, json={})).json()
            assert fresh["session_id"] == root
            assert fresh["agent_selection"]["agent"] == "reviewer"
            assert [item["id"] for item in fresh["sessions"]] == [root]
            await state.history.add_message(root, Message(role="user", content="Retain this conversation"))
            empty = (await client.post("/api/sessions/new", headers=headers, json={})).json()["session_id"]
            await choose(client, headers, empty, "assistant")
            resumed = await client.post("/api/sessions/resume", headers=headers, json={"session_id": root})
            assert resumed.status_code == 200
            assert [item["id"] for item in resumed.json()["sessions"]] == [root]
            assert empty not in state.session_agents.choices
            remaining = await state.channels.store._transaction(
                lambda db: db.execute("SELECT session_id FROM ngn_web_session_agents ORDER BY session_id").fetchall()
            )
            assert remaining == [(root,)]

    asyncio.run(scenario())


@pytest.mark.requires_posix
def test_idle_retained_context_uses_current_connection_after_endpoint_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nagents.events import ToolCallEvent
    from nagents.provider.context import ContextWindow
    from tests.support.providers import FakeProvider as ProviderFixture

    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if messages[-1].content == "Keep a completed child for inspection":
                yield ToolCallEvent(id="retain-child", name="delegate", arguments={"prompt": "Review the fixture"})
            else:
                yield TextDoneEvent(text="done")

        metadata_reads: list[str] = []

        async def context_window(provider: FakeProvider) -> ContextWindow:
            endpoint = provider.harness_config.provider_profile().base_url
            metadata_reads.append(endpoint)
            return ContextWindow(800000 if endpoint == "https://updated.invalid/v1" else 200000, "provider")

        monkeypatch.setattr(ProviderFixture, "resolve_context_window", context_window)
        async with scheduled_app(tmp_path, monkeypatch, script) as (app, client, headers, state, _, _):
            root = state.selected_session_id
            registry = (await client.get("/api/provider-scopes/global/providers", headers=headers)).json()
            initial = await client.put(
                "/api/provider-scopes/global/providers/fixture",
                headers=headers,
                json={
                    "revision": registry["revision"],
                    "profile": {
                        "kind": "openai_compatible",
                        "auth": "api-key",
                        "api": "responses",
                        "base_url": "https://original.invalid/v1",
                    },
                },
            )
            assert initial.status_code == 200, initial.text
            await submit_and_wait(app, headers, root, "Keep a completed child for inspection")
            retained = state.executions.owners[root]
            assert retained.tasks.list() and state.run_for(root) is None
            old_provider = retained.agent.provider
            assert isinstance(old_provider, ProviderFixture)
            before = (await client.get(f"/api/sessions/{root}/context", headers=headers)).json()
            assert before["context_window"] == 200000
            registry = (await client.get("/api/provider-scopes/global/providers", headers=headers)).json()
            changed = await client.put(
                "/api/provider-scopes/global/providers/fixture",
                headers=headers,
                json={
                    "revision": registry["revision"],
                    "profile": {
                        "kind": "openai_compatible",
                        "auth": "api-key",
                        "api": "responses",
                        "base_url": "https://updated.invalid/v1",
                    },
                },
            )
            assert changed.status_code == 200, changed.text
            assert state.harness is not retained
            assert retained.agent.provider is old_provider and not old_provider.closed
            assert retained.config.provider == state.harness.config.provider == "fixture"
            assert retained.agent.provider.model == state.harness.agent.provider.model == "fake-model"
            after = await client.get(f"/api/sessions/{root}/context", headers=headers)
            assert after.status_code == 200, after.text
            assert after.json()["context_window"] == 800000
            assert after.json()["context_window_source"] == "provider"
            assert metadata_reads[-1] == "https://updated.invalid/v1"
            assert retained.agent.provider is old_provider
            assert retained.config.provider_profile().base_url == "https://original.invalid/v1"

    asyncio.run(scenario())
