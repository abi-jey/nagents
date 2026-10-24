"""Public native tools address retained agents only by short session identity."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import asdict
from typing import TYPE_CHECKING
from typing import TypeGuard

import pytest

from nagents.compaction import ModelCompaction
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.events import ToolResultEvent
from nagents.harness import runtime
from nagents.harness.providers import ProviderProfile
from nagents.harness.subagents import TaskInfo
from nagents.harness.types import TaskStarted
from tests.support.hang_guard import HANG_GUARD
from tests.support.providers import FakeProvider
from tests.support.providers import collect
from tests.support.providers import notifications
from tests.support.providers import setup_harness

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from collections.abc import Callable
    from pathlib import Path

    from nagents.events import Event
    from nagents.harness.agent_status import AgentStatusCollection
    from nagents.harness.agent_status import AgentStatusRecord
    from nagents.harness.agent_status import AgentStatusResult
    from nagents.harness.auth import OpenAIAuth
    from nagents.harness.config import HarnessConfig
    from nagents.types import Message

    Script = Callable[[FakeProvider, list[Message]], AsyncIterator[Event]]


pytestmark = pytest.mark.requires_posix


def is_status_record(value: AgentStatusResult) -> TypeGuard[AgentStatusRecord]:
    return "session_id" in value


def is_status_collection(value: AgentStatusResult) -> TypeGuard[AgentStatusCollection]:
    return "sessions" in value


def record_built_providers(
    monkeypatch: pytest.MonkeyPatch,
    providers: list[FakeProvider],
    script: Script,
) -> None:
    def build(_profile: ProviderProfile, config: HarnessConfig, _auth: OpenAIAuth) -> FakeProvider:
        provider = FakeProvider(config, len(providers), script)
        providers.append(provider)
        return provider

    monkeypatch.setattr(runtime, "build_provider", build)


def test_native_agent_tools_start_continue_and_report_the_same_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            yield TextDoneEvent(text=f"Child response from provider {provider.index}")

        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        record_built_providers(monkeypatch, providers, script)
        alternate = ProviderProfile(kind="openai", auth="api-key")
        harness.providers.providers["alternate"] = alternate
        harness.config.providers["alternate"] = alternate
        await harness.initialize()
        try:
            names = set(harness.agent.tool_registry.names())
            assert {"to_agent", "agent_status"} <= names
            assert not {"delegate", "subagent_status"} & names
            definitions = {tool.name: tool for tool in harness.agent.tool_registry.get_all()}
            native_arguments = definitions["to_agent"].parameters["properties"]
            status_arguments = definitions["agent_status"].parameters["properties"]
            assert {"prompt", "agent", "session_id", "title"} <= native_arguments.keys()
            assert not {"task_id", "name", "id"} & native_arguments.keys()
            assert "session_id" in status_arguments and "task_id" not in status_arguments

            harness.tasks.begin(harness.session_id)
            ack = await harness.tasks.to_agent(
                "Review the first version",
                agent="reviewer",
                title="Research",
                reasoning_effort="high",
                compact_trigger="messages",
                compact_messages=10000,
            )
            assert set(ack) == {"session_id", "status"}
            session_id = ack["session_id"]
            assert session_id == "s1" and ack["status"] == "running"
            initial = harness.tasks._lookup(session_id)
            storage_id = initial._storage_id
            original_title, original_model, original_provider = initial.title, initial.model, initial.provider
            original_options = harness.tasks._options[session_id]
            assert original_title == "Research" and initial.profile == "reviewer"
            await asyncio.wait_for(harness.tasks._workers[session_id], HANG_GUARD)
            first_history = await harness.task_history(session_id)
            initial_child = harness.tasks.child(session_id)
            initial_compaction = initial_child.agent.compaction_strategy
            assert initial_child.reasoning_effort() == "high"
            assert isinstance(initial_compaction, ModelCompaction) and initial_compaction.message_threshold == 10000

            providers_before_status = sum(len(provider.requests) for provider in providers)
            detail = await harness.agent_status(session_id)
            assert is_status_record(detail)
            assert detail["session_id"] == session_id and detail["title"] == "Research"
            assert detail["status"] == "completed"
            assert sum(len(provider.requests) for provider in providers) == providers_before_status
            listing = await harness.agent_status(limit=1)
            assert is_status_collection(listing)
            assert listing["sessions"][0]["session_id"] == session_id
            assert listing["more"] == 0

            original_history = [message.content for message in await harness.task_history(session_id)]
            baseline_used = harness.tasks._used
            invalid_options = (
                {"provider": "missing", "model": "unavailable-model"},
                {"model": " "},
                {"model": 123},
                {"provider": None},
                {"reasoning_effort": "arbitrary"},
                {"compact_trigger": "invalid"},
            )
            for invalid in invalid_options:
                with pytest.raises(ValueError):
                    await harness.tasks.to_agent(
                        "Invalid options must not change the accepted session",
                        session_id=session_id,
                        title="Must not be applied",
                        **invalid,
                    )
                assert harness.tasks._used == baseline_used
                assert harness.tasks._lookup(session_id).title == original_title
                assert harness.tasks._lookup(session_id).model == original_model
                assert harness.tasks._lookup(session_id).provider == original_provider
                assert harness.tasks._options[session_id] == original_options
                assert [message.content for message in await harness.task_history(session_id)] == original_history

            await harness.set_model("new-parent-model")
            continued = await harness.tasks.to_agent(
                "Review the second version", session_id=session_id, agent="", title=""
            )
            assert continued == {"session_id": session_id, "status": "running"}
            await asyncio.wait_for(harness.tasks._workers[session_id], HANG_GUARD)
            after_omitted = harness.tasks._lookup(session_id)
            assert after_omitted._storage_id == storage_id
            assert after_omitted.title == original_title and after_omitted.profile == "reviewer"
            assert after_omitted.model == original_model
            assert after_omitted.provider == original_provider
            assert after_omitted.reasoning_effort == "high"
            assert providers[-1].model == original_model
            continued_child = harness.tasks.child(session_id)
            continued_compaction = continued_child.agent.compaction_strategy
            assert continued_child.reasoning_effort() == "high"
            assert isinstance(continued_compaction, ModelCompaction) and continued_compaction.message_threshold == 10000
            continued_history = await harness.task_history(session_id)
            assert first_history and continued_history, (first_history, continued_history)
            assert len(continued_history) > len(first_history), (first_history, continued_history)

            renamed = await harness.tasks.to_agent(
                "Review the final version",
                session_id=session_id,
                title="  Updated research  ",
                provider="alternate",
                model="explicit-child-model",
                reasoning_effort="default",
                compact_trigger="off",
            )
            assert renamed == {"session_id": session_id, "status": "running"}
            await asyncio.wait_for(harness.tasks._workers[session_id], HANG_GUARD)
            final = harness.tasks._lookup(session_id)
            assert final.session_id == session_id and final._storage_id == storage_id
            assert final.title == "Updated research" and final.profile == "reviewer"
            assert final.model == "explicit-child-model" and final.provider == "alternate"
            assert providers[-1].model == "explicit-child-model"
            assert providers[-1].harness_config.provider == "alternate"
            assert final.reasoning_effort == ""
            final_child = harness.tasks.child(session_id)
            assert final_child.reasoning_effort() == "" and final_child.agent.compaction_strategy is None
            assert final.followups == 2
            history = await harness.task_history(session_id)
            assert [message.content for message in history if message.role == "user"] == [
                "Review the first version",
                "Review the second version",
                "Review the final version",
            ]
            with sqlite3.connect(harness.agent.session.db_path) as db:
                persisted = db.execute(
                    "SELECT role, content FROM v2_messages WHERE session_id = ? ORDER BY id", (storage_id,)
                ).fetchall()
            assert all(
                any(role == "user" and prompt in str(content) for role, content in persisted)
                for prompt in ("Review the first version", "Review the second version", "Review the final version")
            ), persisted
            assert len(harness.tasks.list()) == 1
        finally:
            await harness.tasks.end()
            await harness.close()

    asyncio.run(scenario())


def test_equal_titles_create_distinct_session_identities(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            yield TextDoneEvent(text="Complete")

        harness, _ = setup_harness(tmp_path, monkeypatch, script)
        await harness.initialize()
        try:
            harness.tasks.begin(harness.session_id)
            first = await harness.tasks.to_agent("First task", title="Same title")
            second = await harness.tasks.to_agent("Second task", title="Same title")
            assert first["status"] == second["status"] == "running"
            assert first["session_id"] != second["session_id"]
            assert {info.title for info in harness.tasks.list()} == {"Same title"}
            await asyncio.wait_for(
                asyncio.gather(
                    harness.tasks._workers[first["session_id"]], harness.tasks._workers[second["session_id"]]
                ),
                HANG_GUARD,
            )
            assert {info.session_id for info in harness.tasks.list()} == {first["session_id"], second["session_id"]}
        finally:
            await harness.tasks.end()
            await harness.close()

    asyncio.run(scenario())


def test_ack_persists_effective_identity_before_child_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        child_entered, release_child = asyncio.Event(), asyncio.Event()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            child_entered.set()
            await release_child.wait()
            yield TextDoneEvent(text="Complete")

        harness, providers = setup_harness(tmp_path, monkeypatch, script)
        record_built_providers(monkeypatch, providers, script)
        alternate = ProviderProfile(kind="openai", auth="api-key")
        harness.providers.providers["alternate"] = alternate
        harness.config.providers["alternate"] = alternate
        await harness.initialize()
        try:
            harness.tasks.begin(harness.session_id)
            ack = await harness.tasks.to_agent(
                "Use the alternate provider",
                agent="reviewer",
                title="Provider preflight",
                provider="alternate",
                model="accepted-model",
            )
            session_id = ack["session_id"]
            assert ack == {"session_id": session_id, "status": "running"}
            accepted = harness.tasks._lookup(session_id)
            assert (accepted.title, accepted.provider, accepted.model) == (
                "Provider preflight",
                "alternate",
                "accepted-model",
            )
            assert session_id == "s1"
            with sqlite3.connect(harness.agent.session.db_path) as db:
                row = db.execute(
                    "SELECT root_session_id, storage_id, title FROM nagents_agent_sessions WHERE sequence = 1"
                ).fetchone()
            assert row == (harness.session_id, accepted._storage_id, "Provider preflight")

            detail = await harness.agent_status(session_id)
            assert is_status_record(detail)
            assert (detail["title"], detail["provider"], detail["model"]) == (
                "Provider preflight",
                "alternate",
                "accepted-model",
            )
            assert not child_entered.is_set()
            await asyncio.wait_for(child_entered.wait(), HANG_GUARD)
            child_provider = providers[-1]
            assert child_provider.harness_config.provider == "alternate"
            assert child_provider.model == "accepted-model"
            release_child.set()
            await asyncio.wait_for(harness.tasks._workers[session_id], HANG_GUARD)
        finally:
            release_child.set()
            await harness.tasks.end()
            await harness.close()

    asyncio.run(scenario())


def test_invalid_foreign_cancelled_and_deleted_targets_fail_without_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if provider.index == 1:
                entered.set()
                await release.wait()
            yield TextDoneEvent(text="Child done")

        harness, _ = setup_harness(tmp_path, monkeypatch, script)
        await harness.initialize()
        try:
            harness.tasks.begin(harness.session_id)
            baseline_used = harness.tasks._used
            with pytest.raises(ValueError, match="short session_id"):
                await harness.tasks.to_agent("Invalid alias", session_id="not-an-id")
            with pytest.raises(ValueError, match="Unknown or unavailable"):
                await harness.tasks.to_agent("Unknown alias", session_id="szz")

            foreign = "szzz"
            harness.tasks._infos[foreign] = TaskInfo(
                foreign, "Foreign", root_session_id="ngn-foreign-root", _storage_id="ngn-foreign-child"
            )
            with pytest.raises(ValueError, match="Unknown or unavailable"):
                await harness.tasks.to_agent("Foreign alias", session_id=foreign)
            del harness.tasks._infos[foreign]
            assert harness.tasks._used == baseline_used
            assert harness.tasks.list() == []

            ack = await harness.tasks.to_agent("Wait before continuing")
            session_id = ack["session_id"]
            await asyncio.wait_for(entered.wait(), HANG_GUARD)
            used_after_start = harness.tasks._used
            with pytest.raises(RuntimeError, match="busy"):
                await harness.tasks.to_agent("Do not replace a running session", session_id=session_id)
            assert harness.tasks._used == used_after_start

            worker = harness.tasks._workers[session_id]
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            release.set()
            assert harness.tasks._lookup(session_id).status == "cancelled"
            with pytest.raises(RuntimeError, match="after cancellation"):
                await harness.tasks.to_agent("Do not replay cancelled work", session_id=session_id)
            assert harness.tasks._used == used_after_start

            # A completed but deleted transcript must never be recreated under its alias.
            replacement = await harness.tasks.to_agent("Complete before deleting")
            replacement_id = replacement["session_id"]
            await asyncio.wait_for(harness.tasks._workers[replacement_id], HANG_GUARD)
            replacement_info = harness.tasks._lookup(replacement_id)
            await harness.agent.session.delete_session(replacement_info._storage_id)
            before_deleted_admission = harness.tasks._used
            with pytest.raises(ValueError, match="deleted"):
                await harness.tasks.to_agent("Do not create replacement history", session_id=replacement_id)
            assert harness.tasks._used == before_deleted_admission
            assert not await harness.agent.session.session_exists(replacement_info._storage_id)
        finally:
            release.set()
            await harness.tasks.end()
            await harness.close()

    asyncio.run(scenario())


def test_provider_calls_reject_removed_tool_names_and_legacy_identity_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            if len(provider.requests) == 1:
                for call_id, name, arguments in (
                    ("legacy-to-agent", "to_agent", {"prompt": "must not start", "task_id": "legacy-id"}),
                    ("legacy-status", "agent_status", {"task_id": "legacy-id"}),
                    ("legacy-delegate", "delegate", {"prompt": "must not start"}),
                    ("legacy-status-tool", "subagent_status", {}),
                ):
                    yield ToolCallEvent(id=call_id, name=name, arguments=arguments)
            else:
                yield TextDoneEvent(text="Rejected every legacy call")

        harness, _ = setup_harness(tmp_path, monkeypatch, script)
        try:
            events = await collect(harness, "Reject legacy public contract")
            results = [
                event
                for event in events
                if isinstance(event, ToolResultEvent)
                and event.name in {"to_agent", "agent_status", "delegate", "subagent_status"}
            ]
            assert len(results) == 4
            assert all(event.error for event in results)
            assert harness.tasks.list() == [] and harness.tasks._used == 0
            assert [session.id for session in await harness.list_sessions()] == [harness.session_id]
        finally:
            await harness.close()

    asyncio.run(scenario())


def test_task_started_tracks_each_native_activation_and_keeps_origin_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        continued = False

        async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
            nonlocal continued
            if provider.index:
                yield TextDoneEvent(text=f"Child answer {provider.index}")
            elif len(provider.requests) == 1:
                yield ToolCallEvent(
                    id="initial-child-call",
                    name="to_agent",
                    arguments={"prompt": "Initial child work", "title": "Research"},
                )
            elif notifications(messages) and not continued:
                continued = True
                yield ToolCallEvent(
                    id="child-agent-followup",
                    name="to_agent",
                    arguments={"prompt": "Continue the same child", "session_id": "s1"},
                )
            else:
                yield TextDoneEvent(text="Parent finished")

        harness, _ = setup_harness(tmp_path, monkeypatch, script)
        try:
            events = await collect(harness, "Start and continue a child")
            started = [event for event in events if isinstance(event, TaskStarted)]
            assert len(started) == 2
            initial, continuation = started
            assert initial.activation_call_id == "initial-child-call"
            assert initial.activation_call_position == 0
            assert initial.activation_history_id and initial.activation_run_id
            assert initial.origin_history_id == initial.activation_history_id
            assert initial.origin_run_id == initial.activation_run_id

            assert continuation.session_id == initial.session_id == "s1"
            assert continuation.activation == initial.activation + 1
            assert continuation.activation_call_id == "child-agent-followup"
            assert continuation.activation_call_position == 0
            assert continuation.activation_history_id and continuation.activation_history_id != (
                initial.activation_history_id
            )
            assert continuation.activation_run_id == initial.activation_run_id
            assert (continuation.origin_history_id, continuation.origin_run_id) == (
                initial.origin_history_id,
                initial.origin_run_id,
            )

            private_storage_id = harness.tasks._lookup(initial.session_id)._storage_id
            assert all(private_storage_id not in str(asdict(event)) for event in started)
            host_events = [
                event async for event in harness.continue_task(initial.session_id, "Host owned continuation")
            ]
            host_started = next(event for event in host_events if isinstance(event, TaskStarted))
            assert host_started.trigger == "human"
            assert host_started.activation_history_id == ""
            assert host_started.activation_run_id == ""
            assert host_started.activation_call_id == ""
            assert host_started.activation_call_position == -1
            assert (host_started.origin_history_id, host_started.origin_run_id) == (
                initial.origin_history_id,
                initial.origin_run_id,
            )
            assert private_storage_id not in str(asdict(host_started))
        finally:
            await harness.close()

    asyncio.run(scenario())
