# Intent benchmark

This suite runs the actual native Harness using the orchestration benchmark's
isolated subprocess runner boundary, tool approval policy and measured provider
factory. Root and child generations, cached tokens, reasoning tokens, tool calls,
failures and latency are recorded together. Shell and scheduling tools are disabled;
only explicit authorized correction cases approve root edits of their fixture file.
Children cannot receive write approval. Fixture contents are untrusted task data.
The model has no tools for benchmark-source files outside its temporary workspace.

`cases.py` declares ten development and ten heldout cases **before tuning**.
Paraphrases, config fields and directory layouts differ across these fixed sets. Cases cover
knowledge/conversation, context-known answers, filename versus literal-content
search, inspection questions, advice-only requests, cancelled work, ambiguous target selection
and authorized saved edits.
They are not a general intent accuracy estimate or a multi-turn interruption test.
Machine-readable output fields permit deterministic outcome grading without
matching prose or prescribing a particular tool sequence. Clarification keyword
checks only detect obviously unrelated questions: all ambiguity answers require
manual review of whether the question resolves the missing target choice.
Denied mutation attempts still fail intent compliance on read-only, advice and
cancellation cases; task-answer correctness is retained separately. Equivalent tool choices
are allowed. Tool-free answers require zero calls. Other minimum-action targets are
reported separately, not used to excuse incorrect answers. Saved edits require a
successful read after the last mutation and preserve other fixture content.

From the repository root, using the project's virtual environment:

```bash
PYTHONPATH=src .venv/bin/python -m benchmarks.intent.run \
  --split development --concurrency 2 --output /tmp/intent-baseline
PYTHONPATH=src .venv/bin/python -m benchmarks.intent.run \
  --split heldout --concurrency 2 --output /tmp/intent-heldout
.venv/bin/python -m pytest benchmarks/intent
```

Output must be a new directory. Freeze baseline results before changing prompts,
and never tune against heldout outcomes. For source/worktree comparisons use the
same cases, model and runner, and set PYTHONPATH to the chosen worktree's `src`.
Concurrency is capped at two subprocesses; coordinate with other live benchmark
runs so total concurrency does not exceed two. Source hashes include both the
native Harness and benchmark code, checked at start and end; case prompt hashes
are also recorded at both ends. Any observed source/prompt drift makes a run
ineligible for efficiency comparisons. Efficiency comparisons require correct
outcomes, complete root completion and complete provider usage, with no source
change, child failure or provider error. Compare tokens and tool calls jointly with
success, and retain unsuccessful trials rather than selecting only fast successes.

The runner intentionally reuses `benchmarks.orchestration.run.trial`, including
its saved-file verifier, bounded requests, fixture isolation and telemetry. It
replaces only source hashing and the clarification grader inside each isolated
worker; it does not change model prompts or emulate Harness tools.
