# Self-contained installation and updates

**Available from 0.100.2.** Earlier releases (0.100.0/0.100.1) do not contain this
installer or an embedded Python runtime; install 0.100.2 or newer to use it.

## Recommended installation

Use the managed, per-user bundle rather than a global pip installation. It includes
the native harness/browser UI, pinned CPython **3.13.18**, the matching Python core
and adapter, binary dependency wheels, CA certificates and redistribution notices.
Installing/running it needs **no preinstalled Python, pip, venv, Rust, C compiler
or Node**. It makes no provider request and does not log in or import legacy state.

Initially supported: **Linux x86_64**, with the system libraries required by the
Ubuntu 24.04 native build (glibc 2.39 or newer). The embedded Python's lower glibc
baseline does not lower the Rust binary's requirement. macOS, Windows, ARM and
musl-only Linux are not yet validated installer targets.

Install the latest bundled release:

```sh
curl --proto '=https' --tlsv1.2 -fsSL \
  https://github.com/abi-jey/nagents/releases/download/v0.100.2/install.sh | sh
```

The small shell bootstrap uses standard OS shell, `curl`, and `sha256sum` or
`shasum` utilities. It verifies the version-bound native installer before running
it; Python, pip and tar are never invoked. Alternatively, download the native
`ngn-installer-linux-x86_64` asset and verify it against that release's
`SHA256SUMS`, then run it directly. The native installer itself needs no shell
download/hash/extraction helpers:

```sh
chmod +x ./ngn-installer-linux-x86_64
./ngn-installer-linux-x86_64 install --version 0.100.2
```

Defaults are `~/.local/share/ngn-harness` (respecting `XDG_DATA_HOME`) for immutable
version directories, separate from legacy `ngn` and native `ngn-rust` state, and
`~/.local/bin/ngn` for the command symlink. Add that bin directory to `PATH` if
needed. An existing Python `ngn` executable or unrelated symlink is **not**
overwritten. Select a separate bin directory instead:

```sh
./ngn-installer-linux-x86_64 install --version 0.100.2 \
  --bin-dir "$HOME/.local/ngn-bin"
export PATH="$HOME/.local/ngn-bin:$PATH"
ngn doctor
ngn --demo
```

`--prefix DIRECTORY` chooses a separate managed installation. Existing unsafe
directories/symlinks are rejected, not chmod-repaired or reused silently.

## Configure the bundled agent

Copy one of the installed example configurations to your own private configuration
location, then select your provider/model. The examples omit `runner`: the
installed harness automatically selects its version-specific embedded Python and
adapter with isolated imports. A trusted, explicitly configured runner remains
supported and is not silently replaced. No existing configuration is imported.

For a ChatGPT subscription, use the ChatGPT example and explicitly authorize
`ngn --config /path/to/native.json auth login` in your own browser. For API-key
connections, export the selected environment variable securely; never put key
values in the configuration, command line or URL. See [authentication](chatgpt-auth.md).
No Python packages need to be installed separately.

## Update explicitly

```sh
ngn update --check
ngn update
# Optional: select a particular newer stable harness release.
ngn update --version 0.100.3
```

Discovery selects stable **Rust `v0.100.x`** releases from this repository, not a
newer Python-library tag. It never follows an arbitrary configured update feed.
`--check` does not activate a version. Running a source, pip/package-manager or
unmanaged binary does not authorize self-overwriting it: use the managed installer.
Downgrades and replacing a version with different content are refused.

The updater stages a verified release, checks every payload and the bundled
runtime, then atomically switches `current`. Corrupt/truncated downloads or failed
validation leave the previous version selected. Concurrent updates are locked.
Previous releases are retained; running hosts keep their original version-specific
Python path rather than following the new symlink mid-run.

Updates do **not** touch configuration, credentials, workspaces or conversation
databases, restart a host, refresh a token, or make a model request. Before starting
a newer host against important state, stop the old host and follow its release's
backup/schema instructions. Retaining an older binary is not permission to
downgrade an upgraded database in place.

Downloads use fixed official HTTPS endpoints and release-bound SHA-256 checksums.
These are integrity/provenance checks, **not independent code signatures** or an
OS sandbox. Trust in the publisher and protection of the installed files remain
necessary.

## Local/offline acceptance and source development

An explicitly trusted local bundle can be installed without network access:

```sh
./ngn-installer-linux-x86_64 install --bundle /path/to/bundle.tar.gz \
  --sha256 EXPECTED_SHA256 --version 0.100.2 \
  --prefix /private/test/install --bin-dir /private/test/bin
```

Use a checksum obtained through your trusted delivery channel. Local development
bundles are marked unofficial; network installation and official publication must
use a bundle made with the matching **published** core. The adapter minimum is
`nagents>=0.28.6,<0.29`; publish/test that core before the harness release. Published
0.28.5 lacks its public device-authentication API. Publication guards are not
bypassed by source-only development success.

Building from source still needs development tools. `./ngn/dev` continues to build
and launch current working source, including uncommitted edits; `./ngn/dev --test`
checks its installed release-shaped candidate. That is separate from the
dependency-free end-user installation above. Full legacy UI/remote hosting parity
remains [unfinished](migration.md).
