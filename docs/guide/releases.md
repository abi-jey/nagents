# Release candidate integrity

Stable releases use the CI workflow's `version_bump` input on `main`, after the
candidate's checks pass. The bump starts from the triggering SHA, requires that
SHA still be current main, and uses a non-force push. If main advances, rerun the
workflow on the new candidate rather than rebasing untested changes into it.
The only post-test source change is the version bump; build and frontend checks
run on its exact resulting commit.

The build job exports its full commit SHA and package version and creates a
separate `release-manifest` artifact. This records SHA-256 hashes of exactly one
wheel and one source distribution. Both embedded package metadata versions must
match the candidate version. Stable, alpha, and dev publication download those
same artifacts and verify the manifest, expected SHA, expected version, and local
checkout SHA before publishing. Publish jobs never rebuild distributions or pull
a moving branch tip. This manifest binds artifacts within the trusted workflow;
it is not an independent signature or a claim of reproducible dependency builds.

Stable tag names must equal `v<package version>`. An existing stable tag must
resolve to the exact candidate commit. GitHub releases target that commit
explicitly, rather than the repository's default branch. Previous stable release
notes use the latest merged stable tag excluding the candidate version.

Choose only one publication mode per dispatch. Feature alpha versions use the
existing alpha stamping script. Dev versions are stamped in the build job using
the workflow run number and attempt, then published without a second build.
Existing PyPI files are not silently skipped: if publication partially succeeds,
inspect PyPI and the workflow state before deciding how to recover.

Docker builds use the exact bump commit (or triggering SHA) and an immutable
`sha-<full commit>` tag. Docker still builds its own image and dependencies; it is
not byte-identical to the wheel/sdist artifact and dependency pinning remains a
separate supply-chain concern.

Before a release, require all CI jobs and artifact checks to pass, review the
final diff for private artifacts, verify the intended version is unused on PyPI,
and satisfy any operator timing restriction. The workflow does not enforce a
Europe/Zurich release window. No local review or test run substitutes for the
actual Linux/macOS/Windows and frontend/build workflow on the final candidate.
