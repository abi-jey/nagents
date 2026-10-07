# Native Harness orchestration benchmark

Three identical-goal task fixtures compare direct execution, forced concurrent
delegation, or adaptive choice. They exercise actual Harness sessions, bounded
file tools, approval gates, child lifecycle delivery, and parent synthesis.
The model uses the normal native OpenAI provider and local credential discovery.
Generated tools execute only against private temporary fixtures. Shell is disabled;
only the intended root configuration correction receives edit approval.

```bash
.venv/bin/python -m benchmarks.orchestration.run --model gpt-6.1-sol \
  --strategy both --concurrency 1 --repeats 1 --output /tmp/ngn-orchestration-results
.venv/bin/python -m pytest benchmarks/orchestration -q
```

`--strategy adaptive` leaves delegation available without requiring it. Each
trial runs in its own subprocess and temporary workspace with isolated XDG
stores. The benchmark never changes HOME or copies credentials. Results use
private directories and files; each records source, fixture, goal, and strategy hashes.

The ledger and policy scenarios grade the final JSON answer and original file
integrity. The correction scenario additionally grades the actual saved JSON and
a successful file read after the last edit. No credit is given for merely saying
the correction was made. Forced-strategy compliance is reported separately from
task correctness; at least two overlapping child lifetimes are required.
Efficiency eligibility additionally requires a session-scoped successful root stop,
complete usage, successful child outcomes, inspection of every input, compliance
with the selected strategy, and unchanged source during the trial. Source hashes
are captured before initialization and checked again afterwards.
Lifecycle overlap includes setup/waiting time and does not prove simultaneous
GPU execution. Provider call start times/durations are also retained.

The provider wrapper observes every generation before the agent consumes events,
including root, children, and tool-free calls (which may be compaction). It counts
usage once per generation, never repeated tool events or cumulative session usage.
Cached input and reasoning tokens are subsets of input/output respectively, not
additional totals. `uncached_prompt_tokens` is input minus cached input. Interrupted
calls retain any reported counts but mark usage incomplete; totals then provide
only observed lower bounds. Tool-free calls are labeled, not assumed to be compaction.

Each trial has a 300-second default deadline, 32-generation request ceiling, and
150,000 observed-token admission threshold. In-flight requests may exceed the token
threshold; the Codex transport does not support a hard output-token budget. Both
strategies use the same model and native settings. Initialization and run latency
are reported separately. There is no automatic live-request retry in the wrapper.

These are small controlled tasks, not a general ranking of orchestration designs.
Compare correctness and strategy compliance alongside total/uncached input tokens,
output tokens, actual child overlap, tool errors, and latency. A single repetition
does not establish causal superiority; cache warmth, upstream load, and task size
affect results. Avoid dollar-cost estimates without verified model pricing.
