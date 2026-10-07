# Second optimization cycle: real-model evidence

These are native `gpt-6.1-sol` Harness trials, not scripted model responses.
The baseline runtime was `355adb0`; the combined live candidate was `e7ce8f5`.
Each strategy used the same three tasks with two repetitions, a 12-round test
budget, and the same model and fixture/goal/strategy hashes. One root trial ran
at a time; delegated trials used native overlapping children. Source stayed
unchanged within every trial.

The candidate removes repeated generated context from ordinary child prompts,
uses compact lossless JSON for supported tool results in model history, and
explains evidence reuse and automatic native-file instruction discovery.
Authored/custom instructions, permission ceilings, original public result
objects, and saved tool-file representations remain preserved.

## Final matched successful trials

| Metric | Direct baseline | Direct candidate | Delegated baseline | Delegated candidate |
| --- | ---: | ---: | ---: | ---: |
| Complete paired trials | 6 | 6 | 5 | 5 |
| Total tokens | 41,965 | 32,924 | 192,654 | 140,286 |
| Input tokens | 40,538 | 31,693 | 185,365 | 130,513 |
| Uncached input tokens | 40,538 | 31,693 | 179,989 | 128,977 |
| Output tokens | 1,427 | 1,231 | 7,289 | 9,773 |
| Provider generations | 28 | 21 | 91 | 71 |
| Tool calls | 33 | 25 | 99 | 65 |
| Summed run seconds | 86.56 | 68.88 | 207.75 | 238.25 |

Direct trials used 21.5% fewer total tokens and 24.2% fewer calls. The five
complete delegated pairs used 27.2% fewer total tokens and 34.3% fewer calls.
Delegated output tokens increased 34.1% and summed latency increased 14.7%.
This is evidence of input-token and call savings, not a universal speedup or
billing claim. Cache warmth, variable model plans, and upstream load matter.

The baseline completed 12/12 trials. The candidate completed 11/12 first attempts;
`policy_review-delegated-0` encountered `CODEX_CONNECTION` after its children
completed. Its incomplete usage is excluded from paired efficiency comparisons,
and the corresponding baseline trial is excluded from that table too. A single
separately recorded infrastructure retry on identical source passed: 21,886 total
tokens, 20,879 uncached input tokens, ten calls, and 26.6 seconds. It is not pooled
into the first-attempt results. All thirteen candidate attempts retained identical
runtime source hashes; failed artifacts were not deleted.

## Isolated iterations and independent validation

- Child-context deduplication alone passed six delegated trials: total tokens
  decreased 12.2%, child input tokens decreased 32.2%, while calls increased 4.7%
  and summed latency increased 11.3%. Custom-prompt fallback remains conservative.
- Compact model-history serialization alone passed six direct trials: total
  tokens decreased 7.9%, calls 33 to 31, and generations 28 to 26. Four pairs with
  unchanged generation counts decreased 1.7% in aggregate tokens. Different
  model plans prevent attributing the full difference solely to serialization.
  Quoted code can grow in bytes when encoded as JSON; byte length is not a token
  guarantee. Saved output files retain their original format.
- [Intent evaluation](../intent/RESULTS.md) used separate development and sealed
  heldout cases. The final heldout comparison retained 10/10 correct outcomes
  while reducing calls 14 to nine and tokens 36,097 to 30,107. An additional
  wording experiment was rejected after it failed to improve both token and
  call efficiency on matched successful development cases.

Full regression validation caught offline-demo parsing that assumed Python
literals. A compatibility follow-up accepts both JSON and persisted legacy
literals. That demo-only fix and semantic test-assertion updates followed the
frozen live trials; they do not change the evaluated live provider path.

## Reproduction and private artifacts

Use the commands and caveats in [README.md](README.md). Explicitly select the
runtime checkout with `PYTHONPATH`; reports record the actual imported source
roots and file hashes, independent of the benchmark runner's checkout.

Private artifacts from this session:

- `/tmp/ngn-cycle2-baseline-orchestration`
- `/tmp/ngn-cycle2-context-orchestration`
- `/tmp/ngn-cycle2-output-orchestration`
- `/tmp/ngn-cycle2-final-orchestration`
- `/tmp/ngn-cycle2-final-orchestration-infra-retry`
- `/tmp/ngn-cycle2-heldout-baseline` and `/tmp/ngn-cycle2-heldout-final`

These small controlled fixtures and two repetitions do not establish a broad
model ranking or an optimal tool policy for every task. Report correctness,
incomplete usage, failed attempts, output tokens, and latency alongside savings.
