"""Bounded public projections over durable child activation evidence."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from typing import TYPE_CHECKING

import pytest

from nagents.events import TextDoneEvent
from nagents.harness.agents.subagents import TaskInfo
from nagents.harness.session_identity import SessionIdentityStore
from nagents.types import Message
from tests.web.execution.test_web_wakeups import scheduled_app

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.types import Message as HarnessMessage
    from tests.support.providers import FakeProvider


@pytest.mark.requires_posix
def test_projection_bounds_large_history_and_respects_compaction_and_actor_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async def idle_script(_provider: FakeProvider, _messages: list[HarnessMessage]) -> AsyncIterator[Event]:
            yield TextDoneEvent(text="unused")

        async with scheduled_app(tmp_path, monkeypatch, idle_script) as (_, _, _, state, _, _):
            root = state.selected_session_id
            await state.tasks.store.initialize()
            identities = SessionIdentityStore(state.history.db_path)
            oversized_prompt = "P" * 16_000
            oversized_result = "R" * 12_000
            summaries: list[tuple[str, str, str, str]] = []
            activation_rows: list[tuple[str, int, str, dict[str, object], dict[str, object]]] = []

            # 64 child identities with nine lifecycle records each exercise the
            # usual retention shape while making unbounded projections exceed
            # the intended response budget by several megabytes.
            for index in range(64):
                identity = await identities.reserve(root, "", f"Agent {index}")
                storage_id = identity.storage_id
                await state.history.get_or_create_session(storage_id, "harness")
                old_history = str(
                    await state.history.add_message(root, Message(role="user", content=f"before compaction {index}"))
                )
                summaries.append((identity.session_id, storage_id, old_history, ""))

            # Invocation anchors belong to the invoking parent. Insert every
            # pre-boundary row before post-boundary rows, then set the actual
            # session compaction marker consumed by the default projection.
            boundary = str(
                await state.history.add_message(
                    root, Message(role="compaction_summary", content="Older agent launch history was compacted.")
                )
            )
            await state.history.store._transaction(
                lambda db: db.execute(
                    "UPDATE v2_sessions SET compacted_at_message_id = ? WHERE id = ?",
                    (int(boundary), root),
                )
            )
            for index, summary in enumerate(summaries):
                session_id, storage_id, old_history, _ = summary
                visible_history = str(
                    await state.history.add_message(root, Message(role="user", content=f"after compaction {index}"))
                )
                summaries[index] = (session_id, storage_id, old_history, visible_history)

                for activation in range(9):
                    anchor = old_history if activation < 5 else visible_history
                    started = {
                        "session_id": session_id,
                        "title": f"Agent {index}",
                        "root_session_id": root,
                        "parent_session_id": "",
                        "activation": activation,
                        "activation_history_id": anchor,
                        "prompt": oversized_prompt,
                        "status": "running",
                        "timestamp": float(activation + 1),
                    }
                    completed = {
                        "status": "completed",
                        "result": oversized_result,
                        "timestamp": float(activation + 2),
                    }
                    activation_rows.append((session_id, activation, anchor, started, completed))
            await state.tasks.store.db._transaction(
                lambda db: db.executemany(
                    "INSERT INTO ngn_web_agent_activations "
                    "(root, session_id, activation, epoch, started, completed) VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (
                            root,
                            session_id,
                            activation,
                            state.tasks.store.epoch,
                            json.dumps(started),
                            json.dumps(completed),
                        )
                        for session_id, activation, _anchor, started, completed in activation_rows
                    ],
                )
            )

            visible_ids = frozenset(item[3] for item in summaries)
            all_projected, all_omitted = await state.tasks.store.activation_projection(
                root,
                history_ids=frozenset({*(item[2] for item in summaries), *visible_ids}),
                parent_session_id="",
            )
            assert all_omitted == 0
            assert len(all_projected) == 64 * 9
            assert len(json.dumps(all_projected, ensure_ascii=False).encode("utf-8")) <= 2 * 1024 * 1024

            # A real snapshot includes full current task metadata as well as
            # the bounded history manifest. Both fit the browser's frame cap.
            for alias, storage_id, _old, _visible in summaries:
                latest = TaskInfo(
                    alias,
                    "Current title",
                    root_session_id=root,
                    _storage_id=storage_id,
                    activation=8,
                    status="completed",
                    prompt=oversized_prompt,
                    result=oversized_result,
                )
                await state.tasks.store.save(root, asdict(latest), storage_id)
            await state.history.store._transaction(
                lambda db: db.execute("UPDATE v2_sessions SET compacted_at_message_id = NULL WHERE id = ?", (root,))
            )
            cold = await state.snapshot(root)
            frame = json.dumps({"type": "snapshot", "session_id": root, "epoch": "test", "cursor": 0, "snapshot": cold})
            assert len(frame.encode("ascii")) < 8 * 1024 * 1024
            assert cold["retained_activations_omitted"] == 0
            cold_records = cold["retained_activations"]
            assert isinstance(cold_records, list)
            assert len(cold_records) == 576

            def authority(item: object) -> tuple[object, ...]:
                assert isinstance(item, dict)
                return tuple(item.get(field) for field in ("session_id", "activation", "status", "profile", "model"))

            assert [authority(item) for item in cold_records] == [authority(item) for item in all_projected]
            assert all(storage_id not in frame for _alias, storage_id, _old, _visible in summaries)
            await state.history.store._transaction(
                lambda db: db.execute(
                    "UPDATE v2_sessions SET compacted_at_message_id = ? WHERE id = ?", (int(boundary), root)
                )
            )

            default_projected, default_omitted = await state.tasks.store.activation_projection(
                root, parent_session_id=""
            )
            assert default_omitted == 0
            assert len(default_projected) == 64 * 4
            assert all(item["activation_history_id"] in visible_ids for item in default_projected)

            projected, omitted = await state.tasks.store.activation_projection(
                root, history_ids=visible_ids, parent_session_id=""
            )
            assert omitted == 0
            assert len(projected) == 64 * 4
            assert all(item["activation_history_id"] in visible_ids for item in projected)
            for item in projected:
                projected_activation = item["activation"]
                assert type(projected_activation) is int and projected_activation >= 5
            assert all(item["details_truncated"] is True for item in projected)
            assert all(len(str(item["prompt"]).encode("utf-8")) <= 512 for item in projected)
            assert all(len(str(item["result"]).encode("utf-8")) <= 2048 for item in projected)
            assert len(json.dumps(projected, ensure_ascii=False).encode("utf-8")) <= 2 * 1024 * 1024

            # Public projection is compact only: complete accepted content stays
            # in the immutable started/completed journal records.
            first_alias = summaries[0][0]
            raw = await state.tasks.store.db._transaction(
                lambda db: db.execute(
                    "SELECT started, completed FROM ngn_web_agent_activations "
                    "WHERE root = ? AND session_id = ? AND activation = 5",
                    (root, first_alias),
                ).fetchone()
            )
            assert raw is not None
            started = json.loads(raw[0])
            completed = json.loads(raw[1])
            assert started["prompt"] == oversized_prompt
            assert completed["result"] == oversized_result
            assert len(str(projected[0]["prompt"]).encode("utf-8")) < len(oversized_prompt.encode("utf-8"))

            # A parent's projection cannot expose a nested actor's records even
            # if its visible-history set includes both actors' history rows.
            parent = await identities.reserve(root, "", "Parent actor")
            child = await identities.reserve(root, parent.session_id, "Nested actor")
            await state.history.get_or_create_session(parent.storage_id, "harness")
            await state.history.get_or_create_session(child.storage_id, "harness")
            for identity, parent_alias, title in (
                (parent, "", "Parent actor"),
                (child, parent.session_id, "Nested actor"),
            ):
                await state.tasks.store.save(
                    root,
                    asdict(
                        TaskInfo(
                            identity.session_id,
                            title,
                            root_session_id=root,
                            parent_session_id=parent_alias,
                            _storage_id=identity.storage_id,
                            status="completed",
                        )
                    ),
                    identity.storage_id,
                )
            for identity, parent_alias, label in (
                (parent, "", "Parent actor"),
                (child, parent.session_id, "Nested actor"),
            ):
                prompt = "🙂" * 300 if identity is parent else label
                history_id = str(
                    await state.history.add_message(
                        root if not parent_alias else parent.storage_id,
                        Message(role="user", content=label),
                    )
                )
                await state.tasks.store.record_activation(
                    root,
                    {
                        "event": "task_started",
                        "root_session_id": root,
                        "session_id": identity.session_id,
                        "parent_session_id": parent_alias,
                        "activation": 0,
                        "title": label,
                        "status": "running",
                        "prompt": prompt,
                        "activation_history_id": history_id,
                        "timestamp": 1.0,
                    },
                )
            both_histories = frozenset(
                str(item[0])
                for item in await state.history.store._transaction(
                    lambda db: db.execute(
                        "SELECT json_extract(started, '$.activation_history_id') "
                        "FROM ngn_web_agent_activations WHERE root = ? "
                        "AND session_id IN (?, ?) ORDER BY sequence",
                        (root, parent.session_id, child.session_id),
                    ).fetchall()
                )
            )
            parent_rows, parent_omitted = await state.tasks.store.activation_projection(
                root, history_ids=both_histories, parent_session_id=""
            )
            child_rows, child_omitted = await state.tasks.store.activation_projection(
                root, history_ids=both_histories, parent_session_id=parent.session_id
            )
            assert parent_omitted == child_omitted == 0
            assert sum(item["title"] == "Parent actor" for item in parent_rows) == 1
            assert not any(item["title"] == "Nested actor" for item in parent_rows)
            projected_parent = next(item for item in parent_rows if item["title"] == "Parent actor")
            assert len(str(projected_parent["prompt"]).encode("utf-8")) <= 512
            assert "�" not in str(projected_parent["prompt"])
            assert projected_parent["details_truncated"] is True
            assert [item["title"] for item in child_rows] == ["Nested actor"]
            default_tree, default_omitted = await state.tasks.store.activation_projection(root)
            assert default_omitted == 0
            assert sum(item["title"] == "Parent actor" for item in default_tree) == 1
            assert sum(item["title"] == "Nested actor" for item in default_tree) == 1
            parent_detail = await state.tasks.detail(root, parent.session_id)
            nested_records = parent_detail["retained_activations"]
            assert isinstance(nested_records, list)
            assert [item["session_id"] for item in nested_records] == [child.session_id]
            assert parent_detail["retained_activations_omitted"] == 0
            assert parent.storage_id not in json.dumps(parent_detail)
            assert child.storage_id not in json.dumps(parent_detail)

    asyncio.run(scenario())


@pytest.mark.requires_posix
def test_parent_response_identity_roundtrips_in_completed_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async def idle_script(_provider: FakeProvider, _messages: list[HarnessMessage]) -> AsyncIterator[Event]:
            yield TextDoneEvent(text="unused")

        async with scheduled_app(tmp_path, monkeypatch, idle_script) as (_, _, _, state, _, _):
            root = state.selected_session_id
            anchor = str(await state.history.add_message(root, Message(role="user", content="Start child")))
            started = {
                "event": "task_started",
                "root_session_id": root,
                "session_id": "s1",
                "parent_session_id": "",
                "activation": 0,
                "title": "Child",
                "status": "running",
                "activation_history_id": anchor,
                "timestamp": 1.0,
            }
            completed = {
                "event": "task_completed",
                "root_session_id": root,
                "session_id": "s1",
                "parent_session_id": "",
                "activation": 0,
                "title": "Child",
                "status": "completed",
                "parent_response_id": "active-parent-response",
                "parent_response_session_id": root,
                "completed_at": 2.0,
                "timestamp": 2.0,
            }
            await state.tasks.store.record_activation(root, started)
            await state.tasks.store.record_activation(root, completed)

            rows, omitted = await state.tasks.store.activation_projection(
                root, history_ids=frozenset({anchor}), parent_session_id=""
            )
            assert omitted == 0 and len(rows) == 1
            assert rows[0]["parent_response_id"] == "active-parent-response"
            assert rows[0]["parent_response_session_id"] == root
            assert rows[0]["completed_at"] == 2.0

    asyncio.run(scenario())


@pytest.mark.requires_posix
def test_projection_reports_exceptional_omissions_explicitly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        async def idle_script(_provider: FakeProvider, _messages: list[HarnessMessage]) -> AsyncIterator[Event]:
            yield TextDoneEvent(text="unused")

        async with scheduled_app(tmp_path, monkeypatch, idle_script) as (_, _, _, state, _, _):
            root = state.selected_session_id
            await state.tasks.store.initialize()
            identity = await SessionIdentityStore(state.history.db_path).reserve(root, "", "Many activations")
            await state.history.get_or_create_session(identity.storage_id, "harness")
            history_id = str(
                await state.history.add_message(root, Message(role="user", content="visible activation history"))
            )
            activation_rows: list[tuple[str, int, str, dict[str, object], dict[str, object]]] = []
            for activation in range(1_050):
                started = {
                    "session_id": identity.session_id,
                    "root_session_id": root,
                    "parent_session_id": "",
                    "activation": activation,
                    "title": "Many activations",
                    "status": "running",
                    "activation_history_id": history_id,
                    "prompt": f"prompt {activation}",
                    "timestamp": 1.0,
                }
                completed = {"status": "completed", "result": f"result {activation}", "timestamp": 2.0}
                activation_rows.append((identity.session_id, activation, history_id, started, completed))
            await state.tasks.store.db._transaction(
                lambda db: db.executemany(
                    "INSERT INTO ngn_web_agent_activations "
                    "(root, session_id, activation, epoch, started, completed) VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (
                            root,
                            session_id,
                            activation,
                            state.tasks.store.epoch,
                            json.dumps(started),
                            json.dumps(completed),
                        )
                        for session_id, activation, _anchor, started, completed in activation_rows
                    ],
                )
            )

            projected, omitted = await state.tasks.store.activation_projection(
                root, history_ids=frozenset({history_id}), parent_session_id=""
            )
            assert len(projected) <= 1_024
            assert omitted == 1_050 - len(projected)
            assert omitted > 0
            assert all(item["activation_history_id"] == history_id for item in projected)

    asyncio.run(scenario())
