# Natural native-tool failure benchmark

Each isolated subprocess gives the actual Harness a short user request exactly as stored in the case. The runner adds no execution strategy, answer format, tool contract reminders, or grading hints. Native tools retain their normal descriptions. Shell, delegation, and wakeups are disabled for the first batch. Only the case's explicitly authorized file edits and writes receive approval.

Fixtures live in a private, randomly named workspace. Expected answers and task grading stay outside it. A stale-file control changes a target after its first successful read; the model discovers that change through the native edit conflict. Its original failed attempt remains in the trace.

Run from a Python virtual environment:

```sh
python -m benchmarks.natural_errors.run --split development --output /tmp/natural-errors-results
```

The default model is `gpt-6.1-sol`. Defaults bound each trial to 180 seconds, 32 model requests, and 150,000 observed tokens. The token bound is a soft admission bound: a request already admitted can exceed it. Independent subprocess trials run concurrently, with an additional parent watchdog. `--concurrency` defaults to 2 and accepts 1..4. XDG configuration, state, and cache directories are isolated; HOME stays intact so the transport can use existing local OpenAI authentication.

Private result files include every offered tool schema and complete model input before transport, its hash, chronological tool attempts, actual responses, native generation IDs, actor and task IDs, task grading separately from avoidable-call auditing, and imported source hashes before and after the trial. Keep all trials, including unsuccessful and interrupted attempts. Result files contain private user content and should not be published without inspection.

The ten case families cover long-file lookup, CRLF edits, duplicate text,
new/existing files, user-supplied filename typos, stale edits, denied approval,
latest-error lookup and literal punctuation searches. The two splits vary wording
and some fixture values within the same families; the heldout split is a wording
and data check, not an independently sourced generalization benchmark. These are
original authored prompts informed by the public prompt style survey in
`benchmarks/prompt_corpus`, not copied human conversations.

A denied edit is a blocked control, never a completed requested edit. Artifact
integrity is scored separately, and refusal honesty needs manual review. Exact
file comparisons check narrow fixture changes; answer-term checks are only a
minimal functional check and also need trace review. No tool-interface change
should be accepted on this small suite alone.

See [RESULTS.md](RESULTS.md) for the real-model comparison and rejected description candidates.

The opt-in `--suite generalization --split heldout` adds seven frozen novel task
families: nested configuration discovery, noisy filename typos, missing files,
nested file creation, contextual duplicate edits, filenames mentioned in document
contents, and ambiguous existing targets. `--suite all` selects both suites;
the default `original` preserves the original twenty trials. Generalization cases
have a single heldout variant and are independently authored rather than extracted
from the prompt corpus. Their exact wording and grading fixtures were frozen
before runtime optimization comparison; runtime implementers receive family-level
coverage descriptions only.

Missing-file and ambiguous-target controls permit no writes. Their unchanged
artifacts and nonempty final response establish only a
`clarification_review_candidate`, never automatic semantic correctness. Inspect
the trace and final response to distinguish an honest request for missing context
from invented answers, false completion, or unnecessary refusal. `blocked_reason`
separates these controls from denied approvals. Report reviewed appropriate
clarifications separately from completed actionable tasks and unresolved controls;
`task_correct` remains reserved for completed actionable tasks. The runner adds
only public fixture files and the original user utterance to model context;
expected artifacts, answer terms, and control metadata remain outside it.

See [IMPROVEMENTS.md](IMPROVEMENTS.md) for the retained harness changes and repeated real-model validation.
