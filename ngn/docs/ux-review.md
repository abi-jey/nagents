# Native TUI UX review and roadmap

Status: living document, started with 0.100.44. It records what users lost in
the move from the Python Textual UI to the native Rust `ngn`, what has been
fixed, and what is next. The bar is the maturity of terminal agents such as
opencode and Codex CLI: a first-time user should understand every screen
without reading docs, and an experienced user should never wait on the UI.

## Principles

1. **Say what happened and what to do.** Status text is a sentence for a
   person, not an enum name, an internal ID or a wire field.
2. **One glance per item.** A tool call is one header row: disclosure marker,
   outcome icon, tool, target, time. Details are one click (or F4) away.
3. **The conversation is the product.** Chrome (rails, panels, banners) only
   appears when it has content or the user asked for it.
4. **Approvals show exactly what will run**, in the shape a developer reads
   every day: unified diffs and the literal command. Nothing is hidden or
   truncated; oversized previews are refused, not shortened.
5. **Keys are forgiving.** Single-letter keys where there is no text input,
   a short guard against typed-ahead approvals, and Esc always backs out.
6. **Fail fast and visibly.** Provider retries are announced as they happen;
   a failure ends the turn with a specific reason instead of a silent loop.
7. **Copy gives back what you see.** Mouse selection copies the logical text,
   not the soft-wrapped screen rows or the gutter.

## Findings (before 0.100.44)

| Area | Problem | Severity |
| --- | --- | --- |
| Provider errors | A 429 or 5xx after retries re-ran the model round for minutes with no message; the final error was generic. | Blocker |
| Approvals | The preview printed the entire file twice as `BEFORE:`/`AFTER:` in a full-screen box; keys were Ctrl+Y / Ctrl+D. | High |
| Assistant prose | Markdown was shown raw (`**bold**`, `# heading`, list markers). | High |
| Tool cards | Headers ended in words such as `complete` and `Approval waiting · active · 9ms` with a frozen timer; `>`/`v` markers. | Medium |
| Layout | An empty Agents rail took a third of wide terminals in every session. | Medium |
| Messages | User turns were hard to tell apart from assistant output. | Medium |
| Copy | Selecting text copied soft-wrap breaks and padding. | Medium |
| Help | One flat list mixing F-keys, slash commands and modes, with inconsistent indentation. | Medium |
| Empty state | A single dim tip; no version, workspace or next step. The composer had no hint. | Low |
| Turn outcomes | `— Failed`, `— Cancelled` and `[Input queue paused · resume explicitly]`. | Low |
| Slash menu | Only four of ten commands visible at a time. | Low |

## Shipped in 0.100.44

- **Provider failures:** the runtime adapter stops a turn on the first
  unrecovered provider error and explains it from a fixed, allowlisted set
  (credentials, access, model or endpoint, rate limit, server error,
  network categories). Each automatic retry is announced with its HTTP
  status, delay and attempt number. Upstream payloads are never echoed.
- **Approvals:** a compact panel anchored above the composer, titled
  `Allow edit to path?` or `Run shell command?`. Writes and edits show a
  unified diff with three lines of context (new files diff against
  `/dev/null`); shell calls show `$ command`, the timeout and the workspace.
  **y**/**Enter** allows once, **n** denies, **Esc** stops the turn, and
  PgUp/PgDn scroll. Plain y/Enter are ignored for 300 ms after the panel
  appears.
- **Approval hardening** (from the security review): a plain y/Enter only
  counts after 300 ms with no other keypress, so typing never approves;
  auto-repeat keys never decide. If the panel cannot show at least three
  lines it covers the whole screen, and Allow is refused until it can.
  Invisible and bidirectional characters in commands, paths and diffs are
  shown as `⟨U+202E⟩` instead of reordering the text, and tool paths with
  control characters (such as newlines) are rejected. The footer says when
  more of the preview is below.
- **Markdown** rendering for assistant prose (pulldown-cmark, no extra
  features) with soft-wrap metadata so copy stays faithful.
- **Tool cards:** `▸`/`▾` markers, `✓`/`✗`/`⊘` outcome icons, no `complete`
  suffix, and *waiting for approval* without a timer.
- **User messages** carry a left accent bar (plain bar under `NO_COLOR`).
- **Agents rail** only appears once a run delegates, or when Ctrl+T focuses it.
- **Welcome screen, composer placeholder, grouped help, full slash menu** and
  plain turn-outcome and queue notices.

## Roadmap

Ordered by user impact relative to effort.

1. **`/model` switching** within a session, with the provider and model in the
   footer as a clickable target.
2. **"Always allow" for a tool or a command prefix** in the approval panel,
   scoped to the session and listed in `/permissions`. Today every call needs
   its own approval.
3. **Streaming tool output** for shell calls (last few lines live in the
   card), instead of a silent spinner until completion.
4. **Context and cost meter** in the footer (tokens used / window), with a
   warning before compaction.
5. **Themes:** light/dark detection exists for the composer; extend it to the
   transcript palette and expose `/theme`.
6. **Diff-aware tool details:** expanded write/edit cards show the same
   coloured diff as the approval panel.
7. **File mentions** (`@path` completion) and image paste.
8. **Session titles** generated from the first prompt; the picker currently
   shows raw prompts and IDs.
9. **Undo the last turn's file changes** (`/undo`), using the diffs the
   approval flow already produces.
10. **Accessibility:** a plain mode (`--plain`) without box drawing or
    animation, friendly to screen readers.

## How to evaluate a UX change

- Run a scripted OpenAI-compatible fixture server and drive the TUI in tmux
  at 80×24, 100×30 and 160×45, capturing panes before and after.
- Check `NO_COLOR=1` and a narrow (≤ 42 column) terminal.
- Run `scripts/smoke.py` (real PTY, mouse and OSC 52), then
  `composer_smoke.py`, `steering_smoke.py` and `fifo_fault_smoke.py` with
  `--binary`.
- Prefer assertions on painted cells in `TestBackend` tests over string
  snapshots of internal state.
