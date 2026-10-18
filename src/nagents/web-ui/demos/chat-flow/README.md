# Conversation in motion

Run `npm run demo:chat-flow` from `src/nagents/web-ui`, then open
`http://127.0.0.1:18892/`. Build with `npm run demo:chat-flow:build` and test with
`npm run demo:chat-flow:test`.

A local 36-second event replay compares three reasoning presentations. Use Play,
Pause, Replay, the seek bar, playback speed, or a named moment. Tool previews,
canonical calls, output, results, response chunks, and follow-up admission pass
through the production transcript reducer. The conversation renderer, grouping,
tool inspectors and Markdown are production components. Read first uses the
production reasoning component; Inline and Margin remain comparison prototypes.
The replay includes an Explorer delegation, its separate reasoning and tool call,
and its returned result while the main assistant continues working.
It opens paused during streamed output so the five detail levels can be compared
immediately. The Detail bar above the conversation switches presets at the same
point in the replay; Appearance retains every per-category control.
No model or tool is called. The event stream is synthetic and deterministic.

The scripted follow-up arrives while an edit runs. It remains queued until the
next response boundary. The full Appearance panel controls visibility, grouping,
and collapsed or expanded details. Select Everything to watch all arguments fill,
or Compact to inspect the quieter grouped view. Manual disclosures remain open
through content updates. Pause or seek to inspect any intermediate state.

Context is the only metrics entry. The conversation has no run-usage footer.
Agent bylines use the selected name once per admitted turn; subagent labels name
their immediate parent and target.
Appearance settings use the real browser-local preferences, independently of
the prototype choice. Reduced-motion preferences disable decorative motion;
playback remains explicit and can be paused at any point.
