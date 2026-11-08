# Motion studies II

A quieter conversation-first revision, with provider + model below the message
box. No activity dashboard, spinner-frame animation, or additional dependencies.

In your existing Poetry shell, from `~/nagents`:

```sh
python -m examples.tui_motion_v2 --theme graphite
```

Three methods are available in the same app, using **1 / 2 / 3**:

1. **Shimmer** moves interpolated light across unchanged status text.
2. **Dot field** rasterizes continuously moving particles into terminal braille
   cells. Its movement comes from positions and time rather than a frame list.
3. **Breathe** gently interpolates the status brightness. State changes also use
   a short, partially transparent fade.

Use `--effect shimmer`, `--effect field`, or `--effect breathe` to start directly
in an option. The `terminal` theme preserves native terminal colors, with
bold/dim fallbacks. Painted themes show the smooth RGB light interpolation.

These techniques are inspired by the approaches documented in
[TerminalTextEffects](https://chrisbuilds.github.io/terminaltexteffects/),
[tachyonfx](https://docs.rs/tachyonfx/latest/tachyonfx/), and
[Textual's animation system](https://textual.textualize.io/guide/animation/).
The demo implements small effects directly; those external effect engines are
not installed or added to ngn's dependencies.

| Key | Action |
| --- | --- |
| M | Assistant thinking / tool / response / ready |
| S | Subagent working / waiting / returned |
| W | Schedule a wake-up for 20 seconds later |
| D | Make the wake-up due now; it queues while the main agent is busy |
| E | Expand agent and wake-up information, including reason and due UTC time |
| L | Show event timestamps and monotonic elapsed timings |
| Space | Turn motion off/on; clocks keep running |
| 0 | Preview the startup effect |
| Q / Ctrl+C | Exit |

This example uses sample actor/model state and in-memory timings. It does not
call models, execute tools, read credentials, or write session/wire logs. The
production TUI is unchanged while the interaction design is being chosen.
