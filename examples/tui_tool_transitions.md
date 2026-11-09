# Compact tool-call transitions

From the repository root:

```bash
poetry run python -m examples.tui_tool_transitions
```

This is a standalone Textual design study, not a change to ngn's production
renderer or an assertion that providers stream arguments/results in this way.
No models, MCP servers, commands, or file mutations are invoked.

The same mounted row transitions automatically through writing arguments (2.4s),
queued (1.5s), waiting for simulated approval (2s), and running (4s), then freezes
at complete or failed. Timers use a simulated clock advancing 1/30s per tick;
pause freezes the entire simulation. Queue and approval time are not counted as
execution time. Replay restarts it. The shell's 30s label is a timeout budget,
not an estimate of remaining time.

The underlying **Specular shimmer** (the former design 2) remains the only
activity design. A feathered teal (#81c7be) to lavender (#b5a5df) foreground wave
crosses the name using smootherstep easing over 1.8s, blending in over 240ms at
30Hz. The palette does not switch with tool state. Base foreground is #888888;
light-terminal contrast still needs visual testing.

Three hover treatments are built into the demo. Press a number while pointing at
a tool; the heading identifies the selected treatment. Switching does not restart
the scenario, reset timers, or close details:

- **1 Subtle background tint (default):** a muted blue-gray (#819db0 at 12%
  opacity) fills the entire header, including its whitespace and disclosure.
- **2 Bold header:** the full header becomes bold without changing its background.
- **3 Tint + bold name:** the same full-line tint, with only the tool name bold.

Exactly these three replace the earlier lift/sweep/reverse experiments; there are
no additional choices. Background and full-header weight use native Textual
`:hover` styling, independent of the simulation and animation timers. They apply
immediately, including after playback finishes. The name shimmer remains; option
3 adds its name weight on mouse entry. No animated fade gates the hover cue.

The tint is opt-in painted background on the hovered header only. Unhovered rows
remain transparent. Text, positions, semantic colors and timer values remain
unchanged. Try Space to freeze the scenario, then 1/2/3 over the same row.

Expanded arguments/output are not affected by header hover. Preview opacity stays
at 70% instead of pulsing or jumping on completion. Receipts retain semantic
failure colors; syntax highlighting remains available in expanded details.

All tools start closed: exactly one line per tool with muted, non-bold text.
Click a header or focus it and press Enter to open details; E toggles all. The
three-line preview and receipt are inside the collapsible body, never extra lines
on a closed row. State changes, completion, and failure never auto-expand a tool.
State and timer columns have fixed widths and elapsed time stays at the right
edge. Names and targets never slide or scramble. Queue and approval transitions
update immediately. Shimmer repeats during writing/running or hover; completed
rows otherwise settle. Each tool's activity uses its own phase clock, avoiding
synchronized parallel spinners. Press **M** for reduced motion: static glyphs and
static hover emphasis while state text and timers continue updating.
Options 1/3 tint the hovered header; option 2 preserves the terminal background.
Pointer movement never changes preview content or contrast.

The three-line viewport deliberately clips long output. This is a design study,
not a full-output inspector. New arrivals add rows; state changes and hover do not.

## OpenCode implementation check

Read through Chrome CDP on the current `dev` branch (not a pinned release):

- [shimmer-text.tsx](https://github.com/anomalyco/opencode/blob/dev/packages/tui/src/component/shimmer-text.tsx):
  1200ms cycle, `coast` travel and `intensityAt` mask with front/tail widths.
- [masked-text.ts](https://github.com/anomalyco/opencode/blob/dev/packages/tui/src/component/masked-text.ts):
  scratch-buffer foreground-only color matrix; wide-glyph continuation cells
  retain the head cell's intensity.
- [title-shimmer.tsx](https://github.com/anomalyco/opencode/blob/dev/packages/tui/src/component/title-shimmer.tsx):
  240ms fade envelope and 450ms title-arrival transition.
- [one-cell-spinner.tsx](https://github.com/anomalyco/opencode/blob/dev/packages/tui/src/component/one-cell-spinner.tsx):
  independent timing/glow and paused/animations-off paths.

This verifies reusable shimmer components, not which specific tool rows invoke
them. Our implementation is an adaptation in Textual/Rich, not a copy of OpenTUI's
buffer compositor or an exact reproduction of OpenCode timing. Status words
change immediately; the per-character foreground effect is what interpolates.

## Research and design rationale

Read public GitHub README pages through the shared Chrome CDP browser:

- [Bubbles](https://github.com/charmbracelet/bubbles): customizable spinner frames,
  independent stopwatch/timer and viewport primitives.
- [TachyonFX](https://github.com/ratatui/tachyonfx): cell-precise effects, foreground
  fades, sweep transitions, and composition. Adopted localized effects rather
  than layout motion or background painting.
- [TerminalTextEffects](https://github.com/ChrisBuilds/terminaltexteffects): scenes
  with character/color changes and easing. Adapted the character-cycle idea only
  to the indicator cell, avoiding text scattering or path animation.

Further research through Chrome CDP:

- [TTE Effects Showroom](https://chrisbuilds.github.io/terminaltexteffects/showroom/):
  Highlight's specular pass, Beams' illumination, Sweep/Wipe reveals. Adopted a
  short foreground-only name highlight, rejecting moving text and canvas effects.
- [Harmonica](https://github.com/charmbracelet/harmonica): critical/over-damping
  settle without oscillation. This informed the no-overshoot approach; the demo
  uses smootherstep easing, not a spring solver.

The latest pass revisited the TTE showroom's Beams, Rings, and LaserEtch
entries through Chrome CDP, adapting illumination, radial motion and etching into
whitespace/foreground effects rather than moving the actual text. Attempts to
read cli-spinners and a fresh TachyonFX page returned no readable text and were
not used as new evidence.

These are design inspirations, not new runtime dependencies or copied engines.

Previews explicitly indicate omitted lines. These are short mock previews, not
full-output inspectors. Screen/row backgrounds are transparent in CSS and syntax
backgrounds use the terminal default; actual terminal transparency still needs
visual verification on the user's terminal.
The default is always the parallel scenario: new calls arrive every 2.2 seconds,
with completed calls retained while later calls are active. No mode switch is
needed. Targets stay muted; a terminal result receipt appears inside each
completed/failed row's expandable details.

- **Space:** pause/play; **R** or **A:** replay the scenario; **N:** scroll to the
  next tool without leaving the scenario; **E:** collapse/expand previews;
  **Q:** quit.

Tool fixtures cover read_file (line-numbered Python), search (file/line matches),
edit (colored diff), shell (incrementally revealed stdout and exit code), custom
JSON, and MCP JSON with content, structuredContent and isError. The final MCP
fixture fails. JSON arguments are typed as literal text; completed JSON output
is syntax highlighted. Output streaming is fabricated for visual exploration,
not a guarantee that custom/MCP tools expose streaming results. All fixtures go
through approval so that each phase can be compared; real read-only tools may
skip that phase.

The original static design gallery remains at `examples.tui_tool_calls`.
