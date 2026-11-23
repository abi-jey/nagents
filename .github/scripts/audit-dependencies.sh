#!/usr/bin/env bash
set -euo pipefail

scanner=$1
audit_dir=$2
mkdir -p "$audit_dir"
# Do not pick up implicit OSV ignore rules from the checkout.
config="$audit_dir/osv-scanner.toml"
: > "$config"

lockfiles=(
    poetry.lock
    src/nagents/web-ui/package-lock.json
    ngn/Cargo.lock
    benchmarks/terminal_bench/requirements.txt
    contracts/engine/v1/requirements.txt
)

# The standalone installers ship these exact wheels, not the Poetry resolution.
for lock in ngn/runtime-lock{,.macos-arm64,.macos-x86_64}.json; do
    dir="$audit_dir/${lock##*/}"
    mkdir -p "$dir"
    jq -er '.wheels[] | .name + "==" + .version' "$lock" > "$dir/requirements.txt"
    lockfiles+=("$dir/requirements.txt")
done

# Test jobs and Runtime.Dockerfile resolve Python dependencies without Poetry.
for extras in dev,voice,web web,a2a; do
    dir="$audit_dir/pip-${extras//,/-}"
    mkdir -p "$dir"
    printf '\nResolving Python dependencies for .[%s]\n' "$extras"
    python -m pip install --quiet --no-input --disable-pip-version-check --dry-run --ignore-installed \
        --report "$dir/resolution.json" ".[$extras]"
    jq -er '.install[] | .metadata | select((.name | ascii_downcase) != "nagents") | .name + "==" + .version' \
        "$dir/resolution.json" > "$dir/requirements.txt"
    lockfiles+=("$dir/requirements.txt")
done

# Scan individually so a missing or unreadable input cannot be hidden by a
# successful scan of another lockfile. Show all findings before failing the job.
failed=0
for lock in "${lockfiles[@]}"; do
    printf '\nAuditing %s\n' "$lock"
    "$scanner" scan source --all-vulns --no-resolve --config "$config" --lockfile "$lock" || failed=1
done
exit "$failed"
