"""Discovery must not import repository code before trust or under read-only ceilings."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from nagents.events import TextDoneEvent
from tests.support.providers import collect
from tests.support.providers import setup_harness

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.types import Message
    from tests.support.providers import FakeProvider


@pytest.mark.asyncio
@pytest.mark.parametrize("restriction", ["untrusted", "reviewer", "read_only", "ceiling", "trusted"])
async def test_import_side_effects_obey_trust_and_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, restriction: str
) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "project"
    home.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for root, label in ((home, "personal"), (workspace, "project")):
        directory = root / ".agents/tools"
        directory.mkdir(parents=True)
        (directory / f"{label}.py").write_text(
            "from pathlib import Path\nfrom nagents import tool\n"
            f"Path({str(tmp_path / (label + '-imported'))!r}).touch()\n"
            "@tool\ndef ping() -> str:\n    return 'pong'\n",
            encoding="utf-8",
        )

    async def script(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
        yield TextDoneEvent(text="done")

    harness, _ = setup_harness(workspace, monkeypatch, script)
    harness.config.trust_project = restriction != "untrusted"
    if restriction == "reviewer":
        harness.config.agent = "reviewer"
    elif restriction == "read_only":
        harness.config.read_only = True
    elif restriction == "ceiling":
        harness._permission_ceiling = "reviewer"
    try:
        await collect(harness)
        assert (tmp_path / "personal-imported").exists() == (restriction in {"untrusted", "trusted"})
        assert (tmp_path / "project-imported").exists() == (restriction == "trusted")
        names = harness.resources.current.tool_modules
        assert ("project" in names) == (restriction == "trusted")
        if restriction == "trusted":
            harness.config.trust_project = False
            await collect(harness)
            assert "project" not in harness.resources.current.tool_modules
            assert "personal" in harness.resources.current.tool_modules
    finally:
        await harness.close()


def test_home_workspace_requires_project_trust(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from nagents.harness.tools.discovery import discover_tool_sources

    monkeypatch.setenv("HOME", str(tmp_path))
    directory = tmp_path / ".agents/tools"
    directory.mkdir(parents=True)
    (directory / "sample.py").write_text("raise RuntimeError('must not import')\n", encoding="utf-8")
    assert not discover_tool_sources(tmp_path).modules
    assert len(discover_tool_sources(tmp_path, trust_project=True).modules) == 1
    assert not discover_tool_sources(tmp_path, trust_project=True, read_only=True).modules
