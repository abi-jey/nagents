# ngn

Independent Rust harness, TUI and web host. `nagents` is an optional Python agent
runtime connected over the versioned Agent Runtime Protocol, never a UI dependency.

- Keep protocol types independent of core, transports and UI.
- Execute tools only in the Rust host after validation and approval. The Python
  adapter must not install local filesystem tools or load arbitrary plugins.
- Never replay tools or automatically retry interrupted runs.
- Never write to or open a Python ngn database as a live Rust store. An explicit
  upgrader may read a consistent backup and copy supported history into separate
  Rust-owned databases; startup must never implicitly import or replay it.
- Without `--config`, read (never write) the existing Python ngn provider files
  from their existing paths (`ngn-core/src/legacy.rs`). Do not introduce a new
  automatic config path.
- Run `cargo fmt --all --check`, `cargo clippy --workspace --all-targets -- -D warnings`,
  `cargo test --workspace` and `pre-commit run --all-files` before completion.
- Run Python checks in an explicit virtual environment. Do not replace an existing venv.
- Document incomplete migration features; do not imply legacy parity.
