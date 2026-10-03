# ngn reliability pilot on Terminal-Bench

This development-only adapter measures the shipping `Harness.run` loop and native
tools. It installs a wheel from an immutable snapshot of the current working tree,
including uncommitted Python changes and built web assets. It does not replace the
agent, add task-specific solving prompts, repair tool failures, or change ngn's
production approval defaults. Harbor is not a nagents runtime dependency.

[Development pilot observations](RESULTS.md) separate setup failures, harness
failures, model completion, and verifier outcomes from the recorded candidates.

The pilot pins **Harbor 0.23.0** (`1e5c5c6db929a10a140d05e606882c671ae20729`) and
**Terminal-Bench 4.0.0** (`452bf305c6daa62fc59061d22133a7cbc7c1572e`). Three CPU-only
tasks exercise stream processing, database recovery, and data processing:
`session-window-debug`, `wal-recovery-ordering`, `data-anonymization`. Their
verifiers run separately. Task instructions go to the agent; verifier/solution
files and the benchmark checkout do not.

This is a bounded reliability pilot, **not a comparable leaderboard score**:
one attempt per task, one concurrent trial, a 900-second agent budget, and no
Harbor retries. The shipping defaults remain 30 tool rounds, 60-second default
shell timeout, and native subagents. Provider-level retries remain the shipping
behavior and appear as rate-limit events when emitted. Task CPU/memory limits are
enforced (2 CPUs, 4–8 GiB), with a 512-process cap. Local Docker does not enforce
the task's declared disk quota; image layers and total disk use need host capacity.

The round limit applies to each underlying `Agent.run` model/tool loop. A round
can contain several tool calls, and normal background-result synthesis starts a
new loop. It is not a 30-call aggregate limit for an entire Harbor trial.

## Prepare without running a model

Use a separate Python 3.12+ environment. From the repository root:

```sh
python3 -m venv /tmp/ngn-harbor-venv
/tmp/ngn-harbor-venv/bin/python -m pip install -r benchmarks/terminal_bench/requirements.txt
/tmp/ngn-harbor-venv/bin/python -m benchmarks.terminal_bench.prepare \
  --bundle /tmp/ngn-tbench-bundle \
  --credentials /tmp/ngn-tbench-private/credentials.json \
  --jobs /tmp/ngn-tbench-jobs \
  --model gpt-6-astra
PYTHONPATH=/tmp/ngn-tbench-bundle /tmp/ngn-harbor-venv/bin/harbor run \
  --config /tmp/ngn-tbench-bundle/job.json --dry-run
```

Preparation records the credential **path only**; it does not read credentials or
start a trial. Use a new bundle directory for every source snapshot. Review
`provenance.json`, `runtime-constraints.txt`, `job.json`, and `isolation.json`.
Provenance records Git HEAD/dirty state, per-source-file hashes, wheel/runner
hashes, frozen adapter files, and dataset/Harbor pins. The installed Python
dependency versions and task image ID are recorded per trial. Build dependency
and container image package repositories are external inputs; this is not a
claim of bit-for-bit reproducible upstream Docker builds.

`--dry-run` validates configuration and adapter import without running a trial.
Harbor reports that it skips Git task contents and commit fetching. It is not an
authentication, image-build, or model-access test.

Use repeatable `--task` flags to select a subset, for example
`--task data-anonymization`. Names are restricted to the three pinned,
resource-reviewed tasks above; duplicates and unknown names are rejected. The
selected subset is recorded in both job config and provenance. Use a new bundle
and jobs directory for a setup-repair run, preserving the original results.
If shipping ngn code also changed, report it as a new candidate rather than an
identical-baseline retry.

Setup checks Python 3.11+ and `ensurepip` before creating its private venv.
If `ensurepip` is missing on Debian/Ubuntu, it explicitly runs `apt-get update`
and installs the matching `python3.x-venv` package. Images missing it on other
distributions fail clearly. Setup collects `install.log`, Python version,
available installed Python dependencies, and Debian Python package versions on
success or failure; setup failure is not a model or task-verifier failure.

## Explicit temporary ChatGPT credentials

Provision a fresh, access-only JSON file outside the repository and results
directories, in a private directory (0700), with file mode 0600. Use the existing
supported ngn/Codex auth resolver; do not copy a home directory, refresh token,
Codex configuration, or provider registry. The exact schema is:

```json
{"access_token":"<temporary access token>","account_id":"<account ID>","residency":""}
```

Do not put real values in commands, this README, job config, or logs. The adapter
does not discover host credentials, refresh login, or accept arbitrary provider
endpoints. The shipping OpenAI provider consumes the explicit credentials through
an access-only `OpenAIAuth` subclass, also shared by normal native subagents.
Refresh the host file through the authorized resolver before a new trial if its
access token expires. The adapter never writes back to the user's login store.

