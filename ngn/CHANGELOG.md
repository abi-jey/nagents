# Changelog

## 0.100.33

- The nagents library, Python ngn client, and runtime adapter now support Python 3.12–3.15. If you use Python 3.11, upgrade to Python 3.12 or newer before installing this release, or stay on a prior compatible release. The native bundle still includes its unchanged CPython 3.13 runtime.

## 0.100.32

- Native updates verify the active bundle once instead of scanning it twice before release discovery.

## 0.100.31

- Native updates refuse to switch releases if the active generation changed during staging.

## 0.100.30

- Hide the successful turn marker in the main native transcript while keeping tool badges and actionable statuses.

## 0.100.29

- Remove the empty row between adjacent compact native tool cards while preserving prose and expanded detail spacing.

## 0.100.28

- Remove the native YOU and NGN transcript headings without changing message content.

## 0.100.27

- Empty native conversations show one short F1 and `/sessions` tip instead of the multi-line welcome. The tip is hidden when it does not fit or the conversation already has content.

## 0.100.26

- The native workspace no longer shows the top version, path and session banner or its divider, leaving more room for the conversation. F1 still opens help.

## 0.100.25

- Managed updates use buffered reads to parse official release metadata, reducing file reads without changing validation or progress output.

## 0.100.24

- The mouse wheel now scrolls the native ngn conversation without moving focus. Scrolling up keeps your place as output streams; scrolling back to the bottom resumes following new output.

## 0.100.23

- Managed ngn updates now report what they are doing on stderr, with a spinner during longer waits on interactive terminals. Redirected output stays plain, and the final result on stdout is unchanged.

## 0.100.22

- Web chat and Live voice keep active runs going when SQLite is temporarily busy during a follow-up inbox check. Queued follow-ups are retried in order even while the root waits for child agents.

## 0.100.21

- Fix #311: name the occupied ngn destination, distinguish permission errors, and show a version-pinned side-by-side retry using a separate bin directory and prefix without replacing the existing ngn.

## 0.100.20

- The native POSIX TUI keeps startup input as a draft without echoing or submitting it while session setup is pending and through the first workspace frame. The native workspace still waits for setup (unlike Python). Handoff is based on the first frame flush and a kernel stdin poll, not a display acknowledgment; an incomplete UTF-8 character gets up to 50 ms to finish.

## 0.100.19

- The native POSIX TUI shows a brief normal-screen startup prelude before opening the workspace. Enter or Space skips it; Escape, Ctrl+C, or Ctrl+Q exits. Printable input can carry into the draft without sending, and reduced motion skips the prelude.

## 0.100.18

- Successful native tool cards now run one foreground-only completion wipe and settle green. Exposed parts keep animating beside an approval dialog, while reduced motion and restored history render the settled style immediately.

## 0.100.17

- Long assistant code fences in the native terminal clip instead of wrapping. Press F8 to focus an overflowing fence and use the left and right arrows to scroll it horizontally.

## 0.100.16

- The native TUI renders assistant Markdown fences with a solid left code border and terminal-default panel colors. Authoritative final text replaces its own streamed response without duplicating it; empty finals keep the streamed text, and user and tool output stay plain.

## 0.100.15

- The native Message composer and caret honor saved painted Python themes. Terminal-default colors and NO_COLOR still work without terminal color queries.

## 0.100.14

- Restore the native --no-animations CLI flag, including unambiguous Python abbreviations. It disables activity motion, tool-card shimmer, and caret blinking for one invocation without changing saved preferences or stopping elapsed timers.

## 0.100.13

- The native TUI now honors the saved Python ngn animation preference, keeping tool-card shimmer, footer motion, and caret blinking off with animations disabled while elapsed timers continue to update.

## 0.100.12

- Animate the active native terminal tool card with a foreground-only teal/lavender sweep that respects reduced motion and stops for approval or completion.

## 0.100.11

- The wide native ngn Agents sidebar adds a collapsed Workspace & context panel for workspace, session, provider, model, and configured authentication mode. Press Tab from the tree to focus the panel and Enter or Space to open it.

## 0.100.10

- The native ngn TUI now has individually expandable tool cards: press F4 to focus, use arrows to select, and press Enter or Space to toggle. The read-only Agents inspector shows tool failure and denial reasons inline.

## 0.100.9

- The native ngn TUI now shows a persistent Agents sidebar above 110 columns, with a collapsible task tree, native status and read-only labels, and exact-run inspection.

## 0.100.8

- The native ngn Message composer now shows a blinking caret at the end of the draft, including after wraps, pastes, and newlines. Set `TEXTUAL_ANIMATIONS=none` for a steady caret.

## 0.100.7

- **Python configuration and auth:** Without `--config`, native ngn reads existing
  Python provider/model settings and honors `read_only`, without writing config
  or importing Python approval or tool policies. Python ChatGPT credentials stay
  separate: if the selected route would use them, configure native ChatGPT and
  sign in to the intended account rather than falling back to another route.
  `auth: "codex"` requires local Codex files and honors their OAuth or API-key
  policy. The welcome screen and `ngn doctor` explain unavailable connections.
  Legacy Foundry/Azure `api_version` routes, Entra auth, and the `assistant.md`
  system prompt are not imported.
