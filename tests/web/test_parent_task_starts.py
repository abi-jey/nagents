"""Parent-scoped human starts preserve the real delegated task tree."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from nagents.events import TextDoneEvent
from nagents.harness.config import AgentProfile
from nagents.harness.subagent_settings import SubagentLimits
from nagents.harness.subagents import TaskInfo
from tests.support.hang_guard import HANG_GUARD
from tests.web.test_web_subscription_lifecycle import until
from tests.web.test_web_wakeups import scheduled_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.types import Message
    from tests.support.providers import FakeProvider

pytestmark = pytest.mark.requires_posix


def _start_body(agent: str, text: str, *, parent_task_id: str = "", message_id: str = "") -> dict[str, str]:
    body = {"message_id": message_id or str(uuid4()), "agent": agent, "text": text}
    if parent_task_id:
        body["parent_task_id"] = parent_task_id
    return body


def test_active_parent_start_creates_real_child_with_inherited_reviewer_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        parent_started, child_started = asyncio.Event(), asyncio.Event()
        release_parent, release_child = asyncio.Event(), asyncio.Event()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            del provider
            prompt = next(str(message.content) for message in reversed(messages) if message.role == "user")
            if prompt == "PARENT ASSIGNMENT":
                parent_started.set()
                await release_parent.wait()
            elif prompt == "NESTED ASSIGNMENT":
                child_started.set()
                await release_child.wait()
            yield TextDoneEvent(text="done")

        async with scheduled_app(tmp_path, monkeypatch, script) as (_, client, headers, state, _, _):
            root = state.selected_session_id
            path = f"/api/sessions/{root}/tasks"
            owner = None
            child_id = ""
            try:
                response = await client.post(path, headers=headers, json=_start_body("reviewer", "PARENT ASSIGNMENT"))
                assert response.status_code == 200, response.text
                parent_id = response.json()["task"]["id"]
                async with asyncio.timeout(HANG_GUARD):
                    await parent_started.wait()

                indexed = (await client.get(path, headers=headers)).json()
                parent_view = next(item for item in indexed["tasks"] if item["id"] == parent_id)
                assert parent_view["parent_task_id"] == ""
                assert parent_view["depth"] == 1
                assert parent_view["can_start_children"] is True
                assert "assistant" in parent_view["child_agents"]
                assert parent_view["child_start_unavailable_reason"] == ""

                child_response = await client.post(
                    path,
                    headers=headers,
                    json=_start_body("assistant", "NESTED ASSIGNMENT", parent_task_id=parent_id),
                )
                assert child_response.status_code == 200, child_response.text
                child_id = child_response.json()["task"]["id"]
                assert child_response.json()["receipt"]["task_id"] == child_id
                assert child_response.json()["receipt"]["session_id"] == root
                async with asyncio.timeout(HANG_GUARD):
                    await child_started.wait()

                owner = state.executions.owners[root]
                parent = owner.tasks.child(parent_id)
                child_info = owner.tasks._lookup(child_id)
                child = owner.tasks.child(child_id)
                assert child_info.parent_task_id == parent_id
                assert child_info.parent_session_id == parent.session_id
                assert child_info.session_id == root and child_info.depth == 2
                assert child_response.json()["receipt"]["child_session_id"] == child_info.child_session_id
                assert child_info.mode == "reviewer"
                assert child.mode == "reviewer"
                assert child._permission_ceiling == "reviewer"
                assert child.mode_for_profile("assistant") == "reviewer"
                assert [item.id for item in parent.tasks.list()] == [child_id]

                indexed = (await client.get(path, headers=headers)).json()
                child_view = next(item for item in indexed["tasks"] if item["id"] == child_id)
                assert child_view["parent_task_id"] == parent_id
                assert child_view["parent_session_id"] == parent.session_id
                assert child_view["depth"] == 2
                assert child_view["mode"] == "reviewer"
                assert child_view["can_start_children"] is False
                assert child_view["child_agents"] == []

                release_child.set()
                await until(lambda: owner.tasks._lookup(child_id).status == "completed")
            finally:
                release_child.set()
                release_parent.set()
                await until(lambda: state.run_for(root) is None)
        assert parent_started.is_set() and child_started.is_set()

    asyncio.run(scenario())


def test_parent_start_rejects_idle_foreign_and_remote_parents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            del provider, messages
            yield TextDoneEvent(text="done")

        async with scheduled_app(tmp_path, monkeypatch, script) as (_, client, headers, state, _, _):
            root = state.selected_session_id
            path = f"/api/sessions/{root}/tasks"
            response = await client.post(path, headers=headers, json=_start_body("reviewer", "COMPLETE PARENT"))
            assert response.status_code == 200, response.text
            parent_id = response.json()["task"]["id"]
            await until(lambda: state.run_for(root) is None)
            completed = await client.get(path, headers=headers)
            parent_view = next(item for item in completed.json()["tasks"] if item["id"] == parent_id)
            assert parent_view["status"] == "completed"
            assert parent_view["can_start_children"] is False
            assert "must be running" in parent_view["child_start_unavailable_reason"]

            denied_idle = await client.post(
                path, headers=headers, json=_start_body("assistant", "NO IDLE CHILD", parent_task_id=parent_id)
            )
            assert denied_idle.status_code == 409
            assert "must be running" in denied_idle.json()["detail"]

            owner = state.executions.owners[root]
            owner.tasks._infos["foreign-parent"] = TaskInfo(
                id="foreign-parent", name="Foreign", session_id="ngn-foreign-root", status="running"
            )
            denied_foreign = await client.post(
                path,
                headers=headers,
                json=_start_body("assistant", "NO FOREIGN CHILD", parent_task_id="foreign-parent"),
            )
            assert denied_foreign.status_code == 404

            owner.tasks._infos["remote-parent"] = TaskInfo(
                id="remote-parent",
                name="Remote",
                session_id=root,
                child_session_id="ngn-remote-child",
                parent_session_id=root,
                status="running",
                provider="a2a",
                provider_kind="a2a",
            )
            denied_remote = await client.post(
                path,
                headers=headers,
                json=_start_body("assistant", "NO REMOTE CHILD", parent_task_id="remote-parent"),
            )
            assert denied_remote.status_code == 409
            assert "Remote agents manage their children" in denied_remote.json()["detail"]
            assert owner.tasks._used == 1
            assert len(owner.tasks._infos) == 3

    asyncio.run(scenario())


def test_parent_profile_availability_intersects_allowed_agents_and_user_invocability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        parent_started, release_parent = asyncio.Event(), asyncio.Event()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            del provider
            prompt = next(str(message.content) for message in reversed(messages) if message.role == "user")
            if prompt == "RESTRICTED PARENT":
                parent_started.set()
                await release_parent.wait()
            yield TextDoneEvent(text="done")

        async with scheduled_app(tmp_path, monkeypatch, script) as (_, client, headers, state, _, _):
            # The root may start the reviewer profile; that parent's own allowlist
            # then limits its children. The other profiles isolate subagent and
            # human-invocation restrictions.
            state.harness.config.profiles["reviewer"] = AgentProfile(
                mode="reviewer", allowed_agents=("assistant", "internal", "hidden")
            )
            state.harness.config.profiles["internal"] = AgentProfile(user_invocable=False)
            state.harness.config.profiles["hidden"] = AgentProfile(subagent_available=False)
            root = state.selected_session_id
            path = f"/api/sessions/{root}/tasks"
            try:
                response = await client.post(path, headers=headers, json=_start_body("reviewer", "RESTRICTED PARENT"))
                assert response.status_code == 200, response.text
                parent_id = response.json()["task"]["id"]
                async with asyncio.timeout(HANG_GUARD):
                    await parent_started.wait()

                owner = state.executions.owners[root]
                indexed = (await client.get(path, headers=headers)).json()
                parent_view = next(item for item in indexed["tasks"] if item["id"] == parent_id)
                assert parent_view["child_agents"] == ["assistant"]

                for agent in ("reviewer", "internal", "hidden"):
                    denied = await client.post(
                        path,
                        headers=headers,
                        json=_start_body(agent, f"DENIED {agent}", parent_task_id=parent_id),
                    )
                    assert denied.status_code == 403, denied.text
                    assert "cannot be started under this parent" in denied.json()["detail"]
                assert len(owner.tasks._infos) == 1
                assert owner.tasks._used == 1
            finally:
                release_parent.set()
                await until(lambda: state.run_for(root) is None)

    asyncio.run(scenario())


def test_parent_start_respects_root_shared_execution_quota(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        parent_started, release_parent = asyncio.Event(), asyncio.Event()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            del provider
            prompt = next(str(message.content) for message in reversed(messages) if message.role == "user")
            if prompt == "QUOTA PARENT":
                parent_started.set()
                await release_parent.wait()
            yield TextDoneEvent(text="done")

        async with scheduled_app(tmp_path, monkeypatch, script) as (_, client, headers, state, _, _):
            root = state.selected_session_id
            path = f"/api/sessions/{root}/tasks"
            try:
                response = await client.post(path, headers=headers, json=_start_body("reviewer", "QUOTA PARENT"))
                assert response.status_code == 200, response.text
                parent_id = response.json()["task"]["id"]
                async with asyncio.timeout(HANG_GUARD):
                    await parent_started.wait()
                owner = state.executions.owners[root]
                owner.tasks.accept_limits(
                    SubagentLimits(max_concurrent_subagents=3, max_subagent_executions=1, max_subagent_depth=2)
                )

                indexed = (await client.get(path, headers=headers)).json()
                parent_view = next(item for item in indexed["tasks"] if item["id"] == parent_id)
                assert set(parent_view["child_agents"]) == {"assistant", "reviewer"}
                assert parent_view["can_start_children"] is False
                assert "execution budget is exhausted" in parent_view["child_start_unavailable_reason"]

                body = _start_body("assistant", "OVER QUOTA", parent_task_id=parent_id)
                denied = await client.post(
                    path,
                    headers=headers,
                    json=body,
                )
                assert denied.status_code == 409, denied.text
                assert "Subagent limits prevent this start" in denied.json()["detail"]
                assert owner.tasks._used == 1
                assert len(owner.tasks._infos) == 1
                assert (
                    await state.tasks.store.duplicate(
                        root, f"profile:{parent_id}:assistant", body["message_id"], body["text"]
                    )
                    == {}
                )
            finally:
                release_parent.set()
                await until(lambda: state.run_for(root) is None)

    asyncio.run(scenario())


def test_parent_start_idempotency_is_scoped_to_parent_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        parent_prompts: set[str] = set()
        parent_started = asyncio.Event()
        child_started = asyncio.Event()
        release_parents, release_child = asyncio.Event(), asyncio.Event()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            del provider
            prompt = next(str(message.content) for message in reversed(messages) if message.role == "user")
            if prompt in {"PARENT ONE", "PARENT TWO"}:
                parent_prompts.add(prompt)
                parent_started.set()
                await release_parents.wait()
            elif prompt == "IDEMPOTENT CHILD":
                child_started.set()
                await release_child.wait()
            yield TextDoneEvent(text="done")

        async with scheduled_app(tmp_path, monkeypatch, script) as (_, client, headers, state, _, _):
            root = state.selected_session_id
            path = f"/api/sessions/{root}/tasks"
            try:
                first = await client.post(path, headers=headers, json=_start_body("reviewer", "PARENT ONE"))
                assert first.status_code == 200, first.text
                first_parent = first.json()["task"]["id"]
                async with asyncio.timeout(HANG_GUARD):
                    await parent_started.wait()

                second = await client.post(path, headers=headers, json=_start_body("reviewer", "PARENT TWO"))
                assert second.status_code == 200, second.text
                second_parent = second.json()["task"]["id"]
                await until(lambda: len(parent_prompts) == 2)
                assert first_parent != second_parent

                message_id = str(uuid4())
                body = _start_body("assistant", "IDEMPOTENT CHILD", parent_task_id=first_parent, message_id=message_id)
                started = await client.post(path, headers=headers, json=body)
                assert started.status_code == 200, started.text
                child_id = started.json()["task"]["id"]
                async with asyncio.timeout(HANG_GUARD):
                    await child_started.wait()
                owner = state.executions.owners[root]
                used = owner.tasks._used

                duplicate = await client.post(path, headers=headers, json=body)
                assert duplicate.status_code == 200, duplicate.text
                assert duplicate.json()["task"]["id"] == child_id
                assert duplicate.json()["receipt"]["task_id"] == child_id
                assert owner.tasks._used == used

                different_parent = await client.post(
                    path,
                    headers=headers,
                    json=_start_body(
                        "assistant", "IDEMPOTENT CHILD", parent_task_id=second_parent, message_id=message_id
                    ),
                )
                assert different_parent.status_code == 409
                assert "different target" in different_parent.json()["detail"]
                assert owner.tasks._used == used
                assert len(owner.tasks._infos) == 3
            finally:
                release_child.set()
                release_parents.set()
                await until(lambda: state.run_for(root) is None)

    asyncio.run(scenario())
