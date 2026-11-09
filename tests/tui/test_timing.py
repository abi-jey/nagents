"""Timer cancellation must not leave native waits running in executor threads."""

import asyncio

import pytest
import textual.timer

from nagents.tui.timing import configure_timer_sleep


@pytest.mark.parametrize("platform", ["win32", "linux", "darwin"])
def test_timer_sleep_platform_boundary(monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    original = vars(textual.timer)["sleep"]
    monkeypatch.setattr("nagents.tui.timing.sys.platform", platform)
    with monkeypatch.context() as patch:
        patch.setattr(textual.timer, "sleep", original)
        configure_timer_sleep()
        assert vars(textual.timer)["sleep"] is (asyncio.sleep if platform == "win32" else original)


@pytest.mark.asyncio
async def test_windows_timer_waits_cancel_without_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("nagents.tui.timing.sys.platform", "win32")
    monkeypatch.setattr(textual.timer, "sleep", vars(textual.timer)["sleep"])
    configure_timer_sleep()
    loop = asyncio.get_running_loop()

    def unexpected_thread(*args: object, **kwargs: object) -> None:
        pytest.fail("Timer sleep must not create native executor waits")

    monkeypatch.setattr(loop, "run_in_executor", unexpected_thread)
    assert vars(textual.timer)["sleep"] is asyncio.sleep
    tasks = [asyncio.create_task(asyncio.sleep(60)) for _ in range(100)]
    await asyncio.sleep(0)
    for task in tasks:
        task.cancel()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)
