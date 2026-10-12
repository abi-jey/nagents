# Subagent interaction studies · revision 8

Two compact chat presentations share one activity tree and one message box. This is a standalone React demo, with simulated agents and tools. No production configuration or provider calls change.

From `src/nagents/web-ui`:

```sh
npm run demo:subagents
npm run demo:subagents:build
npm run demo:subagents:test
```

Preview: `http://127.0.0.1:4175/`. A Activity uses `#more`; B Timeline uses `#drawer`. Older variant hashes fall back to A. Build output remains `dist/subagent-concepts/`.

## Chat and composition

Activity and Timeline keep spawn parent/time and explicit Running, Waiting, Done, or Failed state. Completed entries are short event rows (34 px desktop / 46 px mobile), without a repeated result sentence. Active/waiting entries retain one short activity line or wait reason. No Agent activity heading, New tags, or grouped spawn banner.

The existing footer message box follows the selected conversation. Selecting Explorer addresses Explorer; selecting the root Assistant addresses the main conversation. The recipient is shown in the label and send button. Each agent keeps its own draft. Sending stays in that conversation, uses its own queue, clears only its draft, and reveals sent messages if the Tools filter was active. Main and child queue behavior is unchanged. During the short closing animation the composer still names and addresses the visible child, then switches to Assistant.

## One tree

The main Assistant is the normal root node, rendered with the same activity row, model, context, settings, selection, and scroll behavior as other agents. No pinned root section, originating-message headers, Earlier work labels, archive drawers, or separate old-work groups remain. Redundant row activity/result descriptions have been removed from the side pane.

Siblings are ordered by the newest event anywhere in their branch. `activityTree.ts` computes each node's own activity and the maximum across all descendants, including hidden ones. Message arrival/pickup, tool start/result, acknowledged status events, and spawns contribute; time passing alone and future scheduled work do not. Ties are deterministic, and malformed parent cycles remain inspectable.

The first recent candidates and their ancestors load initially. Scrolling to the end loads more; the Load more control is also keyboard-accessible. The root is depth 0; children at depth 1 and grandchildren at depth 2 show by default. Deeper descendants stay under their parent until expanded. Explicit expansion loads its immediate children even when they are older than the current page. Selecting a deeper conversation reveals its ancestry; explicitly collapsing it leaves the focused conversation and composer recipient unchanged.

## Arrival motion

A new agent slides and fades into its tree row with a short highlight and a drawing connector. Its chat entry settles into place with matching restrained motion. App-owned timestamps cap the effect at 1.4 seconds even when paused. Memoized elapsed delays prevent typing, variant switches, and responsive remounts from restarting it. Old entries loaded later do not animate as new spawns. Reduced motion disables the movement; screen readers receive a concise spawn announcement. Motion never moves focus or scrolls the chat.

## Try it

1. Reset, then Show first spawn (0:02). Use Next event to see approval waiting and Builder spawning Layout.
2. Next delegation reaches 0:52. Show mixed states reaches 1:00: Running, Waiting, and Done appear together.
3. Reviewer → Security is expanded by default. Expand Security to see Access audit at depth 3, or select it from chat. Its 1:01 tool result updates the recency of the entire Reviewer branch.
4. Select agents in chat or the tree. Type drafts in several conversations and switch back; the single message box restores each draft.
5. Send a note during a tool call. It remains queued until the next response. Sending to a finished agent schedules another demo response.
6. Show completions and scroll the tree to load older agents. They stay in their parent branches, without archive sections.

Context windows and token estimates are illustrative. Saved provider/model/compaction settings apply on Reset/Replay and do not rewrite the accepted running configuration. Main and child state, drafts, loaded tree count, and branch expansion survive chat-variant changes.
