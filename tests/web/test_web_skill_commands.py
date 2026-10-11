"""Explicit skill calls share durable input identities and model boundaries."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from typing import cast
from unittest.mock import AsyncMock
from unittest.mock import patch
from uuid import uuid4

import pytest

from nagents.events import TextDoneEvent
from nagents.harness.commands import CommandResult
from nagents.harness.provider import HarnessProvider
from nagents.harness.providers import ProviderRegistry
from nagents.harness.providers import ScopedProviderRegistryStore
from nagents.harness.runtime import Harness
from nagents.types import Message
from tests.support.hang_guard import HANG_GUARD
from tests.support.web import ControlledHarness
from tests.support.web import client_app
from tests.test_web_live_bridge import _config as _base_config

pytestmark = pytest.mark.requires_posix

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.harness.config import HarnessConfig
    from nagents.web.service import WebState


def _config(path: Path) -> HarnessConfig:
    config = _base_config(path)
    profile = config.provider_profile()
    config.providers, config.provider = {"fixture": profile}, "fixture"
    ScopedProviderRegistryStore(path).workspace_store.save(
        ProviderRegistry(active="fixture", providers={"fixture": profile}), expected="0" * 64
    )
    return config


pytestmark = pytest.mark.requires_posix


def skill_file(workspace: Path, name: str = "audit", content: str = "Review $ARGUMENTS") -> Path:
    path = workspace / ".agents" / "skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: Test skill {name}\n---\n{content}\n")
    return path


async def idle_history(state: WebState, root: str, count: int) -> list[dict[str, object]]:
    async with asyncio.timeout(HANG_GUARD):
        while True:
            rows = await state.history.snapshot(root)
            if sum(row["role"] == "user" for row in rows) >= count and state.run_for(root) is None:
                # The producer finishes before the inbox worker commits its
                # terminal receipt. Forking also requires that queue to be idle.
                pending = await state.channels.store._transaction(
                    lambda db: (
                        db.execute(
                            "SELECT 1 FROM ngn_web_inbox WHERE session_id = ? AND status IN ('queued', 'running') LIMIT 1",
                            (root,),
                        ).fetchone()
                        is not None
                    )
                )
                if not pending:
                    return rows
            await asyncio.sleep(0.005)


def test_skill_catalog_is_scoped_metadata_and_reserved_names_never_execute_callbacks(tmp_path: Path) -> None:
    for name in ("audit", "help", "settings", "resume", "custom"):
        skill_file(tmp_path, name, "PRIVATE_SKILL_CONTENT")

    async def scenario() -> None:
        async with client_app(tmp_path, config=_config(tmp_path)) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            root = state.selected_session_id
            callback = AsyncMock(return_value=CommandResult(message="must not execute"))
            state.harness.commands.register("custom", "Registered plugin callback", handler=callback)
            response = await client.get("/api/skills", headers=headers, params={"session_id": root})
            assert response.status_code == 200
            catalog = response.json()
            assert catalog["session_id"] == root and catalog["agent"] == "assistant" and catalog["enabled"]
            entries = {item["name"]: item for item in catalog["skills"]}
            assert entries["audit"]["command"] == "audit"
            for name in ("help", "settings", "resume", "custom"):
                assert entries[name]["command"] == entries[name]["canonical_command"] == f"skill:{name}"
                rejected = await client.post(
                    "/api/messages",
                    headers=headers,
                    json={
                        "session_id": root,
                        "message_id": str(uuid4()),
                        "prompt": f"/{name} task",
                        "command": "skill",
                    },
                )
                assert rejected.status_code == 422
            assert "PRIVATE_SKILL_CONTENT" not in response.text and "SKILL.md" not in response.text
            callback.assert_not_awaited()
            assert state.selected_session_id == root and not await state.harness.history()
            missing = await client.get("/api/skills", headers=headers, params={"session_id": "ngn-missing"})
            assert missing.status_code == 404

    asyncio.run(scenario())


@pytest.mark.parametrize("prompt", ["/missing task", "/skill:missing task", "/skill:../audit", "ordinary text", "/"])
def test_invalid_skill_is_rejected_before_admission(tmp_path: Path, prompt: str) -> None:
    skill_file(tmp_path)

    async def scenario() -> None:
        async with client_app(tmp_path, config=_config(tmp_path)) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            response = await client.post(
                "/api/messages",
                headers=headers,
                json={
                    "session_id": state.selected_session_id,
                    "message_id": str(uuid4()),
                    "prompt": prompt,
                    "command": "skill",
                },
            )
            assert response.status_code == 422
            assert not await state.channels.store.has_pending() and not await state.harness.history()

    asyncio.run(scenario())


def test_disabled_skills_are_listed_but_cannot_be_admitted(tmp_path: Path) -> None:
    skill_file(tmp_path)

    async def scenario() -> None:
        async with client_app(tmp_path, config=_config(tmp_path)) as (app, client, headers, _):
            state = cast("WebState", app.state.web)
            policy = state.harness.tool_settings
            policy.save({"assistant": {"skill": False}}, policy.revision)
            catalog = (
                await client.get("/api/skills", headers=headers, params={"session_id": state.selected_session_id})
            ).json()
            assert catalog["enabled"] is False and not any(item["enabled"] for item in catalog["skills"])
            response = await client.post(
                "/api/messages",
                headers=headers,
                json={
                    "session_id": state.selected_session_id,
                    "message_id": str(uuid4()),
                    "prompt": "/audit task",
                    "command": "skill",
                },
            )
            assert response.status_code == 422 and "disabled" in response.json()["detail"]
            assert not await state.channels.store.has_pending()

    asyncio.run(scenario())


def test_retry_precedes_changed_skill_discovery_and_preserves_raw_chat_and_fork(tmp_path: Path) -> None:
    path = skill_file(tmp_path, content="OLD_SKILL $ARGUMENTS")
    seen: list[str] = []

    async def generate(provider: HarnessProvider, messages: list[Message], **kwargs: object) -> AsyncIterator[Event]:
        seen.append(str(messages[-1].content))
        yield TextDoneEvent(text="Done")

    async def scenario() -> None:
        with (
            patch.object(ControlledHarness, "run", Harness.run),
            patch.object(HarnessProvider, "verify_model", AsyncMock(return_value=True)),
            patch.object(HarnessProvider, "generate", generate),
        ):
            async with client_app(tmp_path, config=_config(tmp_path)) as (app, client, headers, _):
                state = cast("WebState", app.state.web)
                root = state.selected_session_id
                raw = "/audit  preserve\nthis $() {literal}  "
                body = {"session_id": root, "message_id": str(uuid4()), "prompt": raw, "command": "skill"}
                with state.idle():
                    first = await client.post("/api/messages", headers=headers, json=body)
                    assert first.status_code == 200
                    path.unlink()
                    retry = await client.post("/api/messages", headers=headers, json=body)
                    assert retry.status_code == 200 and retry.json() == first.json()
                    conflict = await client.post(
                        "/api/messages", headers=headers, json={**body, "prompt": "/missing changed"}
                    )
                    assert conflict.status_code == 409
                    skill_file(tmp_path, content="CURRENT_SKILL $ARGUMENTS")
                rows = await idle_history(state, root, 1)
                assert len(seen) == 1 and "CURRENT_SKILL  preserve\nthis $() {literal}  " in seen[0]
                assert "OLD_SKILL" not in seen[0] and "$ARGUMENTS" not in seen[0]
                users = [row for row in rows if row["role"] == "user"]
                assert len(users) == 1 and users[0]["content"] == raw and users[0]["message_id"] == body["message_id"]
                assert users[0]["source_verified"] is True
                raw_history = await state.history.get_history(root)
                assert raw_history[0].content == seen[0]
                fork = await client.post(f"/api/sessions/{root}/fork", headers=headers, json={})
                assert fork.status_code == 200, fork.text
                copied = [row for row in fork.json()["history"] if row["role"] == "user"]
                assert copied[0]["content"] == raw and not copied[0].get("message_id")

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["content", "deleted", "disabled"])
def test_skill_followup_loads_at_next_response_boundary_in_its_own_root(tmp_path: Path, change: str) -> None:
    path = skill_file(tmp_path, content="OLD_SKILL")

    async def scenario() -> None:
        entered, release = asyncio.Event(), asyncio.Event()
        prompts: list[str] = []

        async def generate(
            provider: HarnessProvider, messages: list[Message], **kwargs: object
        ) -> AsyncIterator[Event]:
            prompt = str(messages[-1].content)
            prompts.append(prompt)
            if prompt == "hold":
                entered.set()
                await release.wait()
            yield TextDoneEvent(text="Done")

        with (
            patch.object(ControlledHarness, "run", Harness.run),
            patch.object(HarnessProvider, "verify_model", AsyncMock(return_value=True)),
            patch.object(HarnessProvider, "generate", generate),
        ):
            async with (
                client_app(tmp_path, config=_config(tmp_path)) as (app, client, headers, _),
                asyncio.timeout(HANG_GUARD),
            ):
                state = cast("WebState", app.state.web)
                root = state.selected_session_id
                await state.history.add_message(root, Message(role="user", content="Existing root history"))
                other = (await client.post("/api/sessions/new", headers=headers, json={})).json()["session_id"]
                assert other != root
                started = await client.post(
                    "/api/messages",
                    headers=headers,
                    json={"session_id": root, "message_id": str(uuid4()), "prompt": "hold"},
                )
                assert started.status_code == 200, started.text
                await entered.wait()
                run = state.run_for(root)
                assert run is not None
                body = {
                    "session_id": root,
                    "message_id": str(uuid4()),
                    "prompt": "/audit follow-up",
                    "command": "skill",
                }
                admitted = await client.post("/api/messages", headers=headers, json=body)
                assert admitted.status_code == 200
                if change == "content":
                    skill_file(tmp_path, content="CURRENT_SKILL")
                elif change == "deleted":
                    path.unlink()
                else:
                    policy = state.harness.tool_settings
                    policy.save({"assistant": {"skill": False}}, policy.revision)
                retry = await client.post("/api/messages", headers=headers, json=body)
                assert retry.status_code == 200 and retry.json() == admitted.json()
                followup = await client.post(
                    "/api/messages",
                    headers=headers,
                    json={"session_id": root, "message_id": str(uuid4()), "prompt": "after skill"},
                )
                assert followup.status_code == 200, followup.text
                release.set()
                await run.task
                await idle_history(state, root, 4 if change == "content" else 3)
                assert prompts[0] == "hold" and prompts[-1] == "after skill"
                assert len(prompts) == (3 if change == "content" else 2)
                if change == "content":
                    assert "CURRENT_SKILL" in prompts[1] and "OLD_SKILL" not in prompts[1]
                else:
                    assert any(
                        isinstance(record, dict)
                        and record.get("event") == "error"
                        and "Skill command could not run" in str(record.get("message"))
                        for frame, _ in state.bus.ring
                        for record in [frame.get("record", {})]
                    )
                assert not await state.history.get_history(other)
                assert state.selected_session_id == other

    asyncio.run(scenario())
