"""Keep tests away from a developer's real configuration and credentials."""

import os
from collections.abc import Iterator
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


@pytest.fixture
def workspace_discovery_only() -> Iterator[None]:
    """Avoid workspace instructions/skills while keeping real harness execution."""
    from unittest.mock import patch

    from nagents.harness.runtime import Harness
    from nagents.harness.tools import CodingTools

    with (
        patch.object(Harness, "load_project_instructions"),
        patch.object(CodingTools, "instructions", return_value=""),
        patch.object(CodingTools, "discover_skills"),
        patch.object(
            CodingTools, "directory", side_effect=OSError("Guarded workspace files are unavailable")
        ) as directory,
    ):
        yield
        directory.assert_not_called()
