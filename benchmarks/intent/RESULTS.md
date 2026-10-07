# Development evidence: second optimization cycle

Real `gpt-6.1-sol` native Harness runs, ten frozen development cases per variant,
concurrency two. Baseline runtime: `355adb0`; independent guidance candidate:
`3b7210c`. The candidate adds evidence reuse and explains automatic applicable
AGENTS discovery by native file tools, with explicit project-instruction checks
before shell work. It preserves required inspection before edits and saved readback.
No heldout outcomes were inspected to choose this candidate.

| Metric | Baseline | Guidance candidate | Change |
| --- | ---: | ---: | ---: |
| Correct outcomes and intent compliance | 10/10 | 10/10 | unchanged |
| Tool calls | 16 | 11 | −31.25% |
| Provider generations | 25 | 20 | −20.00% |
| Total tokens | 39,155 | 32,019 | −18.23% |
| Prompt tokens | 38,524 | 31,491 | −18.26% |
| Uncached prompt tokens | 38,524 | 31,491 | −18.26% |
| Completion tokens | 631 | 528 | −16.32% |
| Cached tokens | 0 | 0 | unchanged |
| Summed per-trial run seconds | 68.94 | 54.24 | −21.33% |

Every trial completed with full reported usage, unchanged source hashes, no
provider error, and no child failure. Delegation was available; neither variant
spawned children for these small tasks. Root/child accounting remains active.
Summed run seconds are descriptive and are not elapsed matrix wall time.

Three cases account for the five removed calls. Literal content search used its
returned evidence rather than rereading the one-line match (2 → 1). Authorized
correction avoided preliminary directory and AGENTS searches while retaining
read → edit → saved readback (5 → 3). Advice-only avoided directory and AGENTS
searches but still read the named file (3 → 1), exceeding its zero-call efficiency
target. Ambiguous target selection retained four discovery/inspection calls and
asked whether staging, production, or both should change; manual review confirmed
the question resolves the missing choice. Other call counts did not change.
The added guidance increases prompt tokens in zero-call cases by about 50 tokens.

The original baseline exact-object grader incorrectly rejected harmless
`message`/`explanation` fields in two JSON answers (8/10). That raw result remains
frozen. The generic corrected grader compares required fields and accepts harmless
extras, used identically for candidate and future heldout runs. Regrading uses
saved files and ordered successful tool events, retaining native unexpected-file
and alias evidence. Advice inspection is correct but measured separately against
its soft zero-call target. No model call was repeated merely to fix grading.

Private evidence artifacts:

- `/tmp/ngn-intent-cycle2-baseline-development/summary.json` (original raw).
- `/tmp/ngn-intent-cycle2-baseline-development/regraded-v2.json` (references raw and grader hashes).
- `/tmp/ngn-intent-cycle2-candidate-development/summary.json`.

Original raw summary SHA256:
`6f8dcb6e8bef097a7fee30969bb2087937ff27e1cc1c0a43801f5ed26e469ab4`.

These are single runs of a small development set, not proof of a universal
accuracy, latency or token-efficiency improvement. Independent heldout validation
of the final combined candidate is still required.

## Further development iteration (rejected)

A second frozen experiment (`828f37d`, including `55ac910`) added a general
statement that mentioning a path alone does not require file inspection, and
narrowed instruction-discovery wording to redundant preliminary searches. Ten
development trials produced nine completed successes; the review-question trial
failed with `CODEX_CONNECTION` after its successful file read and has incomplete
usage. Its lower aggregate token count cannot be treated as an improvement.

On the nine matched successful cases, the first candidate used 28,886 tokens and
ten calls; the second used 29,175 tokens and ten calls (+1.00% tokens). Advice-only
eliminated its remaining read (1 → 0), while ambiguous target discovery added one
call (4 → 5). The extra path-relevance sentence was rejected for lack of a joint
token/call benefit. Both raw runs are preserved; no retry was used to chase an
improvement. The final guidance keeps the narrower wording “avoid redundant
preliminary searches” so explicit user requests to locate AGENTS files remain
authorized. That safety clarification will be validated with the final combined
candidate rather than attributed to the first candidate's development metrics.

Additional private artifact:
`/tmp/ngn-intent-cycle2-candidate2-development/summary.json`.
