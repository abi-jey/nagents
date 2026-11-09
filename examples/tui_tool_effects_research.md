# Tool-row effects: deeper comparison

Research pass: 2026-11-08, system timezone CET. Public pages were read through
Chrome CDP in temporary tabs. This pass did not add dependencies or change the
renderer. It compares the existing demo against the user's constraints:

- one tool per line, closed initially;
- no hover behavior;
- native terminal background;
- muted text with fixed labels/status/timer placement;
- parallel arrivals and retained completions;
- three choices, not an accumulating effect gallery.

## Sources actually read

1. [TTE Highlight](https://chrisbuilds.github.io/terminaltexteffects/effects/highlight/),
   including embedded HighlightIterator source. It keeps original symbols visible,
   uses an eased sequence to activate character groups, and gives each character
   a base → highlight → base foreground scene. Width controls dwell duration,
   not simply the number of highlighted characters.
2. [TTE Beams](https://chrisbuilds.github.io/terminaltexteffects/effects/beams/),
   including embedded iterator source. Separate pending and active groups advance
   at their own speeds; animated beam symbols transition back to input symbols.
   Its canvas fill and final wipe are not suitable for this tool list.
3. [TTE LaserEtch](https://chrisbuilds.github.io/terminaltexteffects/effects/laseretch/).
   Separate emission speed, delay, and cooling durations make it more than a
   moving underline. Its actual laser, sparks, and character reveal should not
   be copied onto readable status labels.
4. [TTE Waves](https://chrisbuilds.github.io/terminaltexteffects/effects/waves/),
   including embedded build source. Activation travel and each character's local
   animation have separate timing; completion triggers a settling scene.
5. [TTE Rings](https://chrisbuilds.github.io/terminaltexteffects/effects/rings/).
   Actually disperses characters into rotating rings. The demo's prior 'Ripple'
   is only loosely inspired by radial ordering, not an implementation of Rings.
6. [TTE ColorShift](https://chrisbuilds.github.io/terminaltexteffects/effects/colorshift/).
   Looping gradients are possible without positional changes, but constant hue
   changes would compete with semantic status colors.
7. [TTE Spotlights](https://chrisbuilds.github.io/terminaltexteffects/effects/spotlights/).
   Beam width/falloff shape a foreground illumination region; random searching
   and convergence are excessive for multiple compact rows.
8. TTE engine [architecture](https://chrisbuilds.github.io/terminaltexteffects/effectguide/engine/architecture/),
   [animation](https://chrisbuilds.github.io/terminaltexteffects/effectguide/engine/animation/),
   and [performance](https://chrisbuilds.github.io/terminaltexteffects/effectguide/engine/performance/).
   Motion and appearance are separate; per-character scenes can finish while
   characters remain visible. Active and visible sets are distinct. Precompute
   recipes and avoid repeatedly updating settled rows.
9. W3C [Animation from Interactions](https://www.w3.org/WAI/WCAG22/Understanding/animation-from-interactions.html)
   and [Pause, Stop, Hide](https://www.w3.org/WAI/WCAG22/Understanding/pause-stop-hide.html).
   These are web guidelines, used as design guidance rather than a claim of TUI
   conformance. Reduce nonessential motion, allow it to be disabled, and provide
   a global pause independent of pointer focus.

The TTE pages describe development main toward 0.16.0, not necessarily published
0.15.0. GitHub Bubbles spinner source and TachyonFX source-tree requests returned
504 responses. A guessed frame-loop documentation URL returned 404. None of those
failures count as implementation research.

## Assessment of current choices

| Choice | What works | Weakness | Recommendation |
| --- | --- | --- | --- |
| Comet | Stable text, uses spare whitespace | Adds a second moving region; vanishes on narrow layouts; can resemble progress | Keep only as the expressive comparison, not the default |
| Ripple | No character displacement; short phase acknowledgement | A moving bright band is still attention-demanding across many rows | Limit to arrival/run-start/completion, not every queue transition |
| Etch | Effect targets the status that changed; short and localized | Current underline is much simpler than LaserEtch; underline support varies | Best starting point for a restrained default |

The main mistake in earlier iterations was novelty by glyph replacement. Better
variation changes *where attention goes* and *when motion ends*.

## Recommended direction for the next implementation

Keep exactly three designs, sharing one fixed row layout:

1. **Quiet acknowledgement:** one small running indicator, immediate readable
   state changes, then a brief foreground-only status settle. No moving text,
   whitespace particles, or repeated completion animation. Proposed default.
2. **Specular pass:** a single low-contrast highlight across the tool name on
   arrival/run-start, then across status on completion. Preserve all characters
   throughout. No rainbow gradients and no effect on timers.
3. **Signal trail:** a bounded decorative activity track in spare whitespace,
   clearly indeterminate and absent on narrow screens. Distinct expressive
   comparison, not an implied percentage or fabricated ETA.

Suggested timing ranges are hypotheses to compare, not numbers prescribed by
sources: 150–250ms arrival acknowledgement, 300–500ms state settle, and 80–125ms
indicator frame intervals. Keep semantic failure text persistently visible;
never blink it or delay it until an effect finishes.

Use separate clocks for execution elapsed time, per-state decorative effects,
and animation playback. User expansion must survive updates. Reduced motion
should disable decorative effects without freezing real status/timer updates;
Space can pause the offline simulation. Do not animate the entire row's opacity:
foreground effects must not tint the user's terminal background.

## Verification and remaining limits

Ran each existing Comet/Ripple/Etch choice headlessly with a live timer for eleven
seconds. In each run, five calls had arrived; all visible rows remained collapsed
and one terminal row high. Reduced-motion switching also worked.

This confirms behavior and geometry, not subjective visual polish. Browser text
inspection is not watching animation GIFs; no claim is made that those animations
were visually evaluated. A real-terminal comparison, especially on both light
and transparent terminal backgrounds, remains necessary before selecting a
production style.
