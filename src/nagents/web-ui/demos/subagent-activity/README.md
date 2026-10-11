# Subagent interaction studies

Three **standalone React demos** for choosing a UI direction. They do not connect to ngn, run tools, call providers, or reveal model reasoning. All messages, paths, commands, results, approvals, and timings are illustrative fixtures.

From `src/nagents/web-ui`:

```sh
npm run demo:subagents
npm run demo:subagents:build
npm run demo:subagents:test
```

Preview: `http://127.0.0.1:4175/`. Direct choices: `#tree`, `#workspace`, `#board`. The build is written to repository `dist/subagent-concepts/`; it does not replace the production UI build. No additional packages or remote assets are required.

## Directions

- **A — Conversation tree.** Delegation is a branch in the main conversation. Each branch exposes current activity and expands into its messages, tool evidence, assignment, and composer. Nested work remains under its owner.
- **B — Agent workspace.** A persistent agent tree selects a dedicated activity workspace. The parent conversation stays in a separate rail. On narrow screens the agent selector becomes a compact grid.
- **C — Activity board.** Cards show all agents' current work at once, including attention states. A lower focused thread exposes the selected agent's evidence and composer beside the activity stream. The attention filter can isolate failures and approval waits.

## Interaction to review

1. The demo opens paused at 0:08 with running, approval, and completed examples.
2. Open Explorer's tool arguments and output. Send it a note.
3. Advance to 0:12: its tool completes but the note stays **Queued**.
4. Advance to 0:13: the note becomes **Picked up** at the agent's response boundary.
5. Play at 2× or 4× to see approval, test failure, a fix, and a successful recheck.
6. Compare layouts without losing notes or event state. Reset clears the fixture run and notes. Switching layouts does not call any backend.

Queued notes are scoped to their addressed agent. Tools continue while notes wait. Several notes are picked up together in arrival order. Notes sent after a completed/failed turn receive a simulated follow-up boundary after three timeline seconds. An acknowledgement is explicitly labeled simulated; it does not claim the model executed the note.

## Interfaces

`src/types.ts` defines small transport-independent records: agent identity and parent IDs, events, messages with delivery state, JSON tool arguments, literal output, and timestamps. `applyEvent` consumes normalized, ordered events and deduplicates event IDs. `queueMessage` is the demo's local send boundary; it leaves running tools intact. `advanceTo` drives fixtures only. A production adapter must replace the fixture clock/send operation with real backend admission and response-boundary acknowledgements, enforce authorization, and preserve server ordering.

This is deliberately a design study rather than a production transport proposal. The test module checks queue isolation/order, tool versus response boundaries, idempotent event handling, approval isolation, and finished-agent follow-ups. Keyboard controls are native buttons, disclosures, selects, and text areas. Enter sends; Shift+Enter inserts a newline. Animation respects reduced-motion preferences.

Current backend integration still needs work: `SubagentManager.history(task_id)` can inspect a busy child, but `continue_task` rejects a running child and only reactivates a completed one. These demos do not change that behavior or add a web messaging route. Production integration needs child activity/history endpoints and a root-scoped queue that admits addressed messages after the current response/tool batch, with server-confirmed queued and picked-up events. A chosen layout can then use those events in place of the simulator.
