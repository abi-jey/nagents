# Native ngn migration and safety

The [native installer](native-ngn.md) installs a **separate** Rust application,
not an in-place conversion of the existing Python ngn client. The reusable
Python nagents library and feature-rich Python CLI/TUI/web client remain intact.
Their similar command names do not imply matching configuration or HTTP APIs.

## Separate installation, configuration and state

- Managed binaries/runtime: `$XDG_DATA_HOME/ngn-harness`, or
  `~/.local/share/ngn-harness`; the command defaults to `~/.local/bin/ngn`.
- Native host state: `$XDG_DATA_HOME/ngn-rust/workspaces/<workspace-hash>/`, or
  `~/.local/share/ngn-rust/...`; runtime conversation storage is separately owned.
- Existing Python client state/config remains separate. Normal native startup
  on Linux and macOS can make a one-time copy of supported Python transcripts
  as read-only archives in the selected native workspace. It never opens the
  original as the native store or imports credentials, tool state, plugins or
  agent definitions. See [Python session history](https://github.com/abi-jey/nagents/blob/main/ngn/README.md#python-session-history)
  for supported paths and the manual backup fallback.
- Select a distinct `--bin-dir` if a Python/foreign `ngn` is already installed,
  and use the explicit binary path. Installation never overwrites that command
  or repairs unsafe existing paths with chmod.

Use an explicit native JSON file and `-C` for the native workspace. Python
`providers.json`, layered preferences, `ngn login`, `/voice` and plugins belong
to the [Python client](ngn.md), not the native installer guide.

## Stop and back up before opening important state

The native host uses **SQLite schema 5** for read-only archives in addition to
schema 4's steering receipts. Before starting a newer host on important data:

1. Stop the existing host and its owned runners cleanly.
2. Back up the complete selected native workspace state and runtime-owned
   conversation storage consistently. Preserve any explicit config/auth files
   separately and privately; do not upload credentials with diagnostics.
3. Review the release's schema instructions, then start the intended native
   binary with the intended workspace/state paths.

Older binaries refuse newer schemas. Downgrade only using a separate compatible
state directory or a consistent pre-upgrade backup; **never edit SQLite
`user_version`** to force it. The updater retaining an older generation does not
make an upgraded database backward-compatible. Updates do not migrate a legacy
Python database, restart a host, replay interrupted runs/tools, or change
credentials, configuration or workspace files.

## Native scope and remaining gaps

Rust owns native tools, approvals, host lifecycle, event persistence, FIFO queues,
subagent supervision, TUI and loopback HTTP/WebSocket serving. The optional
nagents adapter owns provider/model integration and opaque conversation storage;
it is not the old Python harness embedded behind the Rust UI.

The existing React/Textual UI, full settings/model editors, agent definitions,
skills/MCP/custom Python plugins, advanced conversation/media actions, voice,
channels, A2A, remote deployment, and legacy import are **not all migrated**.
Do not use Python container/Kubernetes or voice recipes with the native binary,
and do not replace an existing production installation on a parity assumption.
Read the maintained source documents for exact implemented features and gaps:

- [Native migration inventory](https://github.com/abi-jey/nagents/blob/main/ngn/docs/migration.md)
- [Native installation/update contract](https://github.com/abi-jey/nagents/blob/main/ngn/docs/installation.md)
- [Native 0.100.2 release notes](https://github.com/abi-jey/nagents/blob/main/ngn/docs/releases/0.100.2.md)
- [Protocol and ownership](https://github.com/abi-jey/nagents/blob/main/ngn/docs/agent-runtime-protocol.md)
- [Native security boundary](https://github.com/abi-jey/nagents/blob/main/ngn/docs/security.md)

These are GitHub source links because `ngn/docs/` is outside this site's
`docs_dir`. Local fixture/Chromium checks are not paid-provider eligibility,
cross-platform installer validation, full feature parity, or production rollout.
Approved shell commands and trusted runtime code still run as your OS user:
**neither application is an OS sandbox**.
