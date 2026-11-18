# Coordinated releases and candidate integrity

## Shared 0.100.2 release contract

Starting with this release, **one common version, `0.100.2`**, identifies the PyPI
`nagents` core, native ngn application, runtime adapter and embedded browser
release. It uses **one Git tag and one GitHub release, `v0.100.2`**, at the same
tested commit. The GitHub release title is version-only: **`v0.100.2`**.

!!! warning "Source documentation is not publication evidence"
    Installation requires the assets on the official
    [v0.100.2 release](https://github.com/abi-jey/nagents/releases/tag/v0.100.2).
    A passing local checkout does not prove final CI or public availability;
    download URLs fail if that release has not yet been published.
    The already published native `0.100.0` and `0.100.1` releases are immutable.
    The former separate `0.28.6` core publication / reserved native-only tag
    plan is superseded, not a prerequisite to follow now.

Version metadata, Cargo/adapter metadata, embedded lock/manifests, bootstrap
version and core dependency binding must agree. If metadata is already
`0.100.2`, **do not request a patch bump to `0.100.3`** merely to trigger a
release. Use the unused tag matching the final reviewed/tested main commit.
The former Python-only automatic bump is rejected for the coordinated series:
update all component metadata together in reviewed source instead.
Never force-move a tag, overwrite a published asset, bypass hooks/provenance
checks, or waive a missing gate because a prior candidate passed.

## Ordered publication

The coordinated workflow order is:

1. **Root CI (`.github/workflows/ci.yml`)** runs lint/pre-commit, all Linux,
   macOS and Windows Python jobs, frontend checks and the distribution build on
   the bound candidate. The wheel and sdist are recorded with commit/version
   and SHA-256 provenance. A clean, exact main candidate and matching unused
   stable tag are required, not a moving branch tip.
2. **`publish-stable` publishes the tested core wheel and sdist to PyPI only**,
   after verifying their existing manifest, commit, versions and hashes. It
   does not create a competing core-only GitHub release or rebuild the files.
3. **The native release job, with `needs: publish-stable`, calls
   `.github/workflows/ngn-release.yml` as a reusable `workflow_call` workflow**.
   It is not an independently racing native tag-push workflow. It receives the
   same exact commit/tag/version and core provenance.
4. The reusable workflow verifies the **exact published** core wheel/sdist
   against the tested artifact hashes and source binding, then builds the native
   assets and binds the embedded core wheel to those same bytes. Merely finding
   another wheel with version `0.100.2` or passing editable-source imports is
   insufficient. Matching adapter/public device-auth integration is required.
5. The **official complete bundle** must pass installed acceptance, including
   the eight CLI/HTTP/WebSocket/PTY suites with empty PATH, isolated embedded
   imports/CA checks, native installer/update safeguards, and actual desktop/touch
   Chromium acceptance against local fixtures. Preserve evidence. Synthetic
   future-version fixtures are tests, never public releases.
6. Only after all gates pass does publication create the **single GitHub
   release `v0.100.2`**, targeting the same verified tag/commit, with the tested
   core/native assets and version-only title. Do not publish a separate release
   for the adapter or browser. The release is created as a draft before being
   made public; an existing release is never overwritten.

The combined release contains the core wheel/sdist, complete native bundle,
small native archive, native source archive, adapter wheel, native installer,
`install.sh` bootstrap, `release-manifest.json` and `SHA256SUMS`, with runtime
provenance inside the bundle.
The small native archive is not the complete Python-inclusive bundle; users
wanting no preinstalled Python should follow the [native installer](native-ngn.md).

Verify the final merged workflow and its exact generated assets, rather than
assuming this guide alone enforces ordering. No local review substitutes for
the actual final platform/frontend/build/publication jobs. A PyPI publication
followed by a failed native gate is a blocked/incomplete coordinated release:
inspect workflow and public state deliberately, never silently skip existing
PyPI files, create a premature GitHub release, or overwrite immutable artifacts.

## Artifact integrity

The distribution manifest records exactly one wheel and one sdist with their
versions, candidate SHA and hashes. Publish jobs download and verify these same
artifacts; they must not pull a moving branch or rebuild distributions. Native
packaging additionally verifies source archives, embedded wheels, bundle
inventory/runtime metadata and release checksums. Every artifact, published
version and tag must resolve to the same candidate binding.

These checks establish integrity/provenance within the trusted workflow and
HTTPS delivery. They are **not independent code signatures**, a claim of
bit-for-bit reproducible dependency builds, paid-provider validation, full
legacy feature parity, or an OS sandbox.

## Alpha/dev and containers

Choose only one publication mode per dispatch. Feature alpha stamping and
run/attempt-based dev stamping apply to the built PyPI artifacts; both modes
are **PyPI-only**, with no extra GitHub release or stable native update target.
Keep their candidate/version/hash verification; do not rebuild in publish jobs.

The ordinary CI image build may publish
`ghcr.io/abi-jey/nagents/agent-server` with its usual main/release tags and an
immutable `sha-<full commit>` tag. It builds its own dependencies, so it is not
byte-identical to a distribution artifact. **Publishing a GHCR image is not a
Kubernetes deployment or production rollout**. Existing Python container/private
deployment recipes require separate explicit operator authorization and remain
separate from the native loopback host.

Documentation deployment is separate. Review the final diff for private data,
confirm that the intended version/tag is unused, satisfy any operator timing
restriction, and verify public version/commit/hashes after publication. No
workflow is evidence that a production rollout was requested or performed.
