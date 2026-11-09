"""Deterministic performance sampling, bounded storage, privacy and panel tests."""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from textual.app import App
from textual.app import ComposeResult

from nagents.tui.performance import PerformanceMonitor
from nagents.tui.performance import PerformancePanel
from nagents.tui.performance import peak_memory_bytes

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass
class Clock:
    wall: float = 0.0
    cpu: float = 0.0

    def now(self) -> float:
        return self.wall

    def process(self) -> float:
        return self.cpu

    def advance(self, seconds: float, cpu: float = 0.0) -> None:
        self.wall += seconds
        self.cpu += cpu


def monitor(clock: Clock, *, cooldown: float = 30.0) -> PerformanceMonitor:
    return PerformanceMonitor(enabled=True, clock=clock.now, process_clock=clock.process, cooldown=cooldown)


def test_disabled_does_not_sample_and_toggle_resets() -> None:
    def forbidden() -> float:
        pytest.fail("disabled monitor read clock")

    disabled = PerformanceMonitor(clock=forbidden, process_clock=forbidden)
    disabled.tick({"backlog": 8})
    with disabled.measure("callback"):
        pass
    assert disabled.snapshot().sample_count == 0
    clock = Clock()
    subject = monitor(clock)
    clock.advance(1.25, 0.25)
    subject.tick({"backlog": 8})
    assert subject.snapshot().cpu_percent == 20
    with subject.measure("spanning reset"):
        subject.enabled = False
        subject.enabled = True
    assert subject.snapshot().callbacks == {}
    assert subject.snapshot().sample_count == 0
    assert subject.snapshot().counters == {}
    clock.advance(1, 0.1)
    subject.tick()
    assert subject.snapshot().loop_lag.latest_ms == 0
    assert subject.snapshot().cpu_percent == pytest.approx(10)


def test_bounded_samples_cpu_counter_copies_and_peak_memory() -> None:
    clock = Clock()
    subject = PerformanceMonitor(
        enabled=True, max_samples=2, clock=clock.now, process_clock=clock.process, memory_reader=lambda: 1048576
    )
    counters = {"backlog": 4}
    for delta in (1.5, 1.25, 1.125):
        clock.advance(delta, 0.25)
        subject.tick(counters)
    counters["backlog"] = 99
    result = subject.snapshot()
    assert result.sample_count == 2
    assert result.loop_lag.latest_ms == 875
    assert result.loop_lag.mean_ms == 812.5
    assert result.loop_lag.max_ms == 875
    assert result.cpu_percent == pytest.approx(100 * 0.25 / 1.125)
    assert result.peak_rss_bytes == 1048576
    assert result.counters == {"backlog": 4}
    subject.tick({"new": 1})
    assert result.counters == {"backlog": 4}
    assert subject.snapshot().counters == {"new": 1}
    assert subject.snapshot().cpu_percent == 0


def test_measure_exception_and_cardinality_bounds() -> None:
    clock = Clock()
    subject = PerformanceMonitor(enabled=True, max_samples=2, clock=clock.now, process_clock=clock.process)
    for name in ("first", "second", "third", "first", "first"):
        with pytest.raises(RuntimeError), subject.measure(name):
            clock.advance(0.25)
            raise RuntimeError("payload never logged")
    snapshot = subject.snapshot()
    assert set(snapshot.callbacks) == {"first", "second"}
    assert snapshot.callbacks["first"].count == 2
    assert snapshot.callbacks["first"].latest_ms == 250
    subject.tick({str(i): i for i in range(100)})
    assert len(subject.snapshot().counters) == 2