- **Logs:** Local lifecycle diagnostics omit prompts, tool payloads, credentials
  and runner stderr; the TUI shows the log path on exit when available. Provider
  HTTP capture (`--wire-log`) is off by default. Its private log redacts transport
  authentication, but payloads can still contain sensitive content.
- **Terminal parity:** Textual-style activity and provider/model footer; a
  filter-first session picker (`F2`, `/sessions`, or bare `/resume`); one
  transcript row per tool call that updates as it progresses; and native slash
  command suggestions that complete a command without executing it.
- **Python history:** On Linux and macOS, normal workspace startup can make a
  one-time copy of supported Python user/assistant text into separate Rust-owned
  read-only archives (host schema 5). The original database is not changed.
  Archives are not synced or resumable; no model context, credentials, approvals,
  or tool state are imported and no actions are replayed. Missing, busy, unsafe,
  unsupported, or WAL databases are skipped without blocking startup. Use
  `ngn upgrade --from BACKUP` with a standalone rollback-journal backup and a
  fresh `--data-dir` if needed. Back up native state before upgrading; use separate
  state or restore a backup to downgrade.

## 0.100.6

- Remove native Rust `--demo`. `ngn doctor` still works without loading a runtime
  or contacting a model; offline acceptance tests use a separate fixture runner.
- Show the reason a prompt could not be sent after Enter, and report the active
  configuration source and model in `ngn doctor`.

## 0.100.5

- Native macOS bundles: install.sh now installs self-contained ngn on Apple silicon and Intel Macs (macOS 11+), alongside Linux x86_64.

## 0.100.4

- Fix: ngn no longer refuses to start when a state parent directory such as ~/.local is group-writable (umask 0002); only world-writable parents are refused.

## 0.100.3

- Fix: the installer no longer rejects group-writable parent directories such as ~/.local/bin (only world-writable or foreign-owned ones), and release publishing restores the installer exec bit.

## 0.100.2 — self-contained installation (2026-11-18)

- Self-contained Linux x86_64 installer with pinned relocatable CPython 3.13.18,
  hashed binary dependency wheels, CA certificates and upstream redistribution
  notices. No Python/pip/Rust/Node setup is needed on the target system.
- Native managed `ngn update --check`/`ngn update`, version pinning, verified
  staging and an atomic current-version switch. Keep prior runtimes, refuse
  foreign executables/unsafe prefixes, and never modify application state/auth.
- Bundled default runners use a version-specific isolated Python with bytecode
  writes disabled; explicit trusted custom runners are still honored.
- Share version 0.100.2 across the reusable core, native application and adapter.
  One tagged CI pipeline publishes the tested core to PyPI before creating the
  sole GitHub release, titled `v0.100.2`, with both exact public core distributions.
  Official bundles bind those published bytes to source and runtime content;
  source-only API availability is not published compatibility.
- Preserve and reconcile the original local Python work without reverting later
  provider, cancellation, streaming and UI fixes already integrated on main.
- `./ngn/dev` builds the optimized binary and installed wheels from current working
  source, isolates development state and runs the extracted application. `--test`
  exercises the real packaged CLI/HTTP/WebSocket/PTY paths, including dirty checkouts.
- Native tool duration excludes its own approval waiting; durable timing snapshots
  keep call attribution and active duration consistent in the TUI and browser.
- Compact bounded durations and recognized tool targets in native client summaries.
- Touch browser Return stays in the draft; deliberate Send/Queue and Stop preserve
  composer focus. Mobile conversation controls, scrollable approval previews and
  non-collapsing decision controls have optional real-Chromium acceptance coverage.
- Native list/search respect bounded workspace `.gitignore` rules, including nested
  origins, negation and explicit starting paths. Protected paths remain excluded.
- The Python library exposes device authentication and guarded storage without a
  harness/UI import, with cooperative cross-process refresh ownership.
- Explicit development ChatGPT connections use a host-generated private store,
  negotiated runtime capability and credentials callback without Codex discovery or
  API-key fallback. Native CLI device login/status/logout use bounded redacted
  frames and shared-host/exclusive-writer ownership. Browser login, live entitlement
  checks and publication of the prerequisite core version remain separate gates.
- Bounded exact-root steering at persisted whole-tool-batch/final boundaries, with
  two-sided optional negotiation and durable pending/offered/applied/uncertain/
  undelivered receipts. Nothing is automatically replayed or relabelled as FIFO.
- Explicit browser Steer active turn and native `/steer TEXT`/F7 receipt controls;
  ordinary Enter/Send still queues next-run input. Reconnect only inspects receipts.
- Host schema 3→4 preserves queues/cursors; back up important state before upgrading
  and use a separate state directory or that backup when downgrading older binaries.
- External development Cargo target bases are namespaced by canonical checkout.
  Both installed archive paths include the checked-in ChatGPT example. Synthetic
  auth gates run explicitly; tag publication refuses a published core without its
  public auth API. Private test roots do not depend on the invoking shell's umask.
