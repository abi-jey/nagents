# Trust model

This is a **trusted local coding assistant**, not an OS sandbox. The operator
trusts the selected workspace, explicit configuration and executable agent runtime.
Provider/model output and tool arguments are untrusted input, not authorization.

## Host tools

- The model receives schema metadata, never native function pointers or Python code.
- Rust validates tool identity, strict argument shape, bounds, mode and paths.
- Unknown tools and modifying calls in read-only/demo mode fail closed, even if
  the runtime emits a call that was not advertised.
- Shell and file mutations need one-time host/UI approval. Scripts cannot approve.
- Approval IDs are host-generated, bound to a run and exact prepared operation;
  they expire after five minutes and are discarded on cancellation/shutdown.
- File tools reject traversal, symlinks, hard links, special files and common
  credential/internal paths. They use capability-relative directory handles.
- Existing files require an exact inspected SHA-256. Mutations re-read the file
  after approval and immediately before commit. Overwrites use a temporary file
  and rename. No tool claims a failed or interrupted action was rolled back.
- Reads are limited to 256 KiB; tool output and discovery/search are bounded.
- Shell output is drained with bounded retained buffers. Timeouts/cancellation
  stop the process group on Unix. These are user-level process controls, not a sandbox.

The credential list is a guardrail, not a secret scanner. An approved shell,
malicious trusted runtime, separate same-user process, network filesystem or hostile
concurrent filesystem mutation is outside the isolation claim. A runtime can use
its inherited provider credentials; it must be trusted executable code. The
protocol ensures the supplied adapter does not execute tools locally; it cannot
stop an arbitrary malicious process from making its own system calls.

## Persistence and execution

State lives under a separate `ngn-rust` namespace, in private workspace directories
and regular private database files on Unix. Host/runtime databases have separate
owners and schemas. Host events include conversation and tool contents and should
be treated as sensitive. There is no telemetry upload or default provider wire log.

A file lock gives a single host ownership of one workspace state directory.
Restart marks active runs interrupted. It does not replay tools. Submission UUIDs
deduplicate identical retry requests; reusing one for different text is rejected.
This is not an exactly-once side-effect guarantee after cancellation or a crash.

## Web serving

0.100.0 binds loopback only. Every request must carry the exact printed Host.
Cross-origin fetches, foreign Origins, unsupported methods, arbitrary query fields
and oversized mutation bodies are rejected. Mutations need an exact Origin,
`application/json`, and a per-host token. WebSockets require the exact Origin and
authenticated subprotocols. Tokens are never accepted in URLs. Responses have
no-store, anti-framing, no-sniff, same-origin and restrictive CSP headers.

The page renders untrusted text with `textContent`/text nodes, never HTML injection.
The terminal strips control characters from model, tool and configuration output.
The web token is CSRF/rebinding protection, **not authentication against local
users**. No remote deployment, TLS termination, multi-user or tenant isolation is
claimed. Do not expose the loopback port through an unauthenticated proxy.

## Platform status

The initial implementation and smoke tests target Linux; POSIX adapter pipes and
Unix process groups are used. Windows is not a supported release target yet.
macOS requires separate validation. Do not assume process-tree cancellation or
filesystem semantics are identical on untested platforms.
