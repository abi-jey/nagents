"""Unsupported private-log platforms fail before creating any diagnostic files."""

from pathlib import Path

import pytest

from nagents.harness.wire_log import WireLog


def test_wire_log_requires_nofollow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    with pytest.raises(ValueError, match="disable wire_log"):
        WireLog(tmp_path)
    assert not (tmp_path / "logs").exists()
