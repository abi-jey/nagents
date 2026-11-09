"""Opt-in, bounded TUI diagnostics; the app owns the sampling timer.

Durations are seconds internally and milliseconds in snapshots. CPU percentage is
process CPU time / wall time (and can exceed 100% on multi-core workloads).
No conversation data or callback names are emitted to the diagnostics logger.
"""

from __future__ import annotations

import logging
import math
import sys
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rich.text import Text
from textual.widgets import Static

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Iterator
    from collections.abc import Mapping

logger = logging.getLogger("nagents.tui")


def peak_memory_bytes() -> int | None:
    """Return process lifetime peak RSS, or None where resource is unavailable."""
    try:
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(peak if sys.platform == "darwin" else peak * 1024)
    except (ImportError, AttributeError, OSError, ValueError):
        return None


@dataclass(frozen=True)
class DurationMetrics:
    count: int = 0
    latest_ms: float = 0.0
    mean_ms: float = 0.0
    max_ms: float = 0.0


@dataclass(frozen=True)
class PerformanceSnapshot:
    enabled: bool
    loop_lag: DurationMetrics
    callbacks: Mapping[str, DurationMetrics]
    cpu_percent: float
    peak_rss_bytes: int | None
    counters: Mapping[str, int]
    sample_count: int
    tool_durations: Mapping[str, DurationMetrics]
    active_warnings: tuple[str, ...]


@dataclass
class _WarningState:
    bad: int = 0
    good: int = 0
    active: bool = False
    last_warning: float = -math.inf


def _metrics(samples: deque[float]) -> DurationMetrics:
    if not samples:
        return DurationMetrics()
    return DurationMetrics(len(samples), samples[-1] * 1000, sum(samples) / len(samples) * 1000, max(samples) * 1000)


class PerformanceMonitor:
    """Collect only when enabled; toggle resets all samples and timing baselines.

    Call tick() at ``interval`` seconds from an app timer, not a background task.
    Counter keys and measure() names should be fixed instrumentation labels, not
    user content. Both their lengths and cardinality are bounded defensively.
    snapshot() is a detached copy and does no sampling or logging.
    """

    def __init__(
        self,
        *,
        enabled: bool = False,
        interval: float = 1.0,
        max_samples: int = 120,
        clock: Callable[[], float] = time.monotonic,
        process_clock: Callable[[], float] = time.process_time,
        memory_reader: Callable[[], int | None] = peak_memory_bytes,
        lag_threshold: float = 0.1,
        callback_threshold: float = 0.1,
        sustained_samples: int = 3,
        cooldown: float = 30.0,
    ) -> None:
        if max_samples < 1 or sustained_samples < 1:
            raise ValueError("sample limits must be positive")
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("interval must be finite and positive")
        if any(not math.isfinite(value) or value < 0 for value in (lag_threshold, callback_threshold, cooldown)):
            raise ValueError("thresholds and cooldown must be finite and nonnegative")
        self.interval = interval
        self.max_samples = max_samples
        self._clock = clock
        self._process_clock = process_clock
        self._memory_reader = memory_reader
        self._lag_threshold = lag_threshold
        self._callback_threshold = callback_threshold
        self._sustained_samples = sustained_samples
        self._cooldown = cooldown
        self._enabled = False
        self._generation = 0
        self._reset()
        self.enabled = enabled

    def _reset(self) -> None:
        self._generation += 1
        self._lag: deque[float] = deque(maxlen=self.max_samples)
        self._callbacks: dict[str, deque[float]] = {}
        self._tool_durations: dict[str, deque[float]] = {}
        self._counters: dict[str, int] = {}
        self._warnings = {"loop_lag": _WarningState()}
        self._callback_warnings: dict[str, _WarningState] = {}
        self._deadline = 0.0
        self._cpu_percent = 0.0
        self._peak_rss: int | None = None
        self._last_tick = 0.0
        self._last_cpu = 0.0

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        if self._enabled == value:
            return
        self._enabled = value
        self._reset()
        if value:
            self._last_tick = self._clock()
            self._deadline = self._last_tick + self.interval
            self._last_cpu = self._process_clock()

    def tick(self, counters: Mapping[str, int] | None = None) -> None:
        """Sample timer lateness and CPU delta; replace supplied counter snapshot."""
        if not self.enabled:
            return
        now, cpu = self._clock(), self._process_clock()
        elapsed = now - self._last_tick
        lag = max(0.0, now - self._deadline)
        # Preserve the enable-time cadence, skipping missed deadlines rather
        # than rebasing on a late tick. Early/catch-up calls consume no deadline.
        if now >= self._deadline:
            missed = math.floor((now - self._deadline) / self.interval) + 1
            self._deadline += missed * self.interval
        self._lag.append(lag)
        self._cpu_percent = max(0.0, cpu - self._last_cpu) / elapsed * 100 if elapsed > 0 else 0.0
        self._last_tick, self._last_cpu = now, cpu
        try:
            self._peak_rss = self._memory_reader()
        except (OSError, ValueError):
            self._peak_rss = None
        if counters is not None:
            self._counters = {}
            for key, value in counters.items():
                if len(self._counters) >= self.max_samples:
                    break
                self._counters[key[:80]] = value
        self._observe("loop_lag", lag, self._lag_threshold, now)

    @contextmanager
    def measure(self, name: str) -> Iterator[None]:
        """Measure a synchronous callback, including exceptional exits.

        Do not hold this context over an await: that measures elapsed latency,
        not synchronous callback work. Disabled contexts never read clocks.
        """
        if not self.enabled:
            yield
            return
        generation, started = self._generation, self._clock()
        try:
            yield
        finally:
            if self.enabled and generation == self._generation:
                now = self._clock()
                duration = max(0.0, now - started)
                label = name[:80]
                if label in self._callbacks or len(self._callbacks) < self.max_samples:
                    self._callbacks.setdefault(label, deque(maxlen=self.max_samples)).append(duration)
                    state = self._callback_warnings.setdefault(label, _WarningState())
                    self._observe("callback", duration, self._callback_threshold, now, state=state)

    def record_tool_duration(self, name: str, duration: float) -> None:
        """Record runtime tool latency in seconds, never synchronous callback cost.

        Supply a fixed tool label, not arguments or output. Disabled recording is
        a no-op. Invalid durations are rejected without changing stored samples.
        """
        if not self.enabled:
            return
        if not math.isfinite(duration) or duration < 0:
            raise ValueError("tool duration must be finite and nonnegative")
        label = name[:80]
        if label in self._tool_durations or len(self._tool_durations) < self.max_samples:
            self._tool_durations.setdefault(label, deque(maxlen=self.max_samples)).append(duration)

    def _observe(
        self, metric: str, duration: float, threshold: float, now: float, *, state: _WarningState | None = None
    ) -> None:
        if state is None:
            state = self._warnings[metric]
        if duration > threshold:
            state.bad = min(state.bad + 1, self._sustained_samples)
            state.good = 0
            if state.bad >= self._sustained_samples:
                state.active = True
                if now - state.last_warning >= self._cooldown:
                    state.last_warning = now
                    self._log(logging.WARNING, metric, "degraded", duration, threshold)
        else:
            state.bad = 0
            state.good = min(state.good + 1, self._sustained_samples)
            if state.active and state.good >= self._sustained_samples:
                state.active = False
                self._log(logging.INFO, metric, "recovered", duration, threshold)

    def _log(self, level: int, metric: str, status: str, duration: float, threshold: float) -> None:
        logger.log(
            level,
            "TUI performance %s: %s",
            status,
            metric,
            extra={
                "phase": "performance",
                "metric": metric,
                "status": status,
                "duration_ms": round(duration * 1000, 3),
                "threshold_ms": threshold * 1000,
                "sample_count": self._sustained_samples,
            },
        )

    def snapshot(self) -> PerformanceSnapshot:
        return PerformanceSnapshot(
            enabled=self.enabled,
            loop_lag=_metrics(self._lag),
            callbacks={name: _metrics(samples) for name, samples in self._callbacks.items()},
            cpu_percent=self._cpu_percent,
            peak_rss_bytes=self._peak_rss,
            counters=dict(self._counters),
            sample_count=len(self._lag),
            tool_durations={name: _metrics(samples) for name, samples in self._tool_durations.items()},
            active_warnings=tuple(metric for metric, state in self._warnings.items() if state.active)
            + (("callback",) if any(state.active for state in self._callback_warnings.values()) else ()),
        )


