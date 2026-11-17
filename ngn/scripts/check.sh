#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$root/ngn"
export PYTHONPATH="$root/ngn/integrations/nagents/src:$root/src${PYTHONPATH:+:$PYTHONPATH}"
case "${1:-all}" in
  rust-format) cargo fmt --all --check ;;
  rust-clippy) cargo clippy --workspace --all-targets --locked -- -D warnings ;;
  rust-tests) cargo test --workspace --locked ;;
  browser-syntax) node --check web/app.js ;;
  browser-tests) node --test web/app.test.js ;;
  python-*)
    test -n "${VIRTUAL_ENV:-}" || { echo 'Activate an explicit Python virtual environment first.' >&2; exit 1; }
    case "$1" in
      python-lint) python -m ruff check integrations/nagents scripts ;;
      python-format) python -m ruff format --check integrations/nagents scripts ;;
      python-types) python -m mypy --strict integrations/nagents/src integrations/nagents/tests ;;
      python-tests)
        python -m unittest discover -s integrations/nagents/tests -v
        python -m unittest discover -s scripts -p 'test_*.py' -v ;;
      *) exit 2 ;;
    esac ;;
  all)
    for check in rust-format rust-clippy rust-tests python-lint python-format python-types python-tests browser-syntax browser-tests; do
      bash "$root/ngn/scripts/check.sh" "$check"
    done ;;
  *) echo "Unknown check: $1" >&2; exit 2 ;;
esac
