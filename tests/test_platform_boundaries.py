"""Unsupported platforms must fail closed, not bypass guarded filesystem access."""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from nagents.harness import Harness
from nagents.harness import HarnessConfig
from nagents.harness import tools as tools_module


@pytest.mark.parametrize("platform", [os.name, "nt"], ids=["host", "windows"])
def test_guarded_filesystem_platform_boundary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    harness = Harness(HarnessConfig(workspace=tmp_path, data_dir=tmp_path / "state", demo=True))
    if platform == "posix":
        with harness.tools.directory(Path()) as descriptor:
            assert os.fstat(descriptor).st_ino == tmp_path.stat().st_ino
    else:
        # Patch only the tool module, not os.name globally (which breaks pathlib).
        if platform != os.name:
            monkeypatch.setattr(tools_module, "os", SimpleNamespace(name=platform))
        with (
            pytest.raises(OSError, match="Guarded workspace file tools currently require POSIX"),
            harness.tools.directory(Path()),
        ):
            pytest.fail("Non-POSIX guarded filesystem access must not succeed")
    assert not harness.config.data_dir.exists()