@pytest.mark.parametrize("metric", ["loop_lag", "callback"])
def test_sustained_warnings_cooldown_recovery_and_privacy(metric: str, caplog: pytest.LogCaptureFixture) -> None:
    clock = Clock()
    subject = monitor(clock, cooldown=10)

    def sample(bad: bool) -> None:
        if metric == "loop_lag":
            clock.wall = int(clock.wall) + 1 + (0.25 if bad else 0)
            subject.tick({"PRIVATE-PAYLOAD": 777})
        else:
            with subject.measure("PRIVATE-PAYLOAD"):
                clock.advance(0.25 if bad else 0.01)

    with caplog.at_level(logging.INFO, logger="nagents.tui"):
        sample(True)
        sample(True)
        assert not caplog.records
        sample(True)
        assert len(caplog.records) == 1
        for _ in range(3):
            sample(True)
        assert len(caplog.records) == 1
        clock.advance(11)
        sample(True)
        assert len(caplog.records) == 2
        for _ in range(3):
            sample(False)
        assert len(caplog.records) == 3
        assert [record.__dict__["status"] for record in caplog.records] == ["degraded", "degraded", "recovered"]
        assert all(record.__dict__["metric"] == metric for record in caplog.records)
        assert all(record.__dict__["phase"] == "performance" for record in caplog.records)
        assert "PRIVATE-PAYLOAD" not in str([record.__dict__ for record in caplog.records])


def test_deadline_lag_late_then_short_intervals() -> None:
    clock = Clock(wall=10)
    subject = PerformanceMonitor(enabled=True, interval=0.25, clock=clock.now, process_clock=clock.process)
    # Miss multiple slots, then catch up early. Early ticks must not consume the
    # next deadline or rebase cadence; a later short interval is still late.
    for timestamp, expected_ms in ((10.875, 625), (10.9375, 0), (11.0625, 62.5), (11.25, 0)):
        clock.wall = timestamp
        subject.tick()
        assert subject.snapshot().loop_lag.latest_ms == expected_ms


def test_callback_warning_state_is_per_label_and_bounded(caplog: pytest.LogCaptureFixture) -> None:
    clock = Clock()
    subject = PerformanceMonitor(enabled=True, max_samples=2, clock=clock.now, process_clock=clock.process)

    def callback(name: str, duration: float) -> None:
        with subject.measure(name):
            clock.advance(duration)

    with caplog.at_level(logging.INFO, logger="nagents.tui"):
        for _ in range(3):
            callback("slow", 0.25)
            callback("fast", 0.01)
        assert subject.snapshot().active_warnings == ("callback",)
        assert len(caplog.records) == 1
        for _ in range(3):
            callback("fast", 0.01)
            callback("untracked", 0.25)
        assert subject.snapshot().active_warnings == ("callback",)
        assert len(caplog.records) == 1
        assert len(subject._callback_warnings) == 2
        for _ in range(3):
            callback("slow", 0.01)
        assert subject.snapshot().active_warnings == ()
        assert [record.__dict__["status"] for record in caplog.records] == ["degraded", "recovered"]
        subject.enabled = False
        assert subject._callback_warnings == {}


def test_transient_lag_does_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    clock = Clock()
    subject = monitor(clock)
    for timestamp in (1.5, 2, 3.5, 4, 5.5):
        clock.wall = timestamp
        subject.tick()
    assert not caplog.records


@pytest.mark.parametrize("platform,multiplier", [("linux", 1024), ("darwin", 1)])
def test_resource_reports_peak_in_bytes(monkeypatch: pytest.MonkeyPatch, platform: str, multiplier: int) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setitem(
        sys.modules, "resource", SimpleNamespace(RUSAGE_SELF=0, getrusage=lambda _: SimpleNamespace(ru_maxrss=123))
    )
    assert peak_memory_bytes() == 123 * multiplier


def test_memory_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "resource", None)
    assert peak_memory_bytes() is None
    clock = Clock()

    def unavailable() -> int:
        raise OSError("unsupported")

    subject = PerformanceMonitor(enabled=True, clock=clock.now, process_clock=clock.process, memory_reader=unavailable)
    clock.advance(1)
    subject.tick()
    assert subject.snapshot().peak_rss_bytes is None


