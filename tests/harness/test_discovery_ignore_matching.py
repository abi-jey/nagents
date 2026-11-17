"""Ignore matching retains ancestor semantics without per-rule Path traversal."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from nagents.harness import Harness
from nagents.harness import HarnessConfig

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.requires_posix


@pytest.mark.asyncio
async def test_ignore_origins_anchoring_negation_and_nested_search(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / ".gitignore").write_text("*.log\n!keep.log\n/root.py\nsrc/hidden.py\ncache/\n")
    (root / "src/deep").mkdir(parents=True)
    (root / "src/cache").mkdir()
    (root / "src/.gitignore").write_text("/local.py\ndeep/hidden.py\n!keep.log\n")
    visible = ["keep.log", "src/keep.log", "src/root.py", "src/deep/local.py", "src/deep/visible.py"]
    hidden = ["root.py", "src/hidden.py", "src/local.py", "src/deep/hidden.py", "src/drop.log", "src/cache/file.py"]
    for name in [*visible, *hidden]:
        (root / name).write_text("needle\n")
    harness = Harness(HarnessConfig(workspace=root, data_dir=tmp_path / "state"))
    for path in (".", "src", "src/deep"):
        expected = sorted(name for name in visible if path == "." or name.startswith(path + "/"))
        found = await harness.tools.find("**/*", path=path)
        paths = found["paths"]
        assert isinstance(paths, list)
        assert [
            name
            for name in paths
            if isinstance(name, str) and not name.endswith("/") and not name.endswith(".gitignore")
        ] == expected
        result = await harness.tools.search("needle", path=path)
        assert result["matches"] == [f"{name}:1: needle" for name in expected]
    assert not (await harness.tools.search("needle", path="src/deep/hidden.py"))["matches"]


@pytest.mark.asyncio
async def test_path_discovery_does_not_load_or_return_instructions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "nested").mkdir()
    (tmp_path / "AGENTS.md").write_text("Root instructions")
    (tmp_path / "nested/AGENTS.md").write_text("Nested instructions")
    (tmp_path / "nested/wire.py").write_text("pass\n")
    harness = Harness(HarnessConfig(workspace=tmp_path, data_dir=tmp_path.parent / "state"))

    def unexpected_instructions(*args: object, **kwargs: object) -> str:
        pytest.fail("Path discovery must not load or refresh instructions")

    monkeypatch.setattr(harness.tools, "instructions", unexpected_instructions)
    for path in (".", "nested"):
        result = await harness.tools.find("**/*wire*", path=path)
        assert result == {"paths": ["nested/wire.py"], "truncated": False}
        listing = await harness.tools.list_files(path=path)
        assert set(listing) == {"paths", "truncated"}
    assert not harness.instructions
