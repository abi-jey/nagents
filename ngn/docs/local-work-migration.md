# Preserved local Python work and the Rust application

## Preservation and reconciliation

The original `/home/abja/nagents` worktree contained 46 changed or untracked source
and test files on top of `bf2edfa`. Those files were backed up with SHA-256 hashes
outside the checkout, committed as `222093d` on
`preserve/local-python-before-rust-20261117`, and pushed before changing the checkout.
The preservation commit passes root pre-commit and its 85 focused tests. Its only
pre-commit repair is formatting and type narrowing in an ignore-discovery test;
the exact original bytes remain in the external backup.

Comparison against main `e7f94b3` found 26 byte-identical files. The remaining
differences are later fixes and additions on main, including managed shells,
bounded cancellation cleanup, generator closure, native OpenAI route selection,
Gemini model-name normalization, malformed reasoning validation, and narrower
terminal layouts. The old worktree's commits are already ancestors of main.

The preservation history is merged into the Rust integration branch while retaining
the reviewed main tree. In particular, older provider implementations are **not**
copied over the newer implementations. The temporary preservation branch is removed
only after verified integration. Nested development worktrees are not source files,
not submodules to publish, and not implicitly deleted.

## Where each group belongs

| Original local work | Owner and disposition |
| --- | --- |
| Provider backend facade/factory, Chat Completions, Responses, Messages and Gemini implementations | Retained Python core on main; used by the Rust runtime through public `Provider` APIs. Not translated into a second provider implementation in Rust. |
| Direct Anthropic prompt caching and gateway-specific behavior | Retained core and regression tests; the installed local-core wheel carries this into a Rust live run. Provider/account validation remains separate. |
| Active tool timing, compact durations, approval-wait exclusion and target summaries | Legacy Python behavior retained; 0.100.2 implements native call-owned clocks, exact approval snapshots, compact time units and recognized targets in both clients. Host/UI regressions exercise them independently. |
| Root/nested instruction deduplication and ignore-aware discovery | Legacy Python behavior retained. Native list/search now honor bounded workspace-root/nested `.gitignore`, including explicit starting paths, without attaching instruction contents. Rust root instructions avoid duplication; nested instruction/skill loading is not implemented. |
| Safe-boundary steering, durable FIFO choices and touch composer behavior | Legacy implementation retained. Native touch Return/focus, mobile conversation selection and approval-preview layout are implemented and tested; Rust's durable FIFO is distinct from active-run steering. No silent Python-harness fallback is installed. |
| Tests, usage dashboard work and existing branch commits | Already retained by current main; the preservation merge records provenance rather than reverting later fixes. |

The migration does not delete `src/nagents/`, change the Python version, rewrite
the published `v0.100.0` tag, or import legacy configuration/databases. Native parity
changes ship as later tested `0.100.x` releases.

## Other-branch review

A read-only audit compared historical branches and worktrees to `e7f94b3`, checking
patch content as well as ancestry because squash merges hide ancestry equivalence.
The priority Python identity/Codex, voice, steering, managed-shell, MCP, settings
and A2A work is already integrated on main. The genuine outstanding queue candidate
is PR #275, whose reviewed history is included here but remains separately gated.
Six historical worktrees contained dirty work: two already-integrated fixes and
four unwired UI prototypes. Those checkouts and branches are left untouched; an
unmerged branch name alone is not evidence that its old implementation should win.

The public Python device-authentication extraction is a prerequisite, not a Rust
login feature. It keeps legacy imports compatible and serializes cooperative POSIX
refresh owners; ambiguous refresh attempts require fresh device login rather than
retrying a possibly spent renewable token. Rust subscription configuration, UI and
live account tests remain separate gates. Published `nagents 0.28.5` does not have
this new public API; a future official adapter using it needs a tested core release.

## One-command development

`./ngn/dev` builds an optimized Rust binary and installed wheels from current
working source, including uncommitted edits. `./ngn/dev --test` exercises the staged
installation with real local CLI/HTTP/WebSocket/PTY checks. This is the same
application and packaging layout as the release, not editable Python execution or
a separate development UI. Its manifest names the commit, working-source digest,
dirty status and artifact digests; it is not an official release manifest.

The full shared Python suite at the released baseline passed locally with
**6,345 passed, 10 skipped**. The combined native-discovery/public-auth/fixture
candidate passed **6,390 tests, 10 skips**, plus **59 Rust** and **16 browser**
regressions, all eight installed acceptance suites and actual desktop/touch Chromium.
Both Python runs used private temporary roots. `/tmp/opencode` is
group-writable on this machine and is deliberately rejected by private A2A and
credential-storage checks; the earlier full-suite run beneath it was invalid.
Use an owned, non-group-writable temporary path below a trusted ancestor for those
tests. Do not weaken storage ownership checks to make a local test directory pass.
If `/tmp` quota is exhausted, use a new private test/build directory on a roomy
owned filesystem rather than deleting checkouts, credentials or evidence. Give
each changing worktree its own Cargo target; mixed-source cached artifacts are
not valid compilation or test evidence.
