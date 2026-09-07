"""Keep tests away from a developer's real configuration and credentials."""

import os
from pathlib import Path

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "requires_posix: uses POSIX-only guarded filesystem, process groups, or protected OAuth storage",
    )


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if os.name == "posix":
        return
    skip = pytest.mark.skip(reason="Requires POSIX guarded filesystem, process groups, or protected OAuth storage")
    for item in items:
        if item.get_closest_marker("requires_posix") is not None:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def isolated_user_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Path.home() uses USERPROFILE rather than HOME on Windows, including XDG fallbacks.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "user-data"))
