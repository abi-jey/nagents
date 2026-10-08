"""Missing reads offer bounded recovery guidance without discovering or selecting files."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from nagents.events import TextDoneEvent
from nagents.types import ToolCall
from tests.support.providers import setup_harness

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from nagents.events import Event
    from nagents.types import Message
    from tests.support.providers import FakeProvider

pytestmark = pytest.mark.requires_posix


async def unused_provider(provider: FakeProvider, messages: list[Message]) -> AsyncIterator[Event]:
    yield TextDoneEvent(text="Unused")


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["config.yaml", "absent/config.yaml"])
async def test_missing_read_guides_explicit_discovery_without_scanning_or_selecting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    (tmp_path / "config.yml").write_text("expected content")
    (tmp_path / ".gitignore").write_text("ignored.yml\n")
    (tmp_path / "ignored.yml").write_text("hidden")
    harness, _ = setup_harness(tmp_path, monkeypatch, unused_provider)
    try:
        await harness.initialize()

        def unexpected_walk(*args: object, **kwargs: object) -> None:
            pytest.fail("Missing reads must not scan the workspace")

        with monkeypatch.context() as patch:
            patch.setattr(harness.tools, "walk", unexpected_walk)
            result = await harness.agent.tool_executor.execute(ToolCall("missing", "read_file", {"path": missing}))
        assert result.error
        assert "list_files" in result.error and "existing ancestor" in result.error
        assert "broaden" in result.error and "extensions" in result.error
        assert "No such file or directory" in result.error
        assert "ask the user only if ambiguity remains" in result.error
        with pytest.raises(FileNotFoundError) as original:
            harness.tools.snapshot(missing)
        with pytest.raises(FileNotFoundError) as guided:
            await harness.tools.read_file(missing)
        assert guided.value.errno == original.value.errno
        assert guided.value.filename == original.value.filename
        assert str(guided.value).startswith(f"[Errno {original.value.errno}] {original.value.strerror}")
        assert "config.yml" not in result.error and "ignored.yml" not in result.error
        assert not harness.tools.read_hashes
        discovery = await harness.tools.find("config.*")
        assert discovery["paths"] == ["config.yml"]
        read = await harness.tools.read_file("config.yml")
        assert read["content"] == "1: expected content"
    finally:
        await harness.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["../missing.txt", ".env/missing.txt", "credentials.json", "linked/missing.txt"])
async def test_recovery_guidance_does_not_replace_protected_path_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    (tmp_path / "linked").symlink_to(tmp_path, target_is_directory=True)
    harness, _ = setup_harness(tmp_path, monkeypatch, unused_provider)
    try:
        await harness.initialize()
        with pytest.raises(PermissionError) as error:
            await harness.tools.read_file(path)
        assert "broaden" not in str(error.value)
        assert not harness.tools.read_hashes
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_active_descriptions_distinguish_existing_reads_from_new_file_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, _ = setup_harness(tmp_path, monkeypatch, unused_provider)
    try:
        await harness.initialize()
        read = harness.agent.tool_registry.get("read_file")
        find = harness.agent.tool_registry.get("find")
        assert read is not None and find is not None
        assert "new file does not require a preliminary read" in read.description
        assert "existing-file inspection or editing" in find.description
        assert "read_file directly when a path is supplied" in find.description
        assert "new target file does not require a preliminary read" in find.description
        assert "broaden the glob" in find.description
    finally:
        await harness.close()
