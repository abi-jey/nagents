"""Safe HTTP diagnostics contain locations only, never exception data."""

from __future__ import annotations

import re
from pathlib import Path

from nagents.a2a.diagnostics import error_frames


def test_error_frames_keep_bounded_file_function_line_without_sensitive_context() -> None:
    exception_secret = "EXCEPTION_ARGUMENT_SECRET_163"
    local_secret = "LOCAL_VARIABLE_SECRET_163"
    source_secret = "SOURCE_LINE_SECRET_163"

    def recurse(depth: int) -> None:
        private_local = local_secret
        if depth:
            recurse(depth - 1)
            return
        raise RuntimeError(f"{exception_secret}: {private_local}; {source_secret}")

    try:
        recurse(12)
    except RuntimeError as error:
        frames = error_frames(error)

    assert 1 <= len(frames) <= 8
    assert all(re.fullmatch(r"[^:/\\\r\n]{1,128}:[^:/\\\r\n]{1,128}:\d{1,10}", frame) for frame in frames)
    assert any(frame.endswith(":recurse:" + frame.rsplit(":", 1)[-1]) for frame in frames)
    assert str(Path(__file__).resolve()) not in " ".join(frames)
    details = " ".join(frames)
    assert exception_secret not in details
    assert local_secret not in details
    assert source_secret not in details
