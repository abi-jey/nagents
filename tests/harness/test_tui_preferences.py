"""Performance preferences are scoped, strict, and never trust project code."""

import json
from pathlib import Path
from typing import Literal
from typing import cast

import pytest

from nagents.cli import _parser
from nagents.harness.config import HarnessConfig
from nagents.harness.config import load_config
from nagents.harness.config import save_tui_performance
from nagents.harness.private_store import ProtectedStoreError


def test_defaults_environment_json_and_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert HarnessConfig(workspace=tmp_path).tui_performance is False
    assert load_config(tmp_path).tui_performance is False
    monkeypatch.setenv("NGN_TUI_PERFORMANCE", "1")
    assert load_config(tmp_path).tui_performance is True
    path = tmp_path / "explicit.json"
    path.write_text('{"tui_performance": false}')
    assert load_config(tmp_path, path).tui_performance is False
    monkeypatch.setenv("NGN_TUI_PERFORMANCE", "yes")
    with pytest.raises(ValueError, match="NGN_TUI_PERFORMANCE"):
        load_config(tmp_path)
    for flag, enabled in (("--tui-performance", True), ("--no-tui-performance", False)):
        assert _parser().parse_args([flag]).tui_performance is enabled
        assert _parser().parse_args(["run", "hello", flag]).tui_performance is enabled
    assert not hasattr(_parser().parse_args([]), "tui_performance")


@pytest.mark.parametrize("value", [0, 1, "true", None, [], {}])
def test_rejects_non_boolean(tmp_path: Path, value: object) -> None:
    with pytest.raises(ValueError, match="tui_performance must be a boolean"):
        HarnessConfig(workspace=tmp_path, tui_performance=cast("bool", value))
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"tui_performance": value}))
    with pytest.raises(ValueError, match="tui_performance must be a boolean"):
        load_config(tmp_path, path)
    with pytest.raises(ValueError, match="tui_performance must be a boolean"):
        save_tui_performance(HarnessConfig(workspace=tmp_path), cast("bool", value), "global")


@pytest.mark.requires_posix
def test_scoped_persistence_preserves_settings_and_trust(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = load_config(workspace)
    global_path = save_tui_performance(config, True, "global")
    assert config.tui_performance is True
    assert load_config(workspace).tui_performance is True
    assert load_config(tmp_path / "other").tui_performance is True
    global_path.write_text('{"tui_performance": true, "unrelated": {"keep": [1, 2]}}')
    save_tui_performance(config, False, "global")
    assert json.loads(global_path.read_text())["unrelated"] == {"keep": [1, 2]}
    local_path = save_tui_performance(config, True, "local")
    assert local_path != global_path
    assert "workspaces" in local_path.parts
    assert load_config(workspace).tui_performance is True
    assert load_config(tmp_path / "other").tui_performance is False
    assert config.trust_project is False
    assert not (workspace / ".ngn").exists()
    assert local_path.stat().st_mode & 0o777 == 0o600
    assert local_path.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.requires_posix
@pytest.mark.parametrize("payload", ['{"tui_performance": 1}', "[]", "not json"])
def test_malformed_preferences_are_not_overwritten(tmp_path: Path, payload: str) -> None:
    config = HarnessConfig(workspace=tmp_path)
    path = save_tui_performance(config, False, "global")
    path.write_text(payload)
    with pytest.raises(ProtectedStoreError):
        save_tui_performance(config, True, "global")
    assert path.read_text() == payload
    assert config.tui_performance is False
    with pytest.raises(ProtectedStoreError):
        load_config(tmp_path)


@pytest.mark.requires_posix
def test_preferences_override_defaults_without_loading_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / ".ngn"
    project.mkdir()
    project_config = project / "config.json"
    project_config.write_text('{"tui_performance": false, "plugins": ["untrusted.py:setup"]}')
    config = HarnessConfig(workspace=tmp_path)
    save_tui_performance(config, True, "local")
    monkeypatch.setenv("NGN_TUI_PERFORMANCE", "false")
    with pytest.warns(UserWarning, match="Ignoring untrusted project config"):
        loaded = load_config(tmp_path)
    assert loaded.tui_performance is True
    assert loaded.plugins == ()
    assert loaded.trust_project is False
    assert "untrusted.py" in project_config.read_text()
    assert project_config not in loaded.config_paths


@pytest.mark.requires_posix
def test_symlink_preference_refused(tmp_path: Path) -> None:
    config = HarnessConfig(workspace=tmp_path)
    path = save_tui_performance(config, False, "global")
    path.unlink()
    target = tmp_path / "unrelated.json"
    target.write_text("{}")
    path.symlink_to(target)
    with pytest.raises((ProtectedStoreError, OSError)):
        save_tui_performance(config, True, "global")
    assert target.read_text() == "{}"


@pytest.mark.requires_posix
@pytest.mark.parametrize("scope", ["global", "local"])
def test_existing_standard_directories_keep_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    from nagents.harness.providers.registry import ScopedProviderRegistryStore

    root = tmp_path / "ordinary-config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root))
    config = HarnessConfig(workspace=tmp_path)
    store = ScopedProviderRegistryStore(config.workspace)
    directory = store.model_store("global" if scope == "global" else "workspace").path.parent
    directory.mkdir(parents=True)
    paths = [directory, *(parent for parent in directory.parents if parent.is_relative_to(root))]
    for path in paths:
        path.chmod(0o755)
    before = {path: path.stat().st_mode for path in paths}
    preference = directory / "tui.json"
    preference.write_text('{"unrelated": "preserved"}')
    preference.chmod(0o644)
    save_tui_performance(config, True, cast("Literal['global', 'local']", scope))
    assert load_config(tmp_path).tui_performance is True
    assert json.loads(preference.read_text())["unrelated"] == "preserved"
    assert {path: path.stat().st_mode for path in paths} == before


def test_nonsecret_store_round_trip(tmp_path: Path) -> None:
    from nagents.harness.tui_preferences import TuiPreferenceStore

    path = tmp_path / "settings" / "tui.json"
    store = TuiPreferenceStore(path)
    assert store.load() == {}
    store.save_performance(True)
    assert store.load() == {"tui_performance": True}
    store.save_performance(False)
    assert store.load() == {"tui_performance": False}


@pytest.mark.requires_posix
@pytest.mark.parametrize("kind", ["directory", "lock", "hardlink"])
def test_unsafe_preference_paths_refused(tmp_path: Path, kind: str) -> None:
    from nagents.harness.tui_preferences import TuiPreferenceStore

    directory = tmp_path / "settings"
    directory.mkdir()
    target = tmp_path / "target"
    if kind == "directory":
        directory.rmdir()
        target.mkdir()
        directory.symlink_to(target, target_is_directory=True)
    else:
        target.write_text("{}")
        if kind == "lock":
            (directory / ".tui.lock").symlink_to(target)
        else:
            (directory / "tui.json").hardlink_to(target)
    with pytest.raises(ProtectedStoreError):
        TuiPreferenceStore(directory / "tui.json").save_performance(True)
