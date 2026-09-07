"""Keep tests away from a developer's real configuration and credentials."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_user_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "user-data"))
