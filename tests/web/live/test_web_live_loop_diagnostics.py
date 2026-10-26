"""A blocked audio loop reports code locations, never live frame values."""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING
from typing import cast

import pytest

from nagents.web.live import loop_diagnostics as live_loop_diagnostics
from nagents.web.live.diagnostics import BrowserAudioHealth
from nagents.web.live.loop_diagnostics import LoopStallSampler
from nagents.web.live.loop_diagnostics import source_sites
from tests.support.hang_guard import HANG_GUARD

if TYPE_CHECKING:
    from types import FunctionType


def _compiled(source: str, filename: str, values: dict[str, object]) -> dict[str, object]:
    scope = {"sys": sys, "source_sites": source_sites, **values}
    exec(compile(source, filename, "exec"), scope)
    return scope


def test_sampler_keeps_only_bounded_package_code_locations() -> None:
    source = "def inspect(depth):\n    return inspect(depth - 1) if depth else source_sites(sys._getframe())\n"
    scope = _compiled(source, str(live_loop_diagnostics._PACKAGE / "web" / "live" / "diagnostics.py"), {})
    inspect = cast("FunctionType", scope["inspect"])
    sites = inspect(10)
    assert sites == ("nagents.web.live.diagnostics:inspect:2",) * 4
    assert source_sites(sys._getframe()) == ()


@pytest.mark.parametrize("filename", ["/workspace/private-token.py", "../web/private.py", "web/private.py"])
def test_sampler_omits_workspace_and_relative_source_paths(filename: str) -> None:
    scope = _compiled("result = source_sites(sys._getframe())", filename, {"private": "secret credential"})
    assert scope["result"] == ()


def test_sampler_omits_untrusted_code_names_and_retains_no_frame_values() -> None:
    filename = str(live_loop_diagnostics._PACKAGE / "web" / "live" / "diagnostics.py")
    scope = _compiled("def inspect():\n    return source_sites(sys._getframe())\n", filename, {})
    inspect = cast("FunctionType", scope["inspect"])
    inspect.__code__ = inspect.__code__.replace(co_qualname="private token\ncredential")
    assert inspect() == ()


def test_sampler_is_overdue_only_rate_limited_and_identity_validated(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    now = 10.0
    monkeypatch.setattr(live_loop_diagnostics, "monotonic", lambda: now)
    caplog.set_level(logging.WARNING, logger="uvicorn.error")
    sampler = LoopStallSampler("Bearer private token")
    sampler._sample()
    assert not caplog.records
    now += 0.2
    sampler._sample()
    sampler._sample()
    assert len(caplog.records) == 1
    assert "voice_session_id=unavailable overdue_ms=150.0" in caplog.text
    assert "Bearer" not in caplog.text and "private" not in caplog.text
    now += 5
    sampler._sample()
    assert len(caplog.records) == 2
    sampler.beat()
    sampler._sample()
    assert len(caplog.records) == 2


@pytest.mark.asyncio
async def test_real_blocked_loop_reports_location_and_joins_sampler_on_cancellation(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    caplog.set_level(logging.WARNING, logger="uvicorn.error")
    sampled = threading.Event()
    warning = live_loop_diagnostics.logger.warning

    def report(message: str, *arguments: object) -> None:
        warning(message, *arguments)
        sampled.set()

    monkeypatch.setattr(live_loop_diagnostics.logger, "warning", report)
    filename = str(Path(live_loop_diagnostics.__file__).with_name("diagnostics.py"))
    scope = _compiled(
        "def blocked_operation():\n    private = 'secret request content'\n    pause(timeout)\n",
        filename,
        {"pause": sampled.wait, "timeout": HANG_GUARD},
    )
    blocked = cast("FunctionType", scope["blocked_operation"])
    before = set(threading.enumerate())
    task = asyncio.create_task(BrowserAudioHealth("a" * 32).watch())
    await asyncio.sleep(0)
    try:
        blocked()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert "sources=nagents.web.live.diagnostics:blocked_operation:3" in caplog.text
    assert "secret" not in caplog.text and "private" not in caplog.text
    assert not [
        thread for thread in threading.enumerate() if thread not in before and thread.name == "ngn-live-loop-sampler"
    ]
