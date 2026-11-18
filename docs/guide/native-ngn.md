# Native ngn: installer and usage

The native Rust `ngn` (TUI, headless CLI, local browser host and updater) ships as a
self-contained bundle: CPython 3.13.18, the matching nagents core and adapter, locked
dependency wheels and CA certificates. No Python, pip, Rust, Node or compiler is needed.

Supported: **Linux x86_64, glibc 2.39+** (Ubuntu 24.04 or newer).

## Install

```sh
curl -fsSL https://github.com/abi-jey/nagents/releases/latest/download/install.sh | sh
```

This installs the latest release to `~/.local/share/ngn-harness` and links
`~/.local/bin/ngn`. The bootstrap verifies the native installer's SHA-256 against the
release's `SHA256SUMS` before running it. To pin a version, replace `latest/download`
with `download/vX.Y.Z`.

```sh
ngn doctor          # paths and bundled runtime; no network or model request
ngn --demo          # offline demo; no model, never edits files
ngn --demo serve    # same, in the browser at the printed loopback URL
```

Options: `sh -s -- --prefix DIR --bin-dir DIR`. The installer never overwrites an
existing unrelated `ngn` (such as the Python client); pick another `--bin-dir` instead.
It refuses world-writable or foreign-owned parent directories.

### Manual install

```sh
base=https://github.com/abi-jey/nagents/releases/latest/download
curl -fsSL -O "$base/ngn-installer-linux-x86_64" -O "$base/SHA256SUMS"
sha256sum --ignore-missing -c SHA256SUMS
chmod +x ngn-installer-linux-x86_64 && ./ngn-installer-linux-x86_64 install
```

For an offline bundle, add `--bundle FILE --sha256 HASH --version X.Y.Z`.

## Configure the bundled agent runtime

Copy `~/.local/share/ngn-harness/current/config.example.json` (or
`config.chatgpt.example.json`) to a private location and set `model`. Omit `runner`:
the bundled Python is used automatically. Keep keys in the environment variable
named by `api_key_env`, never in the file.

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

```sh
ngn --config ~/.config/ngn-native.json -C /path/to/workspace          # TUI
ngn --config ~/.config/ngn-native.json -C /path/to/workspace serve    # browser
ngn --config ~/.config/ngn-chatgpt.json auth login                    # ChatGPT subscription
```

## Update

```sh
ngn update --check   # report a newer stable release
ngn update           # verify, stage and atomically switch; previous versions are kept
```

Updates never touch configuration, credentials, workspaces or conversations, and never
restart a running host. Stop the host before starting a newer version on important state.

## Paths

| Item | Default |
| --- | --- |
| Installation | `$XDG_DATA_HOME/ngn-harness` or `~/.local/share/ngn-harness` |
| Command | `~/.local/bin/ngn` |
| Native state | `~/.local/share/ngn-rust/workspaces/<hash>/` |
| Python client state | `~/.local/share/ngn` (never imported) |

The native and [Python ngn client](ngn-installation.md) share the command name but not
configuration or state. See [native migration and safety](native-ngn-migration.md) for
feature gaps.

## From source

```sh
./ngn/dev            # build and launch the current checkout (offline demo without --config)
./ngn/dev --test     # build and run the installed-candidate acceptance suite
```

Requires Python 3.13, Rust 1.93+ and a C compiler. See
[native development](https://github.com/abi-jey/nagents/blob/main/ngn/README.md#development).
