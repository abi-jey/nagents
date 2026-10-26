"""The task poll refreshes one primary catalog and still observes user edits."""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING
from typing import cast

import pytest

from nagents.harness import Harness
from nagents.harness.config import HarnessConfig
from tests.support.web import client_app

if TYPE_CHECKING:
    from pathlib import Path

    from pytest import MonkeyPatch

pytestmark = pytest.mark.requires_posix


def _mini(description: str) -> str:
    return f"---\nname: mini\ndescription: {description}\ntools: [read_file]\n---\n"


def test_task_index_refreshes_primary_once_and_observes_same_mtime_definition_edit(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    async def scenario() -> None:
        source = tmp_path / ".ngn/agents/mini/agent.md"
        source.parent.mkdir(parents=True)
        original, edited = _mini("Mini A"), _mini("Mini B")
        assert len(original.encode()) == len(edited.encode())
        source.write_text(original, encoding="utf-8")
        config = HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "data", demo=True, trust_project=True)
        async with client_app(tmp_path, controlled=False, config=config) as (app, client, headers, harnesses):
            state = app.state.web
            harness = cast("Harness", state.harness)
            calls = 0
            real_refresh = harness.refresh_agent_definitions

            def counted_refresh() -> bool:
                nonlocal calls
                calls += 1
                return real_refresh()

            monkeypatch.setattr(harness, "refresh_agent_definitions", counted_refresh)
            path = f"/api/sessions/{state.selected_session_id}/tasks"
            first = await client.get(path, headers=headers)
            assert first.status_code == 200
            first_body = first.json()
            first_mini = next(profile for profile in first_body["profiles"] if profile["id"] == "mini")
            assert first_mini["description"] == "Mini A"
            assert calls == 1, "Tasks.index and SessionAgents.view must not refresh the same primary twice"

            original_stat = source.stat()
            source.write_text(edited, encoding="utf-8")
            os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
            after_stat = source.stat()
            assert after_stat.st_size == original_stat.st_size
            assert after_stat.st_mtime_ns == original_stat.st_mtime_ns

            second = await client.get(path, headers=headers)
            assert second.status_code == 200
            second_body = second.json()
            second_mini = next(profile for profile in second_body["profiles"] if profile["id"] == "mini")
            assert second_mini["description"] == "Mini B"
            assert second_body["profile_revision"] != first_body["profile_revision"]
            assert calls == 2
            assert harnesses[0] is harness

    asyncio.run(scenario())


def test_view_refreshes_a_distinct_session_runtime_after_primary_was_refreshed(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    async def scenario() -> None:
        source = tmp_path / ".ngn/agents/mini/agent.md"
        source.parent.mkdir(parents=True)
        source.write_text(_mini("Mini"), encoding="utf-8")
        config = HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "data", demo=True, trust_project=True)
        async with client_app(tmp_path, controlled=False, config=config) as (app, _client, _headers, harnesses):
            state = app.state.web
            primary = cast("Harness", state.harness)
            alternate = Harness(primary.clone_configuration(), allow_subagents=False)
            primary_calls = 0
            alternate_calls = 0
            primary_refresh = primary.refresh_agent_definitions
            alternate_refresh = alternate.refresh_agent_definitions

            def count_primary() -> bool:
                nonlocal primary_calls
                primary_calls += 1
                return primary_refresh()

            def count_alternate() -> bool:
                nonlocal alternate_calls
                alternate_calls += 1
                return alternate_refresh()

            monkeypatch.setattr(primary, "refresh_agent_definitions", count_primary)
            monkeypatch.setattr(alternate, "refresh_agent_definitions", count_alternate)
            monkeypatch.setattr(state, "harness_for", lambda _session_id: alternate)
            try:
                async with state.session_agents.view(state.selected_session_id, refresh_catalog=False) as owner:
                    assert owner is alternate
                assert primary_calls == 0
                assert alternate_calls == 1
            finally:
                await alternate.close()
            assert harnesses[0] is primary

    asyncio.run(scenario())
