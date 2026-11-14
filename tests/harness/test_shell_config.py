"""Background shell capabilities are consumer policy, not model preferences."""

import json
from pathlib import Path

import pytest

from nagents.harness.config import HarnessConfig
from nagents.harness.config import load_config


def test_shell_policy_defaults_and_json_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert HarnessConfig(workspace=tmp_path).allow_background_shell
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "allow_background_shell": False,
                "shell_max_log_bytes": 2048,
                "shell_max_total_log_bytes": 8192,
                "shell_max_concurrent": 2,
                "shell_max_concurrent_per_owner": 1,
            }
        )
    )
    config = load_config(tmp_path, config_path=path)
    assert not config.allow_background_shell
    assert config.shell_max_log_bytes == 2048
    assert config.shell_max_total_log_bytes == 8192
    assert config.shell_max_concurrent == 2
    assert config.shell_max_concurrent_per_owner == 1
    monkeypatch.setenv("NGN_ALLOW_BACKGROUND_SHELL", "false")
    assert not load_config(tmp_path).allow_background_shell


@pytest.mark.parametrize(
    "kwargs",
    [
        {"shell_max_log_bytes": 0},
        {"shell_max_total_log_bytes": 1024},
        {"shell_max_concurrent": 0},
        {"shell_max_concurrent_per_owner": 65},
        {"shell_max_concurrent": 2, "shell_max_concurrent_per_owner": 4},
    ],
)
def test_invalid_shell_resource_policy_is_rejected(tmp_path: Path, kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        HarnessConfig(
            workspace=tmp_path,
            shell_max_log_bytes=kwargs.get("shell_max_log_bytes", 64 * 1024 * 1024),
            shell_max_total_log_bytes=kwargs.get("shell_max_total_log_bytes", 256 * 1024 * 1024),
            shell_max_concurrent=kwargs.get("shell_max_concurrent", 8),
            shell_max_concurrent_per_owner=kwargs.get("shell_max_concurrent_per_owner", 4),
        )
