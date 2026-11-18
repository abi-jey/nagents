# Test performance: TUI design coverage

The ToolCard design suite shares an app's CSS/startup across related outcome
cases, **not** mutable widgets or backend state. Every outcome gets fresh cards,
which are removed before the next case. Theme and width remain pytest parameters;
failures also include `ToolCard outcome case: ...` notes. Other suites keep their
existing isolation and independent test names.

## Retained cases and assertions

`OUTCOMES` is still running, complete, failed, cancelled, exit 7, and timed out.
All four configured themes remain covered; no matrix combination was discarded.

| Test in `tui/test_tool_card_design.py` | Before → after pytest items | Retained coverage |
| --- | --- | --- |
| `test_tool_columns_align_with_and_without_timeout` | 12 → 12, now synchronous | Both 80/160-cell allocations × all six outcomes × absent/60s/600s timeout; exact timing suffixes and aligned status/timer columns. The equal-length assertion is strengthened to the exact allocated title width. This is formatting only; mounted geometry remains covered below and in `test_tool_timer.py`. |
| `test_native_hover_and_focus_keep_closed_rows_stationary` | 24 → 4, six labeled fresh-card cases each | Every theme × outcome; hover, hover+focus, focus-only and blur; unchanged foreground, adjacent rows, exact regions, collapsed height, transparent/unframed surfaces, and native bold/underline behavior. All 18 assertion sites are unchanged. |
| `test_click_and_enter_expand_real_tool_contents` | 24 → 4, six labeled fresh-card cases each | Every theme × outcome; real click and both Enter transitions, exact header regions, focused title, visible arguments/streamed evidence/output, and unframed native hover+focus. All 15 assertion sites are unchanged. |
| `test_status_and_timer_columns_survive_phases_and_resize` | 40 → 8, five labeled fresh-card cases each | Every theme × 48/100-cell width × every terminal outcome; wide Unicode target, live clock advance, changed arguments, closed-card output growth, exact terminal timing, stationary neighbors, timer shutdown, real expansion, +12-cell resize and recollapse. All 12 assertion sites and the real 0.15s timer-stop observation remain. Width and clock are reset between cases. |
| Shimmer and streamed-preview motion tests | 6 → 6 | Enabled/full, disabled/full, enabled/none; exact changing rendered spans, timer identity/replacement and cancellation. Unchanged. |
| Stale compositor allocation test | 9 → 9 | All three widths × running/complete/cancelled; allocation wins over stale geometry, including later timer/result updates. Unchanged. |
| Deferred-layout barrier regression | new: 1 | A queued widget update followed by an after-refresh update must be reflected in both content and painted height. |

Total: **115 → 44 design test items**, with **106 → 23 app startups**.
The complete TUI suite has **897 → 826 items**. The reduction is setup and pytest
item repetition, not a reduction in the 24/24/40 mounted interaction combinations.

`tests.support.tui.layout_ready` waits for widget message queues and a real
`App.wait_for_refresh()` barrier, then drains resulting messages/layout. It uses
the unchanged 60-second hang guard instead of Textual's CPU-idle heuristic.
It is only used for synchronous design/layout assertions, **not** as a substitute
for backend completion, animation completion, deferred scroll delivery, or timer
shutdown. No global motion setting, skip, retry, or watchdog was changed.

## Regression boundaries

The four immediate/deferred × small/tall approval-paging cases, including their
real `InvokeLater` hold and exact proposal/focus/draft assertions, are unchanged.
So are focus/scroll growth and restoration, child approval binding, crash/cancel/
no-replay, credential and protected-path regressions. Production code, pytest
defaults, CI/workflows, dependencies and mandatory platform/security checks are
outside this refactor.

## Measuring fairly

Use an explicit virtual environment and this checkout's source/adapter on
`PYTHONPATH`; keep temporary files, coverage and JUnit outside the checkout in a
private directory with mode 0700. Compare the **same worker count and flags**:

```sh
python -m pytest -q -n 4 tests/tui --durations=40 \
  --junitxml="$EVIDENCE/run.xml" \
  --cov=nagents.tui --cov-branch --cov-context=test \
  --cov-report="json:$EVIDENCE/run-coverage.json"
```

Set `COVERAGE_FILE` to a private evidence path as well. Record the source SHA/diff,
Python/dependency versions, CPU count, host load and concurrent builds. Parallel
testcase duration sums are **not** wall time. Compare covered line/branch sets
and the assertion/case map above, not coverage percentages alone.

### Recorded comparison (2026-11-18, base `f574211`)

With four workers and branch/context coverage, the full TUI suite passed before
and after: 897 items in 219.60s process wall time versus 826 in 242.56s. This is
**not an overall wall-time speedup**: recorded one-minute host load rose from
1.01→2.72 before to 2.59→4.35 after. The changed design module's aggregate JUnit
testcase time fell from 106.99s to 72.42s (32.3%). Its measured TUI line and branch
sets were identical. Full-suite line coverage was 4825/5299 (91.05%) → 4826/5299
(91.07%), and branch coverage 1394/1742 (80.02%) → 1397/1742 (80.20%), with no
previously covered branch lost. Timing is indicative on this shared host.

A single sequential, four-worker design-only pair, with both checkouts' bytecode
warmed by collection and no overlapping validation/build jobs, passed 115 → 44
items in 28.12s → 21.31s process wall time (**24.2% faster**). Its complete measured
TUI line and branch sets were identical. One-minute host load was 2.71→2.31 before
and 2.28→2.39 after; this is localized evidence, not a whole-suite speedup claim.

The final uninstrumented, native-extension full TUI run passed all 826 tests
(195.70s pytest elapsed); the separately selected approval/focus/scroll, queue,
login, child-policy and voice-lifecycle modules passed all 123 tests. These
validation runs overlapped each other and are not additional A/B timing samples.

The supplied profiling environment's aiohttp 3.14.5 C WebSocket reader rejects a
valid text frame when coverage tracing is enabled (the same voice fixture passes
without coverage). Both comparative coverage runs use its supported Python reader
with `AIOHTTP_NO_EXTENSIONS=1`; a separate full, uninstrumented TUI run validates
the default native extensions. This is a profiling-environment workaround only,
never a test-suite default or permission to suppress a failing voice regression.
