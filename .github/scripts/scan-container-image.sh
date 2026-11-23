#!/usr/bin/env bash
set -euo pipefail

trivy=$1
layout=$2
image=$3
digest=$4
work=$5
reports="$work/reports"
summary="$reports/summary.md"
cache="$work/cache"
mkdir -p "$reports"

trap 'if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then cat "$summary" >> "$GITHUB_STEP_SUMMARY"; fi' EXIT
printf '## Trivy runtime image audit\n\nCandidate OCI digest: `%s` (Linux/amd64)\n\n' "$digest" | tee "$summary"
[[ "$digest" =~ ^sha256:[a-f0-9]{64}$ ]]

# Use one fresh snapshot for both scans and disable implicit repo configuration.
"$trivy" image --cache-dir "$cache" --config '' --skip-version-check --download-db-only \
    2>&1 | tee "$reports/database.log"
cp "$cache/db/metadata.json" "$reports/database.json"
"$trivy" --cache-dir "$cache" --version | tee "$reports/version.txt"
printf 'Trivy and database versions:\n\n```\n' >> "$summary"
cat "$reports/version.txt" >> "$summary"
printf '```\n\n' >> "$summary"

scan() {
    local name=$1
    shift
    "$trivy" image --cache-dir "$cache" --config '' --ignorefile '' --skip-version-check --skip-db-update \
        --scanners vuln --pkg-types os,library --severity UNKNOWN,LOW,MEDIUM,HIGH,CRITICAL \
        --platform linux/amd64 --list-all-pkgs --exit-code 0 --format json --output "$reports/$name.json" "$@" \
        2>&1 | tee "$reports/$name.log"
}

scan candidate --input "$layout"
baseline_digest=$(docker buildx imagetools inspect "$image:latest" --format '{{.Manifest.Digest}}')
[[ "$baseline_digest" =~ ^sha256:[a-f0-9]{64}$ ]]
baseline="$image@$baseline_digest"
printf 'Published baseline: `%s`\n\n' "$baseline" | tee -a "$summary"
scan baseline --image-src remote "$baseline"

python3 .github/scripts/report_container_audit.py \
    "$reports/baseline.json" "$baseline" "$reports/candidate.json" "$layout" "$digest" | tee -a "$summary"

# The publishing step copies the content-addressed OCI layout, without rebuilding.
printf 'baseline_digest=%s\n' "$baseline_digest" >> "$GITHUB_OUTPUT"
