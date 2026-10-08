# Public agent prompt style survey

Collected on 2026-10-08 to inform realistic evaluation user prompts. This is a
style/provenance survey, not a benchmark performance result. No model was called
and no dataset instruction was executed.

The private collection contains **2,014 exact, deduplicated prompt/context
strings from five sources**, plus **500 metadata-only SWE-bench Verified issue
records**. Raw text is outside the repository at
`/home/abja/nagents-artifacts/public-prompt-corpus-20261008/prompts.jsonl`; directory permissions are
700 and files 600. This durable workspace artifact is local, not a public download. `sources.json` records the source revisions and license choices.

| Source | Unique retained strings | Provenance / interpretation |
| --- | ---: | --- |
| Terminal-Bench | 241 | Curated original-task specifications |
| OSWorld | 369 | Curated desktop-task utterances |
| ToolSandbox | 91 | Authored static USER utterances, not observed human chats |
| tau2-bench | 413 | Synthetic user-simulator instruction fields, not agent-facing chat turns |
| WorkArena | 900 | Generated/configured sort and filter goal strings |
| SWE-bench Verified | 500 metadata-only | Real GitHub issue title/body; dataset text rights unresolved |

## Findings

Public agent benchmarks mostly provide authored, constrained tasks. They should
not be described as raw human requests. SWE-bench is human-origin, but GitHub
issues are selected engineering reports: median 143 words in Verified, with code
fences in 232 of 500 records. Human-origin does not imply vague, brief, or casual.
Terminal-Bench's median is 138 words; 170/241 instructions mention output/file/
format terms and 112/241 mention implementation/tool terms under simple regex
counts. These lexical indicators are approximate, not a semantic classifier.
OSWorld's median is 26 words, ToolSandbox's is seven, and tau2 retained context
fields have median 19 words. tau2 strings are fragments of simulator scenarios;
they must not be treated as complete human messages or 413 independent tasks.
WorkArena's 900 goals largely vary field names, values, and ordering within
regular templates; volume overstates linguistic diversity.

ToolSandbox is useful for short utterance style: deictic local-state questions,
relationship references, relative time, recent-interaction references, and even
an authored misspelling. This demonstrates plausible style, not its frequency
in real user traffic. The selection cannot estimate the frequency of vagueness,
typos, or omissions in an actual product's user population.

## Collection and exclusions

Repository trees, licenses and prompt-bearing files were fetched through native
GitHub APIs/raw URLs at recorded commit SHAs. JSON/YAML were parsed without
executing source code. Only `instruction`, static `USER` content, stored `goal`,
and tau2 `user_scenario.instructions` textual fields were retained. ToolSandbox
uses AST extraction of constant string literals and excludes interpolated
expressions; no scenario code was imported or run. WorkArena reads only stored
goals, without reconstructing goals from configurations or running the website.

Files can contain other fields during transport and parsing. Those fields were
not examined for their contents or persisted: solutions, patches, tests,
reference answers, evaluator/grader keys, reward criteria, initial states, and
other task configurations are excluded. Terminal YAML uses only `instruction`.
Benchmark tasks may still mention an expected output or contain reproduction
snippets within the public instruction itself; these remain exact source prose,
not separately collected hidden references.

SWE-bench's viewer endpoint returns current rows rather than pinned rows. The
current HF revision was recorded before collection, and this limitation is
explicit in its metadata statistics. The framework's MIT license does not
establish redistribution rights for issue text in a separate dataset whose card
has no clear license declaration. Only IDs, source URLs, hashes and style
statistics were saved for those issues. Viewer responses include additional
columns in transit; no answer columns were inspected or saved.

Exact UTF-8 SHA-256 hashes deduplicate text globally without whitespace
normalization. `duplicates.json` preserves alternate source lineage without
copying text again. This is a census of the selected prompt-bearing directories
and the three tau2 domains, not a random population sample. Extracted records
are strings, not necessarily whole tasks. Reviewed private scripts, complete license
notices, statistics and errors are alongside the corpus; collection reported
zero extraction errors. These scripts use the repository Python venv.

## Applying the findings to evaluation prompts

Author fresh user messages from intent plus realistic missing context. Keep the
scorer's exact correctness constraints in private evaluation definitions rather
than exposing tool names, API parameters, required call order, or grader hints
in the user message. Missing context should produce a justified clarification
or an evidence-based action, not an impossible task that silently expects a
particular guess.

Useful original hard-case templates (these are newly authored, not collected
source records):

- “find the message from the person I talked to last” — resolve conversational
  recency and identity from available history.
- “move that reminder to tomorrow evening” — establish the referred reminder
  and clarify the time if the product has no accepted default.
- “the total looks wrong can you fix it” — inspect the current artifact and
  identify the discrepancy before changing it.
- “change Alex's number to [number]” — disambiguate duplicate names.
- “get me the cheapest one that works with this” — establish the referent,
  compatibility constraints and current evidence.
- “can u sort this by owner then date” — preserve casual phrasing while checking
  the current table and whether direction is meaningfully ambiguous.

Evaluate intent understanding, context lookup, warranted clarification,
permission boundaries, and final artifact correctness separately. Do not score
an exact tool-call spelling unless the evaluation specifically targets a tool
interface. Preserve attribution and license notices if using corpus text.

## Durable archive and reproduction

[The corpus archive](/home/abja/nagents-artifacts/public-prompt-corpus-20261008)
contains the exact corpus snapshot, source trees pinned by commit SHA, original
license notices, metadata-only SWE records and reviewed extraction scripts.
[RUNNING.md](/home/abja/nagents-artifacts/public-prompt-corpus-20261008/RUNNING.md)
documents dependencies, script order and explicit `--output-dir` arguments.
Use a separate copy of the archive to rerun extraction: collectors replace
outputs and the snapshot should remain available for comparison.

The scripts are archived research helpers, not a maintained turnkey library.
They use fixed source selections from the archived repository trees and fetch
only those pinned repository URLs. They parse but never execute dataset content,
and serialize only approved prompt fields. Direct dependencies are pinned in
`requirements.txt`. The SWE viewer helper cannot reproduce historical rows from
its recorded SHA and must be treated as a new current-data metadata survey on
rerun. Repository extraction should reproduce the same text set while file
ordering and representative duplicate lineage may differ from the first run.
No hidden benchmark contents were added to the archive during this review.
