# ADR 0001: Python engine runtime support

This describes the `/api/v1` engine that every ngn client uses since 0.100.47,
when the Rust host was removed (see the
[ADR's migration plan](0001-python-core-rust-tui.md)). It uses the existing
Python `WebState`, Harness, session database, and inbox dispatcher.

## Supported behavior

- Session list, create, get, rename and fork use the existing workspace
  records. Creating or forking does not change another client's selection.
  A fork copies display history without copying approvals, live run IDs,
  tasks or queue authority.
- Run and steering requests are admitted durably. Run request UUIDs are scoped
  to a session; steering UUIDs are scoped to the exact run. An unchanged retry
  returns its existing receipt. Run receipt lookup never submits work.
- Engine queue pause, resume, clear and cancellation affect only the intended
  engine inputs. On restart, a claimed run is interrupted, not replayed.
  Unclaimed engine work is held until an operator resumes its queue.
- Local text steering claims the bounded, already-admitted FIFO prefix after a
  complete tool batch or at the end of a response. Every input has its own
  receipt. The prepared user messages and their origins commit together before
  a later model request. An unclaimed input is undelivered when its run ends;
  a claimed input without committed history is uncertain and is not replayed.
  This does not change browser follow-ups and does not batch A2A or batch API
  conversations.
- Interactive approval requires the live nonce, run and call. Only the first
  valid decision wins. A noninteractive engine run denies requests that reach
  the Harness approval gate; it does not inherit browser allow rules. An
  approval shown in replay is not authority to resume it.
- The event stream merges durable replay and live-only events in increasing
  sequence order. Live text deltas and `engine.connected` reserve sequence IDs
  but their payloads are not retained. A slow listener reconnects at its last
  received cursor and can recover the durable transcript.
- Local child activations record their root and exact parent when admitted.
  A scoped stream carries the conversation and its descendants on one socket;
  child inspection loads durable evidence from that child's history endpoint.
  Child approvals and cancellation address the exact activation. If an owner
  exits before a child's completion arrives, its history is marked interrupted.
- Optional ABI3 helpers keep the Git matcher and diff algorithm of the retired Rust host.
  With `NGN_NATIVE_DISCOVERY=1`, Rust scans and searches under a verified
  workspace directory. Python still supplies the path policy, loads workspace
  instructions, handles approvals, and owns all writes.
- On first start in a workspace the engine copies root display history from
  the retired Rust host's default schema-5 database, through a private backup;
  `nagents import-host` does the same for a backup from elsewhere. Archive rows
  and the one-time marker are written in a single transaction. Imported
  identities can never start or resume work.

## Gaps

- `/history` replays the v1 journal, including explicit archive imports.
  The offline import does not restore provider context, child conversations,
  queued work or old approvals, and never modifies the old Rust database.
- The child stream projects text and structured tool status, not every
  existing Python web feature (such as attachments, remote task artifacts,
  child follow-up editing, or voice).
- Native discovery/search are optional and not in the bundle; they have a
  same-fixture Linux benchmark, not broad workload and platform parity.
  Imported Rust host sessions cannot be continued.