class PerformancePanel(Static):
    """A passive performance display; the app calls refresh_metrics() on its timer."""

    DEFAULT_CSS = """
    PerformancePanel {
        height: auto;
        max-height: 32;
        overflow-y: auto;
        border: round $accent;
        padding: 0 1;
    }
    PerformancePanel.warning {
        border: round $warning;
    }
    """

    def __init__(
        self,
        monitor: PerformanceMonitor,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes, disabled=disabled)
        self.monitor = monitor

    def refresh_metrics(self) -> None:
        self.set_class(bool(self.monitor.snapshot().active_warnings), "warning")
        self.refresh(layout=True)

    @staticmethod
    def _duration_rows(title: str, duration: DurationMetrics) -> list[str]:
        return [
            title,
            f"  now  {duration.latest_ms:.1f} ms",
            f"  mean {duration.mean_ms:.1f} ms",
            f"  max  {duration.max_ms:.1f} ms",
        ]

    def render(self) -> Text:
        snapshot = self.monitor.snapshot()
        if not snapshot.enabled:
            return Text("Performance disabled")
        memory = "n/a" if snapshot.peak_rss_bytes is None else f"{snapshot.peak_rss_bytes / 1048576:.1f} MiB"
        lines = [
            "Performance",
            f"CPU: {snapshot.cpu_percent:.1f}%",
            f"Peak RSS: {memory}",
            f"Samples: {snapshot.sample_count}",
        ]
        for metric in snapshot.active_warnings:
            lines.append("! " + {"loop_lag": "Loop lag warning", "callback": "Callback warning"}[metric])
        lines.extend(self._duration_rows("Loop lag", snapshot.loop_lag))
        for name, duration in sorted(snapshot.callbacks.items()):
            lines.extend(self._duration_rows(f"Callback {name}", duration))
        for name, duration in sorted(snapshot.tool_durations.items()):
            lines.extend(self._duration_rows(f"Tool latency {name}", duration))
        for key, value in sorted(snapshot.counters.items()):
            lines.append(f"{key}: {value}")
        # Keep even unusually long fixed labels or numeric values inside a narrow
        # rail. Full labels and unrounded values remain available in snapshot().
        return Text("\n".join(lines), overflow="ellipsis", no_wrap=True)
