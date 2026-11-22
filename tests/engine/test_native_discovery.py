"""ABI3 integration: opt-in Rust scanning, Python guards and fallback."""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

import pytest

from nagents.harness import Harness
from nagents.harness.config import HarnessConfig
from nagents.harness.tools import coding

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.requires_posix


def _enable_native(monkeypatch: pytest.MonkeyPatch) -> None:
    if not hasattr(getattr(coding, "nagents_native", None), "workspace_search"):
        if os.environ.get("NGN_REQUIRE_NATIVE_DISCOVERY") == "1":
            pytest.fail("The source ABI3 discovery wheel is required for this gate.")
        pytest.skip("The optional ABI3 discovery wheel is not installed.")
    monkeypatch.setenv("NGN_NATIVE_DISCOVERY", "1")


def test_native_scanning_observes_nested_gitignore_and_python_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_native(monkeypatch)
    state = tmp_path / "private-state"
    state.mkdir()
    (state / "history.txt").write_text("not readable\n")
    tools = Harness(HarnessConfig(workspace=tmp_path, data_dir=state, demo=True)).tools
    (tmp_path / "included").mkdir()
    (tmp_path / "ignored").mkdir()
    (tmp_path / ".gitignore").write_text("ignored/\n*.log\n\\#ignored.txt\n")
    (tmp_path / "included/.gitignore").write_text("!keep.log\n")
    (tmp_path / "included/keep.log").write_text("found below\n")
    (tmp_path / "included/drop.log").write_text("must not expose\n")
    (tmp_path / "ignored/.gitignore").write_text("!hidden.txt\n")
    (tmp_path / "ignored/hidden.txt").write_text("must not expose\n")
    (tmp_path / "#ignored.txt").write_text("must not expose\n")
    (tmp_path / ".ENV.fixture").write_text("secret\n")
    (tmp_path / "link").symlink_to(tmp_path / "included")
    os.link(tmp_path / "ignored/hidden.txt", tmp_path / "hardlink.txt")
    os.mkfifo(tmp_path / "fifo")
    tools.relative(".")  # Remember the protected directory before its alias changes.
    state.rename(tmp_path / "moved")

    async def scenario() -> None:
        assert await tools.list_files() == {"paths": [".gitignore", "included/"], "truncated": False}
        assert await tools.find("*.log") == {"paths": ["included/keep.log"], "truncated": False}
        assert await tools.find("*", "ignored") == {"paths": [], "truncated": False}
        result = await tools.search("below", "included/keep.log")
        assert result["matches"] == ["included/keep.log:1: found below"]
        assert (await tools.search("expose", "ignored/hidden.txt"))["matches"] == []
        assert (await tools.search("expose"))["matches"] == []

    asyncio.run(scenario())


def test_native_unsafe_ignore_files_return_no_partial_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_native(monkeypatch)
    tools = Harness(HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "private-state", demo=True)).tools
    (tmp_path / "child").mkdir()
    (tmp_path / "child/visible.txt").write_text("visible\n")
    (tmp_path / "secret.txt").write_text("password\n")
    (tmp_path / "child/.gitignore").symlink_to(tmp_path / "secret.txt")
    with pytest.raises(ValueError, match="Cannot safely read workspace ignore file") as rejected:
        asyncio.run(tools.find())
    assert "password" not in str(rejected.value)
    monkeypatch.delenv("NGN_NATIVE_DISCOVERY")
    assert asyncio.run(tools.list_files())["paths"] == ["child/", "secret.txt"]
