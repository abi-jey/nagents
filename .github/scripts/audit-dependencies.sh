#!/usr/bin/env bash
set -euo pipefail

osv=$1
pip_audit=$2
cargo_audit=$3
audit_dir=$4
reports="$audit_dir/reports"
summary="$reports/summary.md"
mkdir -p "$audit_dir"
mkdir "$reports"
: > "$summary"
if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    trap 'cat "$summary" >> "$GITHUB_STEP_SUMMARY"' EXIT
fi
# Do not pick up implicit OSV ignore rules from the checkout.
config="$audit_dir/osv-scanner.toml"
: > "$config"

failed=0
say() {
    printf '%s\n' "$@" | tee -a "$summary"
}

tool_version() {
    local source=$1 version
    shift
    if version=$("$@" 2>&1); then
        say "- ${version//$'\n'/; } — $source"
    else
        say "- ERROR: Could not identify the tool for $source."
        failed=1
    fi
}

scan() {
    local tool=$1 scope=$2 source=$3 code=0
    shift 3
    local report="$reports/$tool-$scope.json" log="$reports/$tool-$scope.log"
    # Collect every result even when an earlier scanner found a problem.
    printf '\nRunning %s against %s\n' "$tool" "$source"
    timeout 120s "$@" > "$report" 2> "$log" || code=$?
    if (( code )); then
        sed 's/::/%3A%3A/g; s/^/    /' "$log"
    fi
    if ! python .github/scripts/report_dependency_audit.py "$tool" "$source" "$code" "$report" | tee -a "$summary"; then
        failed=1
    fi
}

osv_scan() {
    scan osv "$1" "$2" "$osv" scan source --all-vulns --no-resolve --config "$config" \
        --all-packages --format json --lockfile "$3"
}

python_scan() {
    scan pip-audit "$1" "$2" "$pip_audit" --vulnerability-service pypi --strict --disable-pip --no-deps \
        --format json --desc off --aliases on --progress-spinner off --cache-dir "$audit_dir/pypi-cache" --requirement "$3"
}

say "## Dependency audit" "" "Candidate: \`${GITHUB_SHA:-local}\`" "" \
    "Package counts are per scan and overlap across inputs and tools." ""
tool_version "OSV advisory database" "$osv" --version
tool_version "PyPI advisory service" "$pip_audit" --version
tool_version "RustSec advisory database" "$cargo_audit" audit --version
tool_version "npm advisory service (npm CLI version)" npm --version
tool_version "Python dependency resolver" python --version

osv_scan poetry poetry.lock poetry.lock
osv_scan web-ui src/nagents/web-ui/package-lock.json src/nagents/web-ui/package-lock.json
osv_scan cargo ngn/Cargo.lock ngn/Cargo.lock
scan npm web-ui src/nagents/web-ui/package-lock.json npm --prefix src/nagents/web-ui audit \
    --package-lock-only --include=dev --include=optional --include=peer --audit-level=info \
    --registry=https://registry.npmjs.org --json
scan cargo-audit cargo ngn/Cargo.lock "$cargo_audit" audit --file ngn/Cargo.lock --format json \
    --db "$audit_dir/rustsec-advisory-db" --url https://github.com/RustSec/advisory-db --deny unsound

# pip-audit rejects multiple versions of the same name in one requirements
# file. Separate those versions so marker-specific Poetry pins are all checked.
if python - "$audit_dir" <<'PY'
import re
import sys
import tomllib
from collections import defaultdict
from pathlib import Path

packages = defaultdict(set)
for item in tomllib.loads(Path("poetry.lock").read_text(encoding="utf-8"))["package"]:
    packages[re.sub(r"[-_.]+", "-", item["name"]).lower()].add(item["version"])
if not packages:
    raise ValueError("Empty Poetry lockfile")
batches = [[] for _ in range(max(map(len, packages.values())))]
for name, versions in sorted(packages.items()):
    for index, version in enumerate(sorted(versions)):
        batches[index].append(f"{name}=={version}\n")
for index, pins in enumerate(batches, 1):
    (Path(sys.argv[1]) / f"poetry-pins-{index}.txt").write_text("".join(pins), encoding="utf-8")
PY
then
    for pins in "$audit_dir"/poetry-pins-*.txt; do
        python_scan "${pins##*/}" "poetry.lock (${pins##*/})" "$pins"
    done
else
    say "- ERROR: Could not extract Poetry pins for pip-audit."
    failed=1
fi

for requirement in benchmarks/terminal_bench/requirements.txt contracts/engine/v1/requirements.txt; do
    parent=${requirement%/*}
    osv_scan "${parent##*/}" "$requirement" "$requirement"
    python_scan "${parent##*/}" "$requirement" "$requirement"
done

# The standalone installers ship these exact wheels, not the Poetry resolution.
for lock in ngn/runtime-lock{,.macos-arm64,.macos-x86_64}.json; do
    dir="$audit_dir/${lock##*/}"
    mkdir -p "$dir"
    if jq -er '.wheels[] | .name + "==" + .version' "$lock" > "$dir/requirements.txt"; then
        osv_scan "${lock##*/}" "$lock (wheel pins)" "$dir/requirements.txt"
        python_scan "${lock##*/}" "$lock (wheel pins)" "$dir/requirements.txt"
    else
        say "- ERROR: Could not read wheel pins from $lock."
        failed=1
    fi
done

# Test jobs and Runtime.Dockerfile resolve Python dependencies without Poetry.
for extras in dev,voice,web web,a2a; do
    scope="pip-${extras//,/-}"
    dir="$audit_dir/$scope"
    mkdir -p "$dir"
    printf '\nResolving Python dependencies for .[%s]\n' "$extras"
    if timeout 120s python -m pip install --quiet --no-input --disable-pip-version-check --dry-run --ignore-installed \
        --report "$dir/resolution.json" ".[$extras]" > "$reports/$scope-resolution.log" 2>&1 \
        && jq -er '.install[] | .metadata | select((.name | ascii_downcase) != "nagents") | .name + "==" + .version' \
            "$dir/resolution.json" > "$dir/requirements.txt"; then
        osv_scan "$scope" "Python 3.12 resolution for .[$extras]" "$dir/requirements.txt"
        python_scan "$scope" "Python 3.12 resolution for .[$extras]" "$dir/requirements.txt"
    else
        sed 's/::/%3A%3A/g; s/^/    /' "$reports/$scope-resolution.log"
        say "- ERROR: Could not resolve .[$extras] for auditing."
        failed=1
    fi
done

if (( failed )); then
    say "" "**Dependency audit failed.** See findings or errors above and download the JSON and log artifacts."
else
    say "" "**All dependency scans passed.** This means no known findings in the inputs and databases listed above."
fi
exit "$failed"
