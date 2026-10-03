"""Optional benchmark tests never discover a developer's saved configuration."""

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def private_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "user-data"))


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "requires_posix: exercises native POSIX tools or private file modes")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if os.name != "posix":
        skip = pytest.mark.skip(reason="Native benchmark execution requires POSIX")
        for item in items:
            if item.get_closest_marker("requires_posix") is not None:
                item.add_marker(skip)
