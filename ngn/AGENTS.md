# ngn

Independent Rust harness, TUI and web host. `nagents` is an optional Python agent
runtime connected over the versioned Agent Runtime Protocol, never a UI dependency.

- Keep protocol types independent of core, transports and UI.
- Execute tools only in the Rust host after validation and approval. The Python
  adapter must not install local filesystem tools or load arbitrary plugins.
- Never replay tools or automatically retry interrupted runs.
- Never mutate, migrate or import a legacy nagents database. Without `--config` or
  `~/.config/ngn/rust.json`, read (never write) the Python ngn provider files so
  the same connection and model are selected as before (`ngn-core/src/legacy.rs`).
- Run `cargo fmt --all --check`, `cargo clippy --workspace --all-targets -- -D warnings`,
  `cargo test --workspace` and `pre-commit run --all-files` before completion.
- Run Python checks in an explicit virtual environment. Do not replace an existing venv.
- Document incomplete migration features; do not imply legacy parity.