The runner uses the released YAML/provider configuration API with an explicit
OpenAI/ChatGPT connection and model. Each trial creates fresh private
`XDG_CONFIG_HOME` and `XDG_DATA_HOME` directories below its installed-agent
directory. Both global and workspace provider registries therefore start empty;
image defaults and earlier trials cannot choose another provider. The normal
Harness initialization and native child construction remain in use.

The adapter copies only this file to `/run/ngn-benchmark-creds.json`, outside the
task workspace, and deletes the container copy in cleanup, including failed or
cancelled runs. Delete the host ephemeral file after the job. Docker teardown is
the fallback if a process cannot execute cleanup. Log serialization redacts the
exact token. This is narrow credential provisioning, not a secret sandbox:
shell-capable code inside the container can read credentials available to it.

Before enabling automatic approvals, the adapter inspects Docker and rejects
privileged containers, added capabilities/devices, host namespaces, missing
CPU/memory/PID limits, sidecars, or mounts other than that trial's exact Harbor
log/artifact mounts. A marker and `/.dockerenv` check only prevent accidental host
execution. Docker isolation is the boundary. Do not mount the Docker socket,
host workspace/home, benchmark checkout, tests, or solutions into the agent.

## Run after reviewing the prepared bundle

```sh
PYTHONPATH=/tmp/ngn-tbench-bundle /tmp/ngn-harbor-venv/bin/harbor run \
  --config /tmp/ngn-tbench-bundle/job.json
/tmp/ngn-harbor-venv/bin/python -m benchmarks.terminal_bench.summarize \
  /tmp/ngn-tbench-jobs/ngn-terminal-bench-4-reliability-pilot
```

Harbor owns task containers, verifier execution, rewards, cancellation, and
teardown. The runner adds only explicit container-scoped approvals and event
recording around the normal `Harness.run`. No extra solving turn or retry is
inserted when a tool fails. A shell nonzero exit or validation error can be
recovered by the model normally; it still remains in the report.

`agent/events.jsonl` retains flushed ngn events, approval decisions, root completion,
timeouts, and exceptions. `agent/harness-metrics.json` separates root tool errors,
argument-validation hints, permission hints, nonzero shell exits, shell timeouts,
output truncation, and provider events from the **verifier reward**. Error-category
hints come from ngn's existing error strings; raw errors remain available.
An interrupted stream without an end event is never called completed.

Successful runner completion requires the root session's final `DoneEvent` with
`finish_reason="stop"`, outside compaction, and no terminal Harness error.
Recoverable provider errors remain in the event log and raw error count, with
separate `recoverable_errors` observations; successful recovery does not make the
trial a Harness failure. Terminal errors, exhausted rounds, incomplete output,
timeouts and cancellation remain distinct from completion. None of these statuses
claims verifier success or changes the recorded results of earlier frozen pilots.
A failure while closing the Harness or deleting its credential file is recorded
separately; it makes an otherwise successful trial `cleanup_error` with a nonzero
exit. Earlier failure, timeout, or cancellation status is retained, and adapter
cleanup/Docker teardown still own the final removal of container credentials.

Metric scope is explicit: `Harness.run` exposes the root tool stream plus native
task lifecycle; child tool streams are private to the shipping subagent manager.
All approvals include task IDs, and the runner retains the supported bounded
child-history view (up to 200 current messages per task, subject to compaction).
Root `DoneEvent` usage is recorded, but is not reported as complete billing for
child or compaction calls. Logs and histories may contain task data; do not publish
them automatically.

## Regression checks

```sh
source .venv/bin/activate
python -m pytest -q benchmarks/terminal_bench/tests/test_runner.py
# Adapter tests additionally require the separate Harbor environment:
/tmp/ngn-harbor-venv/bin/python -m pip install pytest
PYTHONPATH=src /tmp/ngn-harbor-venv/bin/python -m pytest -q benchmarks/terminal_bench/tests
pre-commit run --all-files
```

The scripted provider in the regression test only supplies deterministic model
responses. The production harness actually validates malformed calls, launches
and times out shell commands, checks approvals, persists results, and continues
its normal loop. No scripted provider is used by the benchmark adapter.

Official references: [custom installed agents](https://docs.harborframework.com/core-concepts/agents/custom-agents),
[job configs](https://docs.harborframework.com/core-concepts/jobs/configs),
[Terminal-Bench 4.0.0 release](https://github.com/harbor-framework/terminal-bench/releases/tag/v4.0.0).
