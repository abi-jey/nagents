# Natural tool-use experiment — 2026-10-08

Real `gpt-6.1-sol` calls through the native Harness, two isolated trials in
parallel, one trial per case and split per variant. There were **60 comparative
trials plus one separate CRLF smoke trial**. All generations reported complete
usage, all trials reached normal completion, and imported Python source hashes
were unchanged during each trial. No API errors or interrupted attempts occurred.
The user message exactly matched the fixture prompt in every recorded request.

| Variant | Actionable tasks completed | Denied controls preserved | Tool calls | Model generations | Total tokens |
| --- | ---: | ---: | ---: | ---: | ---: |
| Baseline | 18/18 | 2/2 | 57 | 77 | 102,888 |
| Candidate 1: direct-read guidance on read_file and find | 18/18 | 2/2 | 56 | 76 | 105,357 |
| Candidate 2: stronger guidance only on find | 16/18 | 2/2 | 44 | 64 | 88,826 |

**Both description candidates were rejected; production tool descriptions,
names and arguments remain unchanged by this experiment.** Candidate 1 saved
one call but increased tokens by 2.4%. Candidate 2 reduced tokens by 13.7% and
calls by 22.8%, but stopped short of completing both filename-typo tasks.
In those tasks the model read `settings.yaml`, searched the same exact name,
and asked the user for its location instead of discovering `settings.yml`.
The clarification is not an invalid tool call or fabricated answer; it is an
uncompleted task under this fixture's functional check. Counting it as an
efficiency win would reward doing less work.

## Failure and recovery evidence

Every variant produced two denied-edit errors and two stale-edit conflicts.
The denied controls retained their original files and the final responses
accurately said that approval had been denied. Every stale conflict recovered
through reread and corrected edit, preserving the externally changed setting;
the initial failure remains counted, with two further calls to recovery.

Candidate 2 also produced three first missing-file observations: both typo
cases and a check before creating a new note. These are not automatically
attributed to model misuse. The creation check is a candidate unnecessary call
for manual review, not proof of an invalid argument. No automatically classified
avoidable failures, repeated invalid attempts, wrong tool names or malformed
arguments occurred in these live trials. Such failures and recovery are covered
by offline regression tests, explicitly separate from these real-model results.

## Interpretation and limitations

These are small authored tasks, not a representative sample of production user
traffic. Development and heldout share ten task families with changed wording
and some values. They are not independent domains. Candidate wording was
motivated by development traces; each variant ran only once per case, in
sequential batches. Stochastic variation and batch order limit causal claims.
No general token-efficiency improvement has been established.

The classifier uses offered schemas, parameter descriptions and visible prior
tool feedback. It does not consult hidden expected artifacts to decide whether
a call was avoidable. Unknown errors and uncertain path aliases remain ambiguous.
Successful different tools are not automatically called recoveries. Interrupted
tool attempts are counted separately. Natural-language answer terms provide a
minimal score, not a semantic proof; final responses and failure traces were
also reviewed. Denied controls never count as completed requested edits.

This first native suite disables delegation and shell. Existing orchestration
benchmarks remain available; these results establish nothing about child-agent
performance. Further work should include multi-turn references, noisy larger
workspaces, ambiguous user intent and genuine human-origin requests before
broadening conclusions.

Machine-readable per-trial metrics, raw-trace hashes, source-manifest hashes and
local raw-result locations are in [results/2026-10-08.json](results/2026-10-08.json).
Raw private traces are retained under `/home/abja/nagents-artifacts/` and include
all offered schemas, input messages and native outcomes. The smoke trial is
separate and excluded from the comparison above (three calls, 5,251 tokens).
