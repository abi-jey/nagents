# TUI motion studies

Run in your existing Poetry shell, from the repository root:

```sh
python -m examples.tui_motion
```

Three options share the same scenarios. Provider + model always appear below the
message box, never in the header.

```sh
python -m examples.tui_motion --design network
python -m examples.tui_motion --design orbit
python -m examples.tui_motion --design pulse
```

- **1 · Network / inline:** packets between nodes; compact status columns.
- **2 · Orbit / stacked:** orbiting motion; each actor has a separate status row.
- **3 · Pulse / columns:** staggered pulses with a small activity strip.

Use **1/2/3** or the buttons to compare options without restarting.

| Key | Preview |
| --- | --- |
| M | Main assistant: thinking → tool → response → ready |
| S | Subagent: tool → waiting for approval → returned |
| W | Schedule a wake-up for 20 seconds from now |
| D | Make the wake-up due immediately |
| X | Clear the wake-up |
| Space | Pause motion; elapsed clocks and wake-up countdown keep running |
| 0 | Replay the startup preview |
| L | Show UTC timestamps and monotonic elapsed event timings |
| Q / Ctrl+C | Exit the demo |

When a wake-up becomes due while the main assistant is busy, it queues for the
next turn. Press M until the assistant reaches ready to see it resume. Details
include the target, reason, scheduled UTC time, and countdown.

The themes are ngn's existing `terminal`, `graphite`, `ocean`, and `ember` presets:

```sh
python -m examples.tui_motion --theme graphite
python -m examples.tui_motion --no-animations
```

This is an isolated animation/layout demo with simulated actor state and sample
model identity. It never reads credentials, calls a provider, executes tools, or
writes workspace files. The timing preview stays in memory. It does not change
the production TUI until a direction is selected.
