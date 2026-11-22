# Native ngn: installer and usage

The native Rust `ngn` (pronounced *"engine"*—TUI, headless CLI, local browser client and updater) ships as a
self-contained bundle: CPython 3.13.18, the matching `nagents` (pronounced *"N-agents"*) engine, locked
dependency wheels and CA certificates. No Python, pip, Rust, Node or compiler is needed.

Supported:

- **Linux x86_64**, glibc 2.39+ (Ubuntu 24.04 or newer)
- **macOS 11+** on Apple silicon (`macos-arm64`) or Intel (`macos-x86_64`); an x86_64
  shell under Rosetta still gets the native Apple silicon build

## Install

```sh
curl -fsSL https://github.com/abi-jey/nagents/releases/latest/download/install.sh | sh
```

This installs the latest release to `~/.local/share/ngn-harness` and links
`~/.local/bin/ngn`. The bootstrap verifies the native installer's SHA-256 against the
release's `SHA256SUMS` before running it. To pin a version, replace `latest/download`
with `download/vX.Y.Z`.

```sh
ngn doctor          # the engine's Python and diagnostics; no model request
```

The installer checks the destination `~/.local/bin/ngn` (or `DIR/ngn` with
`--bin-dir DIR`), not the first `ngn` on `PATH`. It won't replace an unmanaged entry there,
including the Python client or a symlink. To install alongside it, use an unused
bin directory and a separate unused prefix, for example:

```sh
curl -fsSL https://github.com/abi-jey/nagents/releases/latest/download/install.sh | sh -s -- \
  --prefix "$HOME/.local/share/ngn-native" --bin-dir "$HOME/.local/ngn-native-bin"
"$HOME/.local/ngn-native-bin/ngn" doctor
```

Use the full path to run or update this installation without changing which
`ngn` your shell finds first. If you started with a version-pinned installer, use
its printed retry command to keep that same version instead of fetching `latest`.
The installer refuses world-writable or foreign-owned parent directories.

### Manual install

Pick the installer for your platform: `linux-x86_64`, `macos-arm64` or `macos-x86_64`.

```sh
platform=linux-x86_64
base=https://github.com/abi-jey/nagents/releases/latest/download
curl -fsSL -O "$base/ngn-installer-$platform" -O "$base/SHA256SUMS"
sha256sum --ignore-missing -c SHA256SUMS   # macOS: shasum -a 256 --ignore-missing -c SHA256SUMS
chmod +x "ngn-installer-$platform" && "./ngn-installer-$platform" install
```

For an offline bundle, add `--bundle FILE --sha256 HASH --version X.Y.Z`.

## Sign in and run

ngn runs the bundled Python engine for you and uses the same provider settings as
the Python client (`~/.config/ngn/providers.json`). Sign in once, then start ngn in
a workspace:

```sh
ngn auth login                          # choose ChatGPT, OpenAI, Anthropic, OpenRouter, Gemini or custom
ngn auth status
ngn -C /path/to/workspace               # TUI
ngn -C /path/to/workspace serve         # browser
```

API keys stay in the environment or the login store, never in a config file.
`--config FILE` selects an explicit engine configuration; see
[configuration](ngn-configuration.md).

## Update

```sh
ngn update --check   # report a newer stable release
ngn update           # verify, stage and atomically switch; previous versions are kept
```

Updates never touch configuration, credentials, workspaces or conversations, and never
restart a running engine. Stop running ngn clients before updating.

## Paths

| Item | Default |
| --- | --- |
| Installation | `$XDG_DATA_HOME/ngn-harness` or `~/.local/share/ngn-harness` |
| Command | `~/.local/bin/ngn` |
| Sessions and engine state | `~/.local/share/ngn` (shared with the Python client) |
| Rust host history from 0.100.46 and earlier | `~/.local/share/ngn-rust/` (read once, never changed) |

The native and [Python ngn client](ngn-installation.md) share configuration, sign-in and
sessions, because both run the same engine. See [native migration and safety](native-ngn-migration.md) for
feature gaps.

## From source

```sh
npm ci --prefix src/nagents/web-ui
./ngn/dev            # build and launch with existing provider settings
./ngn/dev --test     # build and run the installed-candidate acceptance suite
```

Requires Python 3.13, Rust 1.93+, a C compiler and Node.js (24 in CI). See
[native development](https://github.com/abi-jey/nagents/blob/main/ngn/README.md#development).
