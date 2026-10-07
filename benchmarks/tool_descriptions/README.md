# Tool schema comprehension benchmark

Opt-in real-model evaluation of the tool definitions registered by an initialized shipping `Harness`, using a temporary private workspace and the normal locally authenticated `OpenAIProvider`. Generated calls are recorded and graded, **never executed**. This does not run shell commands, create child agents, or grant approvals. A run sends 12 requests per repeat; concurrency bounds simultaneous requests. Credentials are never included in result records and exception messages are deliberately omitted. Schema capture uses a non-demo harness with an isolated deferred provider and performs no generation; the demo-only `demo_preview` tool is excluded.

```sh
.venv/bin/python -m benchmarks.tool_descriptions.run \
  --model gpt-6.1-sol --concurrency 4 --repeats 3 \
  --variant current --output /tmp/ngn-tools-current
.venv/bin/python -m pytest benchmarks/tool_descriptions/test_run.py
```

Use `--suite observed` for the two CRLF edit scenarios with native-shaped prior assistant/tool messages, exact fixture SHA-256 values, and renamed tool references matching each variant. These remain synthetic histories, not executed tool reads.

Use `--suite challenge` for 11 harder boundary cases (9 automatically graded, 2 manually reviewed), or `--suite all` for both sets. The challenge suite includes original CRLF preservation, unique edits with duplicate text, whole-file replacement, line deletion, large line windows, filename references in contents, millisecond conversion, self-contained delegation, and oversized-file/unsupported-deadline handling. Edit grading applies the proposed literal replacement to a fixed in-memory fixture and checks the complete resulting text; it never writes files. Delegation grading checks only filename/focus/profile coverage, so manually review the prose as well.

Output must be a new directory. Its schema snapshot/hash, prompt-suite and runner-code hashes, variant mappings, per-case raw calls, response text, errors, wall timing, reported token usage, and summary are saved privately (directory 0700, files 0600). Authentication comes from normal local Codex configuration; missing authentication produces failed cases rather than prompting for credentials.

Variants are cloned in memory and never modify production schemas:

- `current`: actual shipping names, descriptions, and argument schemas.
- `clarified`: descriptions only; names, required arguments and types remain unchanged.
- `tool_names`: tool names only, with corresponding lexical references updated.
- `arg_names`: argument names only, with corresponding lexical references updated.
- `explicit_names`: both tool and argument names, with corresponding lexical references updated.

All name variants preserve schema types/requirements and translate generated calls back to canonical names before grading. Clarification appends guidance to existing descriptions and argument descriptions, retaining the original contract.

The 12 fictional scenarios cover 1-based line offsets vs counts, literal search, filename globs, edit vs creation, pre-read requirements, bounded read size, default/explicit shell timeout, independent delegation, and absent task-polling tools. Deterministic grading validates shipping argument schemas and narrowly specified expected actions/arguments. Omitted documented defaults and equivalent `./`/trailing-slash paths are accepted. Task polling only checks nonempty text with no calls; it is marked for manual review and excluded from the automated score. `passed` counts automated successes out of `automated_cases`; `structural_passed` additionally counts structurally valid manual cases. Review the saved explanation before reporting a task-status success.

These are one-turn probes, not end-to-end task completion or provider throughput benchmarks. Existing-file/read history is stated in the prompt rather than produced by real tool execution. Calls never encounter real filesystem state or approval. Small stochastic samples, shared provider load and fixed prompts limit causal claims; compare multiple repeats/models and review individual failures before changing production descriptions. Times include response latency and do not measure harness tool runtime. API/provider errors count as failures and are recorded separately from rubric failures.

Provider-reported prompt/completion/cached/reasoning/audio token counts are retained per case and summed across cases. Repeated cumulative usage on different stream events is merged by field maxima, not summed. `usage_reported`/`cases_with_usage` distinguish missing telemetry from measured zero. `schema_chars` measures compact JSON characters of shared schemas, not tokens or full provider wire size. Cached and reasoning tokens are subsets, not additional totals.

Exploratory runs made before the non-demo schema correction included an irrelevant `demo_preview` tool and omitted usage telemetry. Keep those reports separate from corrected runs; compare schema hashes/provenance before pooling results.