@pytest.mark.parametrize(
    "factory",
    [
        lambda: PerformanceMonitor(interval=0),
        lambda: PerformanceMonitor(interval=float("nan")),
        lambda: PerformanceMonitor(max_samples=0),
        lambda: PerformanceMonitor(sustained_samples=0),
        lambda: PerformanceMonitor(cooldown=-1),
        lambda: PerformanceMonitor(lag_threshold=float("inf")),
    ],
)
def test_invalid_settings(factory: Callable[[], PerformanceMonitor]) -> None:
    with pytest.raises(ValueError):
        factory()


@pytest.mark.asyncio
async def test_panel_in_textual_app() -> None:
    clock = Clock()
    subject = PerformanceMonitor(clock=clock.now, process_clock=clock.process, memory_reader=lambda: None)
    panel = PerformancePanel(subject, id="performance")

    class PanelApp(App[None]):
        def compose(self) -> ComposeResult:
            yield panel

    async with PanelApp().run_test() as pilot:
        assert "disabled" in panel.render().plain
        subject.enabled = True
        clock.advance(1.25, 0.25)
        subject.tick({"backlog": 3})
        with subject.measure("[markup]"):
            clock.advance(0.05)
        panel.refresh_metrics()
        await pilot.pause()
        text = panel.render().plain
        assert "Loop lag\n  now  250.0 ms" in text
        assert "CPU: 20.0%" in text
        assert "Peak RSS: n/a" in text
        assert "backlog: 3" in text.splitlines()
        assert "Callback [markup]\n  now  50.0 ms" in text
        assert panel.render().spans == []
        assert panel.render().no_wrap
        assert all(len(line) <= 27 for line in text.splitlines())


def test_tool_latency_is_bounded_and_never_callback_warning(caplog: pytest.LogCaptureFixture) -> None:
    clock = Clock()
    subject = PerformanceMonitor(enabled=True, max_samples=2, clock=clock.now, process_clock=clock.process)
    for name in ("tool", "tool", "tool", "second", "ignored"):
        subject.record_tool_duration(name, 60)
    snapshot = subject.snapshot()
    assert set(snapshot.tool_durations) == {"tool", "second"}
    assert snapshot.tool_durations["tool"].count == 2
    assert snapshot.tool_durations["tool"].latest_ms == 60000
    assert snapshot.callbacks == {}
    assert snapshot.active_warnings == ()
    assert not caplog.records
    for invalid in (-1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            subject.record_tool_duration("tool", invalid)
    subject.enabled = False
    subject.record_tool_duration("disabled", 99)
    assert subject.snapshot().tool_durations == {}


@pytest.mark.asyncio
async def test_narrow_panel_warning_recovery_and_scroll() -> None:
    clock = Clock()
    subject = PerformanceMonitor(
        enabled=True, interval=0.25, max_samples=240, clock=clock.now, process_clock=clock.process
    )
    panel = PerformancePanel(subject)

    class RailApp(App[None]):
        CSS = "PerformancePanel { width: 31; }"

        def compose(self) -> ComposeResult:
            yield panel

    async with RailApp().run_test(size=(40, 40)) as pilot:
        for _ in range(3):
            clock.advance(0.5)
            subject.tick({"backlog": 2, "tasks": 4})
        assert subject.snapshot().active_warnings == ("loop_lag",)
        for i in range(10):
            subject.record_tool_duration(f"tool{i}", 0.5)
        panel.refresh_metrics()
        await pilot.pause()
        assert panel.has_class("warning")
        assert "! Loop lag warning" in panel.render().plain
        assert "backlog: 2\ntasks: 4" in panel.render().plain
        assert panel.max_scroll_y > 0
        assert panel.size.height <= 32
        for _ in range(3):
            clock.advance(0.25)
            subject.tick()
        panel.refresh_metrics()
        await pilot.pause()
        assert not panel.has_class("warning")
        assert subject.snapshot().active_warnings == ()
        assert "warning" not in panel.render().plain
        # Sustained degradation still shows during log cooldown after recovery.
        for _ in range(3):
            clock.advance(0.5)
            subject.tick()
        panel.refresh_metrics()
        assert panel.has_class("warning")
        subject.enabled = False
        panel.refresh_metrics()
        assert not panel.has_class("warning")
