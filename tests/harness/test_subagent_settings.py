"""Subagent limit preferences are scoped, revisioned, and read without writes."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from typing import cast

import pytest

from nagents.harness.config import HarnessConfig
from nagents.harness.subagent_settings import LocalSubagentSettings
from nagents.harness.subagent_settings import SubagentLimits

if TYPE_CHECKING:
    from pathlib import Path


def settings_for(
    workspace: Path,
    data_dir: Path,
    *,
    config_paths: tuple[Path, ...] = (),
) -> LocalSubagentSettings:
    return LocalSubagentSettings(HarnessConfig(workspace=workspace, data_dir=data_dir, config_paths=config_paths))


@pytest.mark.asyncio
async def test_read_is_read_only_and_initially_uses_trusted_defaults(tmp_path: Path) -> None:
    config = settings_for(tmp_path / "workspace", tmp_path / "data")

    global_settings = await config.read("global")
    workspace_settings = await config.read("workspace")

    assert global_settings.limits == SubagentLimits(3, 8, 2)
    assert workspace_settings.limits == SubagentLimits(3, 8, 2)
    assert workspace_settings.effective == SubagentLimits(3, 8, 2)
    assert not config.path.exists(), "reading settings must not create the database"


@pytest.mark.asyncio
async def test_save_uses_revision_cas_and_stale_write_preserves_committed_value(tmp_path: Path) -> None:
    config = settings_for(tmp_path / "workspace", tmp_path / "data")
    initial = await config.read("workspace")
    saved = await config.save("workspace", initial.revision, SubagentLimits(5, 24, 4))

    with pytest.raises(ValueError, match="changed"):
        await config.save("workspace", initial.revision, SubagentLimits(9, 64, 6))

    current = await config.read("workspace")
    assert current.limits == SubagentLimits(5, 24, 4)
    assert current.revision == saved.revision
    assert current.revision != initial.revision


@pytest.mark.asyncio
async def test_global_inheritance_and_workspace_overrides_are_isolated(tmp_path: Path) -> None:
    data = tmp_path / "shared-data"
    first = settings_for(tmp_path / "one", data)
    second = settings_for(tmp_path / "two", data)

    global_initial = await first.read("global")
    await first.save("global", global_initial.revision, SubagentLimits(4, 32, 3))

    first_inherited = await first.read("workspace")
    second_inherited = await second.read("workspace")
    assert first_inherited.limits == SubagentLimits(4, 32, 3)
    assert second_inherited.limits == SubagentLimits(4, 32, 3)

    await first.save("workspace", first_inherited.revision, SubagentLimits(6, 48, 5))
    first_override = await first.read("workspace")
    second_still_inherited = await second.read("workspace")
    global_current = await first.read("global")

    assert first_override.limits == SubagentLimits(6, 48, 5)
    assert first_override.effective == SubagentLimits(6, 48, 5)
    assert second_still_inherited.limits == SubagentLimits(4, 32, 3)
    assert global_current.limits == SubagentLimits(4, 32, 3)


@pytest.mark.asyncio
async def test_trusted_config_change_invalidates_workspace_revision(tmp_path: Path) -> None:
    config_path = tmp_path / "trusted.json"
    config_path.write_text(json.dumps({"max_concurrent_subagents": 5, "unrelated": "ignored"}), encoding="utf-8")
    config = settings_for(tmp_path / "workspace", tmp_path / "data", config_paths=(config_path,))

    initial = await config.read("workspace")
    assert initial.limits == SubagentLimits(5, 8, 2)
    assert (await config.read("global")).limits == SubagentLimits(5, 8, 2)
    saved = await config.save("workspace", initial.revision, SubagentLimits(7, 20, 3))

    config_path.write_text(json.dumps({"max_concurrent_subagents": 6, "unrelated": "still ignored"}), encoding="utf-8")
    reloaded = await config.read("workspace")

    assert (await config.read("global")).limits == SubagentLimits(6, 8, 2)
    assert reloaded.limits == SubagentLimits(7, 20, 3)
    assert reloaded.revision != saved.revision
    with pytest.raises(ValueError, match="changed"):
        await config.save("workspace", saved.revision, SubagentLimits(8, 20, 3))
    assert (await config.read("workspace")).limits == SubagentLimits(7, 20, 3)


@pytest.mark.asyncio
async def test_final_harness_limit_overrides_keep_precedence_over_trusted_config_files(tmp_path: Path) -> None:
    config_path = tmp_path / "trusted.json"
    config_path.write_text(
        json.dumps(
            {
                "max_concurrent_subagents": 7,
                "max_subagent_executions": 64,
                "max_subagent_depth": 4,
            }
        ),
        encoding="utf-8",
    )
    config = HarnessConfig(
        workspace=tmp_path / "workspace",
        data_dir=tmp_path / "data",
        config_paths=(config_path,),
        max_concurrent_subagents=5,
        max_subagent_executions=15,
        max_subagent_depth=1,
    )

    settings = LocalSubagentSettings(config)
    snapshot = await settings.read("workspace")

    assert snapshot.limits == SubagentLimits(5, 15, 1)
    config_path.write_text(
        json.dumps(
            {
                "max_concurrent_subagents": 9,
                "max_subagent_executions": 80,
                "max_subagent_depth": 5,
            }
        ),
        encoding="utf-8",
    )
    assert (await settings.read("workspace")).limits == SubagentLimits(5, 15, 1)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_concurrent_subagents", 0),
        ("max_concurrent_subagents", 65),
        ("max_subagent_executions", 0),
        ("max_subagent_executions", 4097),
        ("max_subagent_depth", -1),
        ("max_subagent_depth", 9),
        ("max_concurrent_subagents", True),
        ("max_subagent_executions", "10"),
    ],
)
def test_limit_values_reject_wrong_types_and_out_of_range_values(field: str, value: object) -> None:
    with pytest.raises(ValueError, match=field):
        if field == "max_concurrent_subagents":
            SubagentLimits(max_concurrent_subagents=cast("int", value))
        elif field == "max_subagent_executions":
            SubagentLimits(max_subagent_executions=cast("int", value))
        else:
            SubagentLimits(max_subagent_depth=cast("int", value))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [("max_concurrent_subagents", 0), ("max_subagent_executions", 4097), ("max_subagent_depth", True)],
)
async def test_trusted_config_rejects_invalid_limit_values(tmp_path: Path, field: str, value: object) -> None:
    config_path = tmp_path / "trusted.json"
    config_path.write_text(json.dumps({field: value}), encoding="utf-8")
    config = settings_for(tmp_path / "workspace", tmp_path / "data", config_paths=(config_path,))

    with pytest.raises(ValueError, match=field):
        await config.read("global")


@pytest.mark.asyncio
async def test_invalid_scope_is_rejected_without_creating_a_database(tmp_path: Path) -> None:
    config = settings_for(tmp_path / "workspace", tmp_path / "data")

    with pytest.raises(ValueError, match="scope"):
        await config.read("untrusted-scope")  # type: ignore[arg-type]
    assert not config.path.exists()
