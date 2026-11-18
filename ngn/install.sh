#!/bin/sh
# Version-bound bootstrap only. The native installer verifies and installs the
# complete Python-inclusive release; this script never invokes Python or pip.
set -eu

NGN_BOOTSTRAP_VERSION=0.100.2

fail() {
    printf 'ngn installer: %s\n' "$*" >&2
    exit 1
}

if [ "${1:-}" = --help ] || [ "${1:-}" = -h ]; then
    printf '%s\n' \
        'Install the self-contained ngn harness (Linux x86_64).' \
        'Usage: sh install.sh [--prefix DIRECTORY] [--bin-dir DIRECTORY]' \
        'No Python, pip, Rust or Node installation is required.' \
        'Existing unrelated ngn executables and application state are not replaced.'
    exit 0
fi

[ "$(uname -s)" = Linux ] || fail 'Only Linux x86_64 bundles are currently validated.'
case "$(uname -m)" in
    x86_64|amd64) ;;
    *) fail 'Only Linux x86_64 bundles are currently validated.' ;;
esac

command -v curl >/dev/null 2>&1 || fail 'curl is needed for this shell bootstrap; alternatively download and run the native ngn-installer-linux-x86_64 release asset directly.'
if command -v sha256sum >/dev/null 2>&1; then
    hash_tool=sha256sum
elif command -v shasum >/dev/null 2>&1; then
    hash_tool=shasum
else
    fail 'sha256sum or shasum is required to verify this bootstrap. The native installer needs neither.'
fi

umask 077
temporary=$(mktemp -d "${TMPDIR:-/tmp}/ngn-install.XXXXXXXX") || fail 'Cannot create private bootstrap directory.'
cleanup() {
    rm -f "$temporary/installer" "$temporary/SHA256SUMS"
    rmdir "$temporary"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM HUP

base="https://github.com/abi-jey/nagents/releases/download/v$NGN_BOOTSTRAP_VERSION"
asset=ngn-installer-linux-x86_64
download() {
    curl --fail --silent --show-error --location --max-redirs 5 \
        --proto '=https' --proto-redir '=https' --tlsv1.2 \
        --connect-timeout 30 --max-time 300 --max-filesize "$3" \
        --output "$2" "$1"
}

download "$base/SHA256SUMS" "$temporary/SHA256SUMS" 1048576
expected=
while IFS=' ' read -r checksum filename extra || [ -n "$checksum$filename$extra" ]; do
    if [ "$filename" = "$asset" ]; then
        [ -z "$expected" ] && [ -z "$extra" ] || fail 'Duplicate or malformed native installer checksum.'
        case "$checksum" in
            ''|*[!0-9a-f]*) fail 'Malformed native installer checksum.' ;;
        esac
        [ "${#checksum}" -eq 64 ] || fail 'Malformed native installer checksum.'
        expected=$checksum
    fi
done < "$temporary/SHA256SUMS"
[ -n "$expected" ] || fail 'The selected release does not contain a verified self-contained installer.'

download "$base/$asset" "$temporary/installer" 67108864
if [ "$hash_tool" = sha256sum ]; then
    actual=$(sha256sum < "$temporary/installer")
else
    actual=$(shasum -a 256 < "$temporary/installer")
fi
[ "${actual%% *}" = "$expected" ] || fail 'Native installer checksum mismatch; nothing was installed.'
chmod 700 "$temporary/installer"
"$temporary/installer" install --version "$NGN_BOOTSTRAP_VERSION" "$@"
