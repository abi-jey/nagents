# Architecture decision records

Each record states one decision: the context that forced it, what was decided, what was rejected and what it
costs. Records are numbered and never renumbered. A decision that changes is superseded by a new record, and the
old one says so in its status line.

| ADR | Title | Status |
| --- | --- | --- |
| [0001](0001-python-core-rust-tui.md) | Python core, Rust TUI client | Proposed |
| [0002](0002-web-oidc.md) | Built-in OIDC for the private web workspace | Accepted |
| [0003](0003-agent-pod-network-access.md) | Enforce and audit the agent pod's network access | Proposed |

See [Python engine runtime support](0001-runtime-status.md) for the implemented
v1 scope and remaining runtime gaps in ADR 0001.

## Format

```markdown
# NNNN. Title

- Status: Proposed | Accepted | Superseded by NNNN
- Date: YYYY-MM-DD

## Context
## Decision
## Alternatives considered
## Consequences
```

A record may add sections after these, such as a migration plan, when the decision needs one to be actionable.
