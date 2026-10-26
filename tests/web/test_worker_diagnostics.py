"""Lifecycle diagnostics report failed producers without their sensitive payloads."""

import asyncio
from pathlib import Path

import pytest

from nagents.web.channels.host import ChannelHost
from tests.support.web import client_app
from tests.support.web import no_guarded_workspace_io as no_guarded_workspace_io


def test_inbox_worker_failure_is_logged_without_exception_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def failed(_host: ChannelHost) -> None:
        raise RuntimeError("SECRET credential and private message content")

    monkeypatch.setattr(ChannelHost, "worker", failed)

    async def check() -> None:
        async with client_app(tmp_path) as (app, _, _, _):
            worker = app.state.web.channels.tasks[0]
            with pytest.raises(RuntimeError, match="SECRET"):
                await worker
            assert worker.done()

    asyncio.run(check())
    failures = [record for record in caplog.records if "Web background worker failed" in record.message]
    assert len(failures) == 1
    assert "role=ngn-web-inbox exception=RuntimeError" in failures[0].message
    assert failures[0].exc_info is None
    assert "SECRET" not in caplog.text and "private message content" not in caplog.text
