# Harness improvement validation — 2026-10-08

The retained changes make native scheduling/delegation guidance follow effective
advertised capabilities, refresh generated prompts between runs, distinguish
existing-file reads from new-file creation, and provide actionable missing-file
recovery guidance. Tool names and argument schemas remain stable. Missing-file
errors retain their exception type, errno and filename; recovery never silently
selects or edits a different file. Authored prompts, custom tool overrides,
native delegation aliases, approval checks and initialization diagnostics retain
their existing behavior.

## Real-model comparison

Fresh `gpt-6.1-sol` runs used the same user prompts and actual native tools in
isolated workspaces. Original development/heldout cases ran once per variant;
seven independently authored novel families ran once, followed by a frozen
confirmation run of the original heldout plus novel cases. No candidate tuning
occurred between these repetitions. At most two root-only trials ran concurrently.
The orchestration smoke ran separately because it starts children.

| Metric | Baseline | Retained candidate |
| --- | ---: | ---: |
| Trials | 44 | 44 |
| Actionable tasks completed | 37/37 | 37/37 |
| Manually reviewed controls handled correctly | 7/7 | 7/7 |
| Total reported tokens | 218,121 | 211,190 |
| Uncached prompt tokens | 209,441 | 204,790 |
| Native tool calls | 122 | 115 |
| Model generations | 165 | 155 |
| Automatically classified avoidable failures | 0 | 0 |

Observed totals fell **3.2% for tokens**, **5.7% for tool calls** and **6.1% for
model generations**. Both variants completed every actionable trial. Controls
comprised denied edits and requests needing missing-file or target clarification;
the latter attempted no edits, and every final response was manually reviewed.
They are not counted as completed requested edits.

The gain is modest and uneven. The first original-suite comparison used 4.0%
fewer tokens and 11.3% fewer calls; the first novel suite used 3.6% more tokens.
Its nested discovery task did extra inspection. The confirmation batch used
4.8% fewer tokens and 8.0% fewer calls. These small authored samples do not prove
a population-wide improvement or a lower avoidable-error rate. No such errors
occurred in either variant. Changed error feedback is also covered by focused
offline tests; it is not credited with a measured reduction in live errors.

Every comparison trial finished normally with complete reported usage and
unchanged imported Python source hashes. Captured user messages matched the
fixture prompt exactly, and paired prompt hashes matched. Hidden graders and
expected outputs were never added to the messages. Failed attempts remain in
the traces. Raw results, per-trial counts and hashes are indexed in
[results/2026-10-08-improvement.json](results/2026-10-08-improvement.json).

A separate native delegated review-and-correction smoke completed with two
successful children, zero tool errors, verified saved output, complete usage and
unchanged source. It is a functional regression check, not evidence of improved
child-agent efficiency. Its raw result is under
`/home/abja/nagents-artifacts/harness-improve-orchestration/`.

Final review then corrected constructor-time configuration validation and native
delegation alias rendering. Those edge fixes leave initialized canonical-tool
prompt text unchanged and have focused regressions. A post-review live filename-typo
smoke exercises the initialized canonical implementation separately; it is excluded from the
comparison totals. Its trace is under
`/home/abja/nagents-artifacts/harness-improve-final-smoke/`.

The original rejected candidates and their full evidence remain in
[RESULTS.md](RESULTS.md). The novel cases were frozen before candidate runs,
including a pre-run fixture correction ensuring a new file's parent exists.
No unavailable shell operation is silently expected by their grader.
