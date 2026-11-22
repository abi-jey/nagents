# Releases

The nagents core (PyPI), native ngn, runtime adapter and browser UI share one version
`0.100.x`, one tag `v0.100.x` and one GitHub release.

## Cut a release

```sh
python ngn/scripts/release.py bump 0.100.N --notes "One-line summary of the release."
```

This updates the core and adapter Python versions, Cargo workspace and lock,
`install.sh`, and the runtime locks. The embedded UI reads the native version
from `/api/bootstrap`; there is no separate native browser version to stamp.
The bump writes `ngn/docs/releases/0.100.N.md` and adds a CHANGELOG entry.
Then open a PR, merge it when green, and tag the merge commit:

```sh
git tag -a v0.100.N -m v0.100.N && git push origin v0.100.N
```

`python ngn/scripts/release.py check --tag v0.100.N` confirms that every component agrees.

## What the tag runs

1. Root CI: lint, Linux/macOS/Windows tests and the distribution build.
2. `publish-stable` publishes the tested wheel and sdist to PyPI.
3. `ngn-release.yml` builds the native bundle against that exact published core on each
   platform's own runner (Linux x86_64, macOS arm64, macOS x86_64), then runs the
   bundled-install and empty-`PATH` acceptance there; Linux also runs the
   CLI/HTTP/WebSocket/PTY and Chromium acceptance.
4. `publish` merges the platform builds (`release.py merge`: shared assets come from
   Linux, each macOS build adds only its archive, bundle and installer), verifies the
   result and creates the GitHub release `v0.100.N` with the core wheel/sdist, bundles,
   native archives, installers, `install.sh`, one manifest per platform
   (`release-manifest.json` for Linux, `release-manifest-<platform>.json` otherwise) and
   `SHA256SUMS`.

## Runtime locks

`ngn/runtime-lock.json` is the reviewed Linux lock: the CPython standalone build and every
dependency version. The macOS locks (`runtime-lock.macos-*.json`) and their notices
(`runtime-licenses-macos-*/`) are derived from it with the same versions:

```sh
python ngn/scripts/runtime_lock.py macos-arm64
python ngn/scripts/runtime_lock.py macos-x86_64
```

Re-run both after changing the Linux lock's Python release or a dependency version.

Never move a tag or overwrite a published asset. If PyPI succeeded but a later step
failed, fix the workflow and release the next patch version. Checksums give integrity
within the trusted workflow and HTTPS; they are not code signatures.

## Alpha/dev and containers

Manual `publish_alpha` / `publish_dev` dispatches are PyPI-only and are not native
update targets. CI also pushes `ghcr.io/abi-jey/nagents/agent-server` on main and tags;
that is not a deployment.
