# Releases

The nagents core (PyPI), native ngn, runtime adapter and browser UI share one version
`0.100.x`, one tag `v0.100.x` and one GitHub release.

## Cut a release

```sh
python ngn/scripts/release.py bump 0.100.N --notes "One-line summary of the release."
```

This updates every version field (`pyproject.toml`, Cargo, adapter, `package.json`,
`install.sh`, `runtime-lock.json`, `Cargo.lock`), writes `ngn/docs/releases/0.100.N.md`
and adds a CHANGELOG entry. Then open a PR, merge it when green, and tag the merge commit:

```sh
git tag -a v0.100.N -m v0.100.N && git push origin v0.100.N
```

`python ngn/scripts/release.py check --tag v0.100.N` confirms that every component agrees.

## What the tag runs

1. Root CI: lint, Linux/macOS/Windows tests and the distribution build.
2. `publish-stable` publishes the tested wheel and sdist to PyPI.
3. `ngn-release.yml` builds the native bundle against that exact published core, then
   runs the bundled-install, empty-`PATH` CLI/HTTP/WebSocket/PTY and Chromium acceptance.
4. `publish` creates the GitHub release `v0.100.N` with the core wheel/sdist, bundle,
   native archive, installer, `install.sh`, manifest and `SHA256SUMS`.

Never move a tag or overwrite a published asset. If PyPI succeeded but a later step
failed, fix the workflow and release the next patch version. Checksums give integrity
within the trusted workflow and HTTPS; they are not code signatures.

## Alpha/dev and containers

Manual `publish_alpha` / `publish_dev` dispatches are PyPI-only and are not native
update targets. CI also pushes `ghcr.io/abi-jey/nagents/agent-server` on main and tags;
that is not a deployment.
