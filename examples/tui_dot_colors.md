# Dot field colors

This separate playground keeps option 2's particle motion and changes the color
of the dots. Text stays quiet and stable. The production TUI is unchanged.

From `~/nagents`, in your Poetry shell:

```sh
env -u NO_COLOR python -m examples.tui_dot_colors
```

| Key | Treatment |
| --- | --- |
| 1 | Mint: jade through pale green |
| 2 | Prism: blue, violet and mint |
| 3 | Amber: warm amber, peach and rose |
| 4 | Ice: restrained blue and silver |
| G | Switch between a slowly flowing gradient and a single color |
| C | Cycle colors |

M changes the main agent state; S changes the child state. W schedules a wake-up,
D makes it due now, E shows details, L shows sample timings, and Space pauses the
motion. Q or Ctrl+C exits. Provider + model remain below the message box.

Startup begins as a small animated block **in the existing shell**, growing
before Textual enters the alternate screen. The workspace then fades into view.
This is a design preview with a fixed 2.2-second entrance, not real initialization
progress. Press **0** to try an expanding entrance inside the app; the literal
shell-to-TUI handoff is visible when launching the command again.

Use `--color prism` to start in another palette, `--skip-entrance` to go directly
to the playground, or `--no-animations` to disable all entrance/motion effects.
The shell entrance uses ANSI colors because its background is unknown. Inside
the painted TUI the dots use RGB gradients. `--theme terminal` preserves native
terminal colors and uses an ANSI fallback throughout.

The launch command enables color for this process even when your shell sets
`NO_COLOR`; it does not change your shell's configuration. Without that prefix,
the app respects `NO_COLOR` and Textual displays it in monochrome.

All conversations, agent states, provider/model labels and timings are sample
data. No models, credentials, tools or persistent logs are used.
