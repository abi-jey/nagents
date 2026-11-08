# State-driven dot field

In your Poetry shell, run:

```sh
env -u NO_COLOR python -m examples.tui_state_field
```

The terminal's own background and ordinary text color are preserved everywhere,
including the message area and controls. Only particle foregrounds receive RGB
gradients. This revision keeps the final layout positions fixed and reveals
foreground content in place, instead of stretching and clipping rows. It uses
the same inline terminal buffer throughout. `--fullscreen` is available for a
full-screen comparison.

Before the interface opens, a distinct compact introduction shows **ngn**, the
current checkout's version, **Your assistant.**, and the gradient particle field.
The wordmark stays in place as Sweep brings in the TUI below it. Press Enter to
continue early, or use `--skip-intro` when comparing only interface transitions.
The version is read from this checkout's `pyproject.toml`, falling back to installed
package metadata outside the checkout; no version number is hardcoded.

## Opening and closing

| Key | Paired entrance / exit |
| --- | --- |
| F1 | Sweep: text draws in horizontally with a colored leading edge; exit reverses the sweep |
| F2 | Stagger: complete lines arrive in sequence; exit clears them in reverse order |
| F3 | Quiet: a short entrance and 250 ms exit |
| R | Replay closing, the versioned introduction, and reopening without exiting or restarting particle motion |
| Q | Animate the exit, gathering the field before returning to the shell |
| Escape | Cancel the transition and keep the demo open |
| Ctrl+C / Ctrl+Q | Exit immediately |

Each entrance opens the same live field; each exit gathers it back to a small
mark. Ordinary state transitions and gradient palettes are unchanged. Closing
takes 650 ms (250 ms in Quiet). Pressing Q again skips the animation. Other state
controls dismiss a transition so you can keep exploring without waiting.

You can also launch with `--transition sweep`, `--transition stagger`, or
`--transition quiet`. `--no-animations` opens and closes immediately.

## State gradients

The gradient now follows the state rather than a manually selected palette:

| Key | State | Color and motion |
| --- | --- | --- |
| 0 | Starting | Gathered blue/teal field |
| 1 | Working | Blue/violet/mint, roaming particles |
| 2 | Tool use | Amber/peach, faster directional motion |
| 3 | Delegating | Violet/blue, outward flow; child starts |
| 4 | Parallel work | Cooler violet/teal, quieter circulation |
| 5 | Delegation returned | Gold/mint, inward flow; child settles |
| 6 | Waiting for approval | Amber, restrained motion |
| 7 or W | Waiting for wake-up | Slow blue field, countdown and reason |
| 8 or D | Waking up | Blue through teal/green, gathering energy |
| 9 | Responding | Mint/teal, steady flow |
| I | Idle | Quiet blue/gray |
| C | Complete | Green, settling particles |
| X | Error | Muted rose, persistent indication without flashing |

Click any state, including during a transition. Each color channel and motion
parameter retains its current value and velocity when the target changes; the
particle phase never restarts. The same fixed-size field stays mounted in idle,
waiting, completion and error states too. Labels state the current target
immediately while the visual transitions catch up continuously.

**P** plays/stops a complete 34-second scenario with independent child activity.
Selecting a state stops playback and transitions directly from the current
visual. **S** cycles the child's state independently. **G** compares gradient and
solid color. **Space** toggles motion; **L** opens timestamped sample events.
**7** starts a 12-second timer that wakes automatically;
**D** wakes immediately. The sequence uses a shorter timer to keep the demo brief.

The provider/model label remains below the message box. All conversation,
delegation, errors and timers are simulated. No model calls, tool execution,
credentials, persistent logs, or production TUI changes are involved.
