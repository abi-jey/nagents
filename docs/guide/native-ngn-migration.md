# Native ngn migration and safety

The [native installer](native-ngn.md) installs a **separate** Rust application,
not an in-place conversion of the existing Python ngn client. The reusable
Python nagents library and the Python CLI/web client remain intact; the Python
terminal UI has been removed in favor of this native client.
Their similar command names do not imply matching configuration or HTTP APIs.

## Separate installation, configuration and state

- Managed binaries/runtime: `$XDG_DATA_HOME/ngn-harness`, or
  `~/.local/share/ngn-harness`; the command defaults to `~/.local/bin/ngn`.
- Sessions, queues and approvals belong to the Python engine and live in the
  normal ngn data directory, like the Python client's. Releases up to 0.100.46
  kept a separate Rust host store in `$XDG_DATA_HOME/ngn-rust/workspaces/<hash>/`;
  0.100.47's engine copies each workspace's old history from there once, as
  read-only sessions, and leaves the old files untouched.
- Select a distinct `--bin-dir` if a Python/foreign `ngn` is already installed,
  and use the explicit binary path. Installation never overwrites that command
  or repairs unsafe existing paths with chmod.

The native client reads the same `providers.json`, preferences and sign-in as
the Python client, because both run the same engine.

## Back up before upgrading from 0.100.46

0.100.47 stops using the Rust host database. Before updating, stop running
`ngn` clients and keep a copy of `~/.local/share/ngn-rust` if its history
matters to you. The engine only reads it; older binaries can still open it.
Updates do not restart running clients, replay interrupted runs or tools, or
change credentials, configuration or workspace files.

## Native scope and remaining gaps

Since 0.100.47 the Python engine owns tools, approvals, lifecycle, event
storage, FIFO queues, steering and subagents. The Rust binary is the terminal
client and the `ngn serve` browser gateway; see
[ADR 0001](../adr/0001-python-core-rust-tui.md). The browser workspace still
lacks some Python web features, such as voice and channels, and
remote deployment is not supported. Do not replace an existing production
installation on a parity assumption.

- [Engine migration plan](https://github.com/abi-jey/nagents/blob/main/ngn/docs/migration.md)
- [Native installation/update contract](https://github.com/abi-jey/nagents/blob/main/ngn/docs/installation.md)
- [Native 0.100.2 release notes](https://github.com/abi-jey/nagents/blob/main/ngn/docs/releases/0.100.2.md)
- [Native security boundary](https://github.com/abi-jey/nagents/blob/main/ngn/docs/security.md)

These are GitHub source links because `ngn/docs/` is outside this site's
`docs_dir`. Local fixture/Chromium checks are not paid-provider eligibility,
cross-platform installer validation, full feature parity, or production rollout.
Approved shell commands and trusted runtime code still run as your OS user:
**neither application is an OS sandbox**.
