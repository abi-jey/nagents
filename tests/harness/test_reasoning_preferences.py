"""Effort preferences resolve without mutating accepted child activations."""

import asyncio
import json
from pathlib import Path

import pytest

from nagents.harness import Harness
from nagents.harness.config import AgentProfile
from nagents.harness.config import HarnessConfig
from nagents.harness.config import load_config
from nagents.harness.reasoning import ReasoningPreference
from nagents.harness.subagents import TaskInfo
from nagents.harness.task_activity import TaskOptions


def test_trusted_effort_config_and_native_profile_clear(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NGN_REASONING_EFFORT", "medium")
    assert load_config(tmp_path).reasoning_effort == "medium"
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"reasoning_effort": "high", "profiles": {"quick": {"reasoning_effort": "default"}}}))
    config = load_config(tmp_path, path)
    assert config.reasoning_effort == "high"
    assert config.profile("quick").reasoning_effort == "default"
    path.write_text('{"reasoning_effort":"arbitrary"}')
    with pytest.raises(ValueError, match="reasoning_effort"):
        load_config(tmp_path, path)


@pytest.mark.parametrize("profile_effort,expected", [("", "high"), ("low", "low"), ("default", "")])
def test_profile_precedence_and_summary_setting_are_independent(
    tmp_path: Path, profile_effort: ReasoningPreference, expected: str
) -> None:
    async def check() -> None:
        harness = Harness(
            HarnessConfig(
                workspace=tmp_path,
                demo=True,
                agent="worker",
                reasoning_effort="high",
                reasoning_summaries=False,
                profiles={"worker": AgentProfile(reasoning_effort=profile_effort)},
            )
        )
        try:
            generation = harness.generation_config()
            assert harness.reasoning_effort() == expected
            if expected:
                assert generation is not None and generation.reasoning_effort == expected
                assert generation.reasoning_summary == "off"
            else:
                assert generation is None
        finally:
            await harness.close()

    asyncio.run(check())


@pytest.mark.parametrize("next_effort,expected", [("", "high"), ("low", "low"), ("default", "")])
def test_retained_child_effort_changes_only_on_next_activation(
    tmp_path: Path, next_effort: ReasoningPreference, expected: str
) -> None:
    async def check() -> None:
        parent = Harness(HarnessConfig(workspace=tmp_path, demo=True, reasoning_effort="high"))
        parent.session_id = "ngn-root"
        info = TaskInfo("child", "Child", session_id=parent.session_id, child_session_id="ngn-child")
        parent.tasks._infos[info.id] = info
        child = parent.tasks._create_child("assistant", info)
        parent.tasks._children[info.id] = child
        resumed: list[Harness] = []
        try:
            pending = parent.tasks.configure_task(info.id, TaskOptions(reasoning_effort=next_effort))
            assert pending.next_settings["reasoning_effort"] == next_effort
            assert child.reasoning_effort() == "high"
            accepted_generation = child.generation_config()
            assert accepted_generation is not None and accepted_generation.reasoning_effort == "high"
            # A later root preference must not affect this retained child's accepted snapshot.
            parent.config.reasoning_effort = "minimal"
            parent.tasks._options[info.id] = parent.tasks._next_options.pop(info.id)
            next_child = parent.tasks._create_child("assistant", info, continuing=True)
            resumed.append(next_child)
            assert next_child.reasoning_effort() == expected
            assert child.reasoning_effort() == "high"
        finally:
            for runtime in resumed:
                await runtime.close()
            await child.close()
            await parent.close()

    asyncio.run(check())
