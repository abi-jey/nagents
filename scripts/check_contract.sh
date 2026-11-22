#!/usr/bin/env bash
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
PYTHONPATH="$root/src" python "$root/scripts/export_contract.py" --check
python "$root/scripts/generate_rust_api.py" --check
