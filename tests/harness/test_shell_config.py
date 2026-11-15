"""Background shell capabilities are consumer policy, not model preferences."""

import json
from pathlib import Path

import pytest

from nagents.harness.config import HarnessConfig
from nagents.harness.config import ShellConfig
from nagents.harness.config import load_config


def test_shell_policy_defaults_and_json_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert HarnessConfig(workspace=tmp_path).shell.allow_background
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "shell": {
                    "allow_background": False,
                    "max_log_bytes": 2048,
                    "max_total_log_bytes": 8192,
                    "max_concurrent": 2,
                    "max_concurrent_per_owner": 1,
                },
            }
        )
    )
    config = load_config(tmp_path, config_path=path)
    assert not config.shell.allow_background
    assert config.shell.max_log_bytes == 2048
    assert config.shell.max_total_log_bytes == 8192
    assert config.shell.max_concurrent == 2
    assert config.shell.max_concurrent_per_owner == 1
    monkeypatch.setenv("NGN_SHELL_ALLOW_BACKGROUND", "false")
    assert not load_config(tmp_path).shell.allow_background


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_log_bytes": 0},
        {"max_total_log_bytes": 1024},
        {"max_concurrent": 0},
        {"max_concurrent_per_owner": 65},
        {"max_concurrent": 2, "max_concurrent_per_owner": 4},
    ],
)
def test_invalid_shell_resource_policy_is_rejected(tmp_path: Path, kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        ShellConfig().merged(kwargs)


@pytest.mark.parametrize(
    "value",
    [None, [], "false", {"unknown": 1}, {"allow_background": 0}, {"max_concurrent": "2"}, {"max_log_bytes": True}],
)
def test_strict_shell_json(tmp_path: Path, value: object) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"shell": value}))
    with pytest.raises(ValueError):
        load_config(tmp_path, config_path=path)


def test_shell_partial_merge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NGN_SHELL_ALLOW_BACKGROUND", "false")
    monkeypatch.setenv("NGN_SHELL_MAX_LOG_BYTES", "4096")
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"shell": {"max_concurrent": 5}}))
    config = load_config(tmp_path, config_path=path)
    assert not config.shell.allow_background
    assert config.shell.max_log_bytes == 4096
    assert config.shell.max_concurrent == 5


@pytest.mark.parametrize("key", ["allow_background_shell", "shell_max_log_bytes", "shell_max_concurrent"])
def test_flat_fields_rejected(tmp_path: Path, key: str) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({key: True}))
    with pytest.raises(ValueError, match="Unknown configuration fields"):
        load_config(tmp_path, config_path=path)


def test_shell_defaults_are_not_shared(tmp_path: Path) -> None:
    first = HarnessConfig(workspace=tmp_path)
    second = HarnessConfig(workspace=tmp_path)
    first.shell.allow_background = False
    assert second.shell.allow_background
