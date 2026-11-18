# Native ngn: installer and usage

The installer lives in
[`ngn/install.sh`](https://github.com/abi-jey/nagents/blob/main/ngn/install.sh).
It is a small version-bound shell bootstrap for the **native** `ngn install`
implementation in
[`ngn/crates/ngn/src/install.rs`](https://github.com/abi-jey/nagents/blob/main/ngn/crates/ngn/src/install.rs).
The same Rust application provides the terminal UI, headless CLI, local browser
host and explicit updater.

!!! warning "0.100.2 development — download assets not yet published"
    The public commands below become usable **only after** the official
    `v0.100.2` release is published. Until then, these release URLs will fail;
    running `sh ngn/install.sh` from a checkout does not avoid that requirement.
    Published `0.100.0` and `0.100.1` are immutable and have no bundled installer.
    Use [current source development](#try-current-source-now) or an explicitly
    supplied [local bundle](#localoffline-bundle-preview) in the meantime.

## Choose the native bundle deliberately

| Native Rust ngn | Existing Python ngn client |
| --- | --- |
| Managed, immutable native release generations | Python virtual environment / Poetry / optional extras |
| Rust tools and approvals; optional nagents agent/model runtime | Existing Python harness, Textual TUI and React frontend |
| Explicit native JSON via `--config`; `-C /path/to/workspace` | Layered config, `providers.json`, `--workspace`, agent definitions |
| `ngn --config native.json auth login` | `ngn login`, `/login`, `/provider`, voice and plugin workflows |
| Native state under `ngn-rust` | Python client state under `ngn` |

Both executables are called **`ngn`**. Select their explicit paths when both are
installed; similar flag names do not imply compatible schemas or APIs. The
[Python client guides](ngn-installation.md) and [Python library quick start](../getting-started/quickstart.md)
remain valid for those products. Native feature gaps, state backups and trust
boundaries are documented in [native migration and safety](native-ngn-migration.md).
This bundle is not a full legacy-parity or production-replacement claim.

## Requirements and bundle contents

The initial installer target is **Linux x86_64 with glibc 2.39 or newer**,
matching the Ubuntu 24.04 native build. macOS, Windows, ARM and musl-only Linux
are not validated bundle targets. Python-client macOS support does not extend
this native installer support matrix.

The complete bundle contains the native CLI/browser assets, **CPython 3.13.18**,
the matching **nagents core and adapter 0.100.2**, **12 locked dependency wheels**,
CA certificates, manifests and redistribution notices. No end-user Python,
pip, virtualenv, Rust, Node or C compiler installation is needed. Installing
does not log in, call a provider, or import existing configuration/state.
Building from source is different and requires development tools.

## Public installation after v0.100.2 publication

### Version-bound shell bootstrap

```sh
curl --proto '=https' --tlsv1.2 -fsSL \
  https://github.com/abi-jey/nagents/releases/download/v0.100.2/install.sh | sh
```

Review the script before executing it if required by your environment. The
bootstrap requires standard shell utilities, `curl`, and `sha256sum` or
`shasum`. It verifies the release's native installer before running it; it
never invokes Python, pip or tar. It is bound to `0.100.2`, not a moving branch.

### Download and verify the native installer directly

In a new private download directory, after publication:

```sh
set -eu
umask 077
download_dir=$(mktemp -d)
cd "$download_dir"
base=https://github.com/abi-jey/nagents/releases/download/v0.100.2
curl --proto '=https' --tlsv1.2 -fSL "$base/SHA256SUMS" -o SHA256SUMS
curl --proto '=https' --tlsv1.2 -fSL "$base/ngn-installer-linux-x86_64" \
  -o ngn-installer-linux-x86_64
awk '$2 == "ngn-installer-linux-x86_64" {print}' SHA256SUMS > installer.SHA256SUMS
test "$(wc -l < installer.SHA256SUMS)" -eq 1
sha256sum --check installer.SHA256SUMS
chmod 700 ./ngn-installer-linux-x86_64
./ngn-installer-linux-x86_64 install --version 0.100.2
```

Stop on any failed download or checksum check; do not run an unverified file.
The native installer performs the bundle's download, hashing and extraction
itself without shell download/hash/extraction helpers. An already built native
`ngn` also exposes `ngn install --version 0.100.2`; that command is **not** a
Python-client subcommand. Release-bound SHA-256 verification depends on trust
in the publisher/HTTPS delivery; it is not an independent code signature.

## Paths, coexistence and first launch

Defaults:

| Item | Default |
| --- | --- |
| Managed installation | `$XDG_DATA_HOME/ngn-harness`, or `~/.local/share/ngn-harness` |
| Command symlink | `~/.local/bin/ngn` (not changed by `XDG_DATA_HOME`) |
| Native workspace state | `$XDG_DATA_HOME/ngn-rust/workspaces/<workspace-hash>/`, or `~/.local/share/ngn-rust/...` |
| Existing Python state | `$XDG_DATA_HOME/ngn`, or `~/.local/share/ngn`; never implicitly imported |

Add `~/.local/bin` to `PATH` if desired, or invoke the installed command by path:

```sh
"$HOME/.local/bin/ngn" doctor
"$HOME/.local/bin/ngn" --demo -C /path/to/workspace
"$HOME/.local/bin/ngn" --demo -C /path/to/workspace run --json "hello"
"$HOME/.local/bin/ngn" --demo -C /path/to/workspace serve
```

`doctor` starts no Python runner and makes no network request. The offline demo
uses scripted text and native tools, makes no model/network requests and never
edits workspace files. It **does** persist its own conversations. Use a new
`--data-dir /private/path/native-preview-state` for intentionally isolated state.
The browser server defaults to `http://127.0.0.1:8765`; use the exact printed
address. Native serving is loopback-only, not the Python client's remote
deployment recipe or a multi-user service.

### If another ngn is already installed

The installer **refuses** a foreign executable/symlink. It does not overwrite a
Python/package-manager command, repair permissions with chmod, or migrate data.
Choose a new private prefix and a distinct bin directory, then use that path:

```sh
./ngn-installer-linux-x86_64 install --version 0.100.2 \
  --prefix "$HOME/.local/share/ngn-native-preview" \
  --bin-dir "$HOME/.local/ngn-native-bin"
"$HOME/.local/ngn-native-bin/ngn" doctor
"$HOME/.local/ngn-native-bin/ngn" --demo -C /path/to/workspace
```

Use dedicated, non-overlapping directories, not existing unrelated state.
Unsafe or unmanaged nonempty paths are rejected rather than silently reused.
Keep the Python environment's explicit `ngn` path for the old client.

## Configure the bundled agent runtime

Copy the installed `config.example.json` or `config.chatgpt.example.json` from
the managed prefix's `current/` directory into a private config location; the
source templates are also available on
[GitHub](https://github.com/abi-jey/nagents/blob/main/ngn/config.example.json).
Select an account-supported model; `YOUR_MODEL_ID` is a placeholder:

```json
{
  "config_version": 1,
  "backend": "nagents",
  "provider": "my-provider",
  "providers": {
    "my-provider": {
      "kind": "openai_compatible",
      "model": "YOUR_MODEL_ID",
      "api": "auto",
      "base_url": "",
      "api_key_env": "OPENAI_API_KEY"
    }
  }
}
```

Supply the referenced API key securely in the process environment, never as a
JSON value, command argument, browser value or URL. `.env` is not automatically
loaded. Then run:

```sh
ngn --config /private/path/native.json -C /path/to/workspace doctor
ngn --config /private/path/native.json -C /path/to/workspace
# Or use the same host/runtime through its local web UI:
ngn --config /private/path/native.json -C /path/to/workspace serve
```

With the managed bundle, **omit `runner`**: the host selects its canonical,
version-specific embedded Python with isolated `-I -B` imports and the matching
adapter. No separate pip install is needed. Only source/small-native archives
need an explicitly prepared runtime environment and an absolute runner argv,
for example `"runner": ["/absolute/path/.venv-native/bin/python", "-I", "-B", "-m", "ngn_nagents"]`.
An explicitly trusted runner is not silently replaced by the installed bundle.

For an eligible ChatGPT subscription, use the separate
[ChatGPT template](https://github.com/abi-jey/nagents/blob/main/ngn/config.chatgpt.example.json)
(`kind: "openai"`, `auth: "chatgpt"`) and explicitly initiate login:

```sh
ngn --config /private/path/native-chatgpt.json auth login
ngn --config /private/path/native-chatgpt.json auth status
```

Follow the printed device authorization in your own browser. Installation and
`--demo` need no login; local fixture tests do not prove subscription eligibility.
See the [native authentication contract](https://github.com/abi-jey/nagents/blob/main/ngn/docs/chatgpt-auth.md).
Do not substitute Python `ngn login`, `providers.json`, or voice instructions.

## Explicit updates

After a managed release is available:

```sh
ngn update --check
ngn update
# Pin only a version that has actually been published; 0.100.2 is the initial bundle.
ngn update --version 0.100.2
```

`--check` queries release metadata without downloading/executing a release
payload or activating a version. Stable discovery checks compatible official
`v0.100.x` manifests; shared `v0.100.2` includes both core and native artifacts.
Alpha/dev versions are not native update candidates. No future `0.100.3`
download is promised. Source, pip and unmanaged executables cannot self-replace.
Older-version downgrades and same-version replacement with different content
are refused; an already-current version is not a reinstall mechanism.

The updater validates/stages the complete payload and runtime before atomically
switching `current`; failed validation retains the prior selection. Updates are
locked, previous generations are retained, and running hosts keep their original
generation's Python path rather than following the new symlink mid-run.
The updater does **not** restart a host or change configuration, auth stores,
workspaces or databases. Stop the host and follow the release's backup/schema
instructions before starting a newer host against important state; see
[migration and safety](native-ngn-migration.md).

## Try current source now

From the repository root on Linux x86_64, with **Python 3.13, Rust 1.93+ and a C
compiler** available:

```sh
./ngn/dev
./ngn/dev serve --port 18768
./ngn/dev --test
```

Without an explicit `--config`, this launcher selects the offline demo itself;
you do not need to append `--demo`. `./ngn/dev --help` explains the wrapper
without building/launching. The launcher rebuilds locked optimized native code,
builds/installs fresh non-editable core/adapter wheels in a new isolated runtime,
and runs the explicit extracted binary, including uncommitted working source.
It does not update a global Python client or pull/publish a release. No Node
build is needed for the native embedded browser UI. Development builds may need
network access for build dependencies; offline demo operation is separate.

Its artifact/state cache is `$XDG_CACHE_HOME/ngn-rust-dev/<checkout-hash>/`
(default `~/.cache/...`), overridable with `NGN_DEV_CACHE`;
`NGN_DEV_PYTHON` selects the bootstrap interpreter. The cache must have safe
ancestry; existing paths are not chmod-repaired. `--test` uses candidate-owned
offline state and local fixtures, not the operator's provider/auth/state.
Source success does not substitute for official published-core/bundle gates.
See [native development](https://github.com/abi-jey/nagents/blob/main/ngn/README.md#development).

## Local/offline bundle preview

If you already have a trusted built native installer and local complete bundle,
provide the bundle path **and its caller-supplied lowercase SHA-256** explicitly:

```sh
./ngn-installer-linux-x86_64 install \
  --bundle /private/delivery/ngn-v0.100.2-linux-x86_64-bundle.tar.gz \
  --sha256 "$EXPECTED_SHA256" --version 0.100.2 \
  --prefix "$HOME/.local/share/ngn-local-1002-preview" \
  --bin-dir "$HOME/.local/ngn-local-1002-bin"
"$HOME/.local/ngn-local-1002-bin/ngn" doctor
"$HOME/.local/ngn-local-1002-bin/ngn" --demo -C /path/to/workspace
```

Replace all delivery/workspace placeholders and obtain the real hash through a
trusted channel. This does not download or magically build a missing bundle.
Local candidates are unofficial; official network publication requires the exact
published core and final source-bound release verification. Do not promote an
older preview core or a synthetic future-version update fixture as a release.
