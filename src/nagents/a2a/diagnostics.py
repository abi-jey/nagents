"""Useful error locations without exception bodies, source lines or locals."""

from __future__ import annotations


def error_frames(error: BaseException) -> tuple[str, ...]:
    frames: list[str] = []
    trace = error.__traceback__
    while trace is not None:
        code = trace.tb_frame.f_code
        filename = code.co_filename.replace("\\", "/").rsplit("/", 1)[-1]
        location = f"{filename[:128]}:{code.co_name[:128]}:{trace.tb_lineno}"
        frames.append("".join(char if " " <= char <= "~" else "?" for char in location))
        trace = trace.tb_next
    return tuple(frames[-8:])
