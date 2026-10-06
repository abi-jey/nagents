# Independent ngn diagnostics

This optional development task exercises the shipping `Harness.run`, native file
tools, test execution, `delegate(..., agent='assistant')` with inspect-only
instructions, and completing actionable review work. It contains no Terminal-Bench
material or reference solution, and produces no comparable benchmark score.

The [0.20.0 release report](../terminal_bench/RESULTS.md#release-0200-evaluation)
also includes a separate Agent-core description comparison using pure in-memory
fixtures. That comparison does not run this diagnostic's Harness review workflow.

The standard-library fixture is deliberately staged: fix ordinary arithmetic,
run the public tests, request an independent review against the public acceptance
contract, then fix valid findings and add/run regression tests. The separate
verifier checks nine functional cases. A post-run audit checks actual tool and
notification order; functional reward and workflow evidence remain separate.

The task image and verifier image are pinned. The audited adapter/runner are
reused unchanged: one attempt, one task at a time, 1 CPU, 1 GiB, 512 PIDs, and a
900-second agent deadline with normal ngn tool budgets. Expected task work is
under five minutes. Task work needs no network and its instructions forbid it;
model/package transport remains available, without a new egress firewall.

## Prepare a new immutable run

From the repository root, use a separate Python 3.12+ environment and a private
run directory. Preparation records an explicit credential **path only** and
never starts a model or discovers/copies a login.

```sh
NGN_DIAG_DIR="$(mktemp -d)"
python3 -m venv "$NGN_DIAG_DIR/venv"
"$NGN_DIAG_DIR/venv/bin/python" -m pip install -r benchmarks/terminal_bench/requirements.txt
: "${NGN_DIAGNOSTIC_CREDENTIALS:?Set an explicit access-only credential file path}"
"$NGN_DIAG_DIR/venv/bin/python" -m benchmarks.diagnostics.prepare \
  --bundle "$NGN_DIAG_DIR/bundle" --jobs "$NGN_DIAG_DIR/jobs" \
  --credentials "$NGN_DIAGNOSTIC_CREDENTIALS" --model gpt-6-astra
PYTHONPATH="$NGN_DIAG_DIR/bundle" "$NGN_DIAG_DIR/venv/bin/harbor" run \
  --config "$NGN_DIAG_DIR/bundle/job.json" --dry-run
```

Review the frozen task, `job.json`, `provenance.json`, and `task-provenance.json`.
The snapshot records source/wheel/runner/adapter hashes, every task file hash, the
audit hash, and configuration hashes. Reusing a bundle directory is rejected.
Built upstream dependencies are recorded; bit-for-bit reproducibility of external
package repositories is not claimed.

Credential provisioning uses the existing [access-only adapter contract](../terminal_bench/README.md#explicit-temporary-chatgpt-credentials).
Keep the credential file private and outside bundles/results. After review and
explicit provisioning, run the same Harbor command without `--dry-run`. Delete
the host temporary credential file afterwards; adapter cleanup handles its
container copy. Keep real-run logs and reports private.

After the trial finishes, run the frozen audit on its trial directory:

```sh
"$NGN_DIAG_DIR/venv/bin/python" "$NGN_DIAG_DIR/bundle/audit_workflow.py" \
  "$NGN_DIAGNOSTIC_TRIAL" > "$NGN_DIAG_DIR/audit.json"
```

The audit records artifact hashes and preserves Harbor exceptions separately
from functional and workflow results. It requires a directly delegated child
whose acknowledgement, start, successful completion, notification to Main, and
retained history share its task ID. Grandchild or unrelated histories do not
count. The required child must have successful native reads of both `report.py`
and `REVIEW.md`, with only supported inspection tools in the retained history.
Empty, missing, or errored required histories cannot establish this evidence.

Recoverable provider errors stay visible as observations and do not override a
subsequent successful root completion. Terminal errors and tool errors remain
separate workflow checks. New traces bind completion to the declared root session
and exclude compaction completion; older traces explicitly report their missing
session binding. Functional verifier results remain independent of these checks.

Automatic test evidence supports plain `python`/`python3 [-B] -m unittest`
invocations with `test_*` modules or `discover` and verbosity flags. An optional
`cd /app &&` prefix and `PYTHONDONTWRITEBYTECODE=1` assignment are supported.
Success requires exit code zero, complete output with a nonzero `Ran N tests`
summary ending in `OK`, and no failure/error summary. Additional shell chains,
redirections, wrappers, and other command forms require manual confirmation;
they are neither accepted as successful tests nor labeled Harness crashes.
Only edits reporting the exact task `report.py` path count toward follow-through.
The last observed test run after the last such edit must pass; an earlier passing
run cannot hide a later failure.

Manually inspect newly added test files and match their cases to the executed test output.
Bounded child histories and operational logs are evidence, not a tamper-proof
attestation or a broad model-accuracy evaluation.

## Verify the infrastructure without a model

Use a different new bundle with `--verifier-only`. No credential path is accepted;
Harbor's `nop` agent leaves the intentionally broken baseline untouched.

```sh
"$NGN_DIAG_DIR/venv/bin/python" -m benchmarks.diagnostics.prepare \
  --bundle "$NGN_DIAG_DIR/verifier-bundle" --jobs "$NGN_DIAG_DIR/verifier-jobs" \
  --verifier-only
"$NGN_DIAG_DIR/venv/bin/harbor" run \
  --config "$NGN_DIAG_DIR/verifier-bundle/job.json"
```

Expect one completed trial, no infrastructure exception, nine verifier cases,
and reward **0** for the broken baseline. This exercises Docker creation, artifact
transfer, and the independent verifier environment; `--dry-run` alone does not.

The optional regression suite includes preparation/audit checks and an opt-in
real Docker check, all without model calls:

```sh
"$NGN_DIAG_DIR/venv/bin/python" -m pip install pytest
PYTHONPATH=src "$NGN_DIAG_DIR/venv/bin/python" -m pytest -q benchmarks/diagnostics/tests
NGN_DIAGNOSTIC_DOCKER_TESTS=1 PYTHONPATH=src \
  "$NGN_DIAG_DIR/venv/bin/python" -m pytest -q benchmarks/diagnostics/tests/test_verifier_setup.py
```

The local pytest collection guard excludes task-data tests from repository test
discovery. It does not alter task filenames or disable the independent verifier.

The offline workflow-audit, preparation, and native runner regressions need no
Harbor installation, Docker, real credentials, or model calls. The optional Harbor
adapter and real verifier setup checks remain available through the commands above
and the adapter's test suite.

The separate **Offline harness diagnostics** workflow uses Python 3.13 and the
pinned Harbor development requirements to run both optional test directories.
It runs for relevant source changes or manual dispatch, has no publishing job,
and explicitly leaves the real Docker test disabled.
