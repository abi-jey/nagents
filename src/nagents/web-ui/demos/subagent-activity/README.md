# Subagent interaction studies · revision 2

Three **standalone React demos** for choosing a UI direction. They do not connect to ngn, run tools, call providers, or expose private model reasoning. All messages, paths, commands, results, approvals, and timings are illustrative fixtures.

Revision 2 combines a conversation tree **inside the chat** with a **Working team panel on the right**. The palette, system font, surface colors, state accents, and control shapes now follow the production ngn interface: charcoal surfaces, readable neutral text, restrained blue selection, and teal completed states. All three options use the same event simulation, so the interaction differences can be compared directly.

From `src/nagents/web-ui`:

```sh
npm run demo:subagents
npm run demo:subagents:build
npm run demo:subagents:test
```

Preview: `http://127.0.0.1:4175/`. Direct choices: `#tree`, `#workspace`, `#board`. The build is written to repository `dist/subagent-concepts/`; it does not replace the production UI build. No additional packages, remote assets, or network connections are required.

## Directions

- **A — Tree + team index (`#tree`).** The main conversation contains expandable delegation branches, with their messages, tool arguments/results, assignment, and addressed composer. Several branches can stay open. The slim right-side team tree selects and scrolls to a branch, preserving the parent/child relationship. The panel can collapse to agent shortcuts.
- **B — Tree + side inspector (`#workspace`).** The main chat keeps a compact tree with current work and the latest published message. Selecting a branch opens a right-side inspector. The short team tree stays above the selected agent's activity, tool evidence, filters, and composer. This option preserves conversation space and puts detailed investigation beside it. On mobile, the inspector opens in the Working team drawer.
- **C — Tree + focus rail (`#board`).** A small delegation tree opens one full-width focused thread inside the conversation. The focused thread has an ownership breadcrumb, activity, tools, and composer; it can be closed and reopened. The right rail starts as agent avatar shortcuts and expands into the full Working team tree. Selecting a parent breadcrumb switches focus to that parent agent.

All options support a native mobile dialog with focus containment, Escape/backdrop close, and an explicit close button. Agent drafts survive layout changes and branch selection. The main-chat composer is a labeled read-only visual reference; only subagent messages are interactive in this study.

## Interaction to review

1. The demo opens paused at 0:08 with running, approval, and completed examples.
2. Open Explorer's tool arguments and output. Send it a note.
3. Advance to 0:12: its tool completes but the note stays **Queued**.
4. Advance to 0:13: the note becomes **Picked up** at the agent's response boundary.
5. Play at 2× or 4× to see approval, test failure, a fix, and a successful recheck.
6. Compare A/B/C without losing sent notes, drafts, or event state. Reset clears the fixture run, sent notes, and drafts.
7. Open Builder and its nested Layout agent. Inspect the ownership tree and, in C, use the parent breadcrumb to return to Builder.
8. Collapse/expand Working team. At 390 px, open its header button and try selecting a branch, messaging in B, and closing with Escape.

Queued notes are scoped to their addressed agent. Tools continue while notes wait. Several notes are picked up together in arrival order. Notes sent after a completed/failed turn receive a simulated follow-up boundary after three timeline seconds. An acknowledgement is explicitly labeled simulated; it does not claim the model executed the note.

## Interfaces

`src/types.ts` defines small transport-independent records: agent identity and parent IDs, events, messages with delivery state, JSON tool arguments, literal output, and timestamps. `applyEvent` consumes normalized, ordered events and deduplicates event IDs. `queueMessage` is the demo's local send boundary; it leaves running tools intact. `advanceTo` drives fixtures only. Revision 2 changes presentation and local UI state, not the event engine.

This is a design study rather than a production transport proposal. A production adapter must replace the fixture clock/send operation with real backend admission and response-boundary acknowledgements, enforce authorization, and preserve server ordering. The test module checks queue isolation/order, tool versus response boundaries, idempotent event handling, approval isolation, and finished-agent follow-ups. Keyboard controls are native buttons, disclosures, selects, dialogs, and text areas. Enter sends; Shift+Enter inserts a newline. Animation respects reduced-motion preferences.

Current backend integration still needs work: `SubagentManager.history(task_id)` can inspect a busy child, but `continue_task` rejects a running child and only reactivates a completed one. These demos do not change that behavior or add a web messaging route. Production integration needs child activity/history endpoints and a root-scoped queue that admits addressed messages after the current response/tool batch, with server-confirmed queued and picked-up events. A chosen layout can then use those events in place of the simulator.
