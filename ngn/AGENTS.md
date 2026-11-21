# ngn

Independent Rust harness (`ngn`, pronounced "engine"), TUI and web host. `nagents` (pronounced "N-agents") is an optional Python agent library/runtime connected over the versioned Agent Runtime Protocol, never a UI dependency.

- Keep protocol types independent of core, transports and UI.
- Execute tools only in the Rust host after validation and approval. The Python
  adapter must not install local filesystem tools or load arbitrary plugins.
- Never replay tools or automatically retry interrupted runs.
- Normal state-opening startup and the explicit upgrader may copy supported
  display history one way from a consistent private snapshot into a separate
  Rust-owned store. Never open the original as a writable or live Rust store,
  modify it or its sidecars, import credentials or tool state, or replay history.
- Without `--config`, read (never write) the existing Python ngn provider files
  from their existing paths (`ngn-core/src/legacy.rs`). Do not introduce a new
  automatic config path.
- Run `cargo fmt --all --check`, `cargo clippy --workspace --all-targets -- -D warnings`,
  `cargo test --workspace` and `pre-commit run --all-files` before completion.
- Run Python checks in an explicit virtual environment. Do not replace an existing venv.
- Document incomplete migration features; do not imply legacy parity.