- Create each missing default/custom development cache ancestor as 0700 without
  changing existing unsafe paths. Acceptance also launches the extracted offline,
  read-only app in the real cache ancestry, not only independent fixture state.

This release is not a rewritten `v0.100.0` or a claim of full legacy
parity. Production deployment and paid-provider/Codex validation are separate gates.

## Official 0.100.1 — durable input queues

- Rust-owned per-session FIFO, stable idempotent request IDs, 32 pending inputs per
  session/256 per host, admission within existing root/run limits and no backlog bypass.
- TUI F6 and `/queue` controls, busy input acceptance, pause/resume, individual
  removal and clear-and-pause; matching browser, HTTP and durable WebSocket controls.
- Stop/failure/shutdown/restart pause pending work. Explicit resume only dispatches
  unstarted inputs; interrupted runs and completed tool actions are never replayed.
- Preserve each queued run's approval mode; a prior grant cannot authorize a future
  turn. Child sessions remain inspect-only and cannot admit/control root queues.
- Host schema 2→3 migration preserves journals and introduces session-scoped queue
  events with an empty run ID. Runtime protocol v1 is unchanged.
- FIFO/capacity/concurrency/recovery and TUI/web regressions, actual model-fixture
  HTTP/WebSocket flows and real PTY removal/resume/clear/restart acceptance.

Steering, follow-ups, original UI parity and the other migration gaps remain open.

## Official 0.100.0 — monorepo release

- Consolidate the Rust application under `ngn/` in `abi-jey/nagents`, retaining the
  Python core and its independent version. No new GitHub repository is used.
- Roll up both local prototypes below, including Rust supervision and the
  successful-tool-result adapter fix. Those standalone tags were local only.
- Shared pre-commit gates and separate Rust CI/release jobs, real TUI/web acceptance,
  adapter-wheel testing, extracted-install checks, commit-bound manifests and checksums.
- The `v0.100.x` tag series is reserved for Rust; it cannot trigger Python/PyPI publishing.
- Support the tested Python core 0.28.x as well as 0.27.1 through the public adapter boundary.

The following entries record **unpublished standalone prototype history**, not
official GitHub releases. Their original local tags/artifacts remain unchanged.

## 0.100.1 — 2026-11-17

### Added

- Rust-owned subagent admission, isolated sessions, immutable parent/root topology,
  per-root depth/concurrency/execution budgets and child execution deadlines.
- `to_agent` foreground/background delegation, `agent_status`, `agent_wait` and
  `agent_cancel`; result/control authorization is scoped to the immediate parent.
- Subtree cancellation and structured cleanup before a parent's terminal event;
  unjoined active children cause failure rather than being silently orphaned.
- TUI F5 agent inspection and Ctrl+X child-only cancellation; browser agent catalog,
  independent child transcript streams, cancellation and attributed child approvals.
- Metadata-only task HTTP endpoint, durable task lifecycle events and interrupted
  child recovery without replay. Host database schema upgrades from 1 to 2.
- Supervisor policy/concurrency/recovery tests and matching TUI, browser, real
  HTTP/WebSocket and PTY regressions using the actual nagents adapter.

### Fixed

- Translate empty wire errors to `ToolResultEvent.error=None`: nagents otherwise
  treated successful Rust tool results as errors in saved/model context. The new
  background-result fixture verifies that the model receives actual tool JSON.
- A cancelled activation does not start a fresh runtime during graceful cleanup.
- Task inspection retains the selected identity while new children are admitted.

Full legacy queues, follow-ups, profiles/settings, voice, channels and original UI
parity remain incomplete. See the migration checklist, not a blanket parity claim.

## 0.100.0 — 2026-11-17

First standalone Rust migration baseline.

### Added

- Native Rust harness admission, per-workspace sessions, durable run/event journal,
  idempotent prompt submission and interrupted-run recovery.
- Ratatui terminal client and Axum local HTTP/WebSocket server with embedded web UI.
- Strict Agent Runtime Protocol v1 with schema-defined tool catalogs, exact
  `(run_id, call_id, name)` binding, cancellation and supervised runner processes.
- Optional nagents adapter using public agent/executor APIs, without importing the
  Python harness, TUI or web server.
- Native Rust file inspection, listing, search, create/replace/edit and shell tools;
  bounded I/O, stale-file checks, read-only mode and one-time approval enforcement.
- Rust-only offline demo and versioned headless JSON events.
- Unit/integration tests and local fixture smoke tests exercising the real CLI,
  nagents runtime, HTTP/WebSocket behavior and terminal interaction.

### Scope

This is a runnable architectural baseline, **not a one-to-one port of every legacy
feature**. Subagent supervision, profiles/settings, MCP/skills, original React/UI
parity, queues, OAuth, A2A, channels and voice remain on the
[migration checklist](docs/migration.md). Linux is the initial validated platform.
No legacy nagents code/configuration/database is removed or migrated implicitly.
