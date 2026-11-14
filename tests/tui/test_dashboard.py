"""Opening analytics is a local action, never an agent prompt."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from tests.support.tui import FakeHarness
from tests.support.tui import idle
from tests.support.tui import make_app
from tests.support.tui import send


@pytest.mark.asyncio
async def test_dashboard_command_opens_browser_without_model_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = FakeHarness(tmp_path)
    app = make_app(backend)
    start = AsyncMock(return_value="http://127.0.0.1:12345/dashboard")
    close = AsyncMock()
    opened: list[str] = []
    monkeypatch.setattr(app._dashboard, "start", start)
    monkeypatch.setattr(app._dashboard, "close", close)

    async def open_browser(url: str) -> bool:
        opened.append(url)
        return True

    monkeypatch.setattr("nagents.tui.app.open_dashboard_browser", open_browser)
    async with app.run_test() as pilot:
        await idle(app, pilot)
        await send(app, pilot, "/dashboard")
        await app.workers.wait_for_complete()
        assert opened == ["http://127.0.0.1:12345/dashboard"]
        assert backend.prompts == []
        assert backend.commands.get("dashboard") is not None
    close.assert_awaited_once()


@pytest.mark.asyncio
async def test_dashboard_start_failure_stays_in_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = make_app(FakeHarness(tmp_path))
    monkeypatch.setattr(app._dashboard, "start", AsyncMock(side_effect=ValueError("Install nagents[web]")))
    async with app.run_test() as pilot:
        await idle(app, pilot)
        app.command("/dashboard")
        await app.workers.wait_for_complete()
        assert not app._dashboard_opening
        assert "Install nagents[web]" in app._last_error


@pytest.mark.asyncio
async def test_dashboard_close_failure_still_closes_harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = FakeHarness(tmp_path)
    app = make_app(backend)
    monkeypatch.setattr(app._dashboard, "close", AsyncMock(side_effect=RuntimeError("dashboard shutdown failed")))
    with pytest.raises(RuntimeError, match="dashboard shutdown failed"):
        async with app.run_test() as pilot:
            await idle(app, pilot)
    assert backend.closed
