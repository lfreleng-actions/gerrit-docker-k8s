# shellcheck shell=sh
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
#
# Fetch the plugin catalogue docker/plugins.lock pins and check each
# jar against its sha256. scripts/fetch-plugin-catalogue.sh runs it
# when the GHCR mirror lacks the lock's tag; the tests run it against
# a local HTTP server.
#
# Usage: sh fetch-plugins.sh [--check] <lock> <catalogue-dir>
#
# Each lock line is "<dir>/<name> <url> <sha256>"; '#' lines and blank
# lines are skipped. The jar lands at <catalogue-dir>/<dir>/<name>.jar,
# and <catalogue-dir>/<dir> must already exist.
#
# --check downloads nothing: it checks a catalogue already laid out
# that way, as Dockerfile.chart does at build time. Either way every
# jar must match its sha256 and the catalogue must hold no other jar.
#
# curl retries connection failures and transient HTTP errors (408,
# 429, 5xx), but never another 4xx: a 404 cannot succeed on retry.
# On a failed fetch the script names the entry, the URL and the HTTP
# status and exits 1; a digest mismatch also exits 1.

set -eu

check_only=false
if [ "${1:-}" = "--check" ]; then
    check_only=true
    shift
fi
if [ "$#" -ne 2 ]; then
    echo "Usage: sh fetch-plugins.sh [--check] <lock> <catalogue-dir>" >&2
    exit 2
fi
lock=$1
catalogue=$2

# fetch <entry> <url> <jar> <name>: download one entry, or exit 1
# naming it, its URL and the HTTP status
fetch() {
    echo "Fetching $1 from $2"
    rc=0
    status=$(curl -fsSL --retry 5 --retry-delay 3 --retry-connrefused \
        --retry-max-time 120 -w '%{http_code}' -o "$3" "$2") || rc=$?
    if [ "$rc" -ne 0 ]; then
        {
            echo "------------------------------------------------------------"
            echo "Failed to fetch plugin catalogue entry: $1"
            echo "URL: $2"
            echo "HTTP status: ${status:-000} (curl exit $rc)"
            if [ "$status" = 404 ]; then
                echo "GerritForge CI keeps only each job's latest build, so"
                echo "the build this entry pins has been discarded. Re-pin"
                echo "it in docker/plugins.lock with:"
                echo "  bash scripts/check-plugin-lock.sh --refresh $4"
            fi
            echo "------------------------------------------------------------"
        } >&2
        exit 1
    fi
}

entries=$(mktemp)
trap 'rm -f "$entries"' EXIT
grep -Ev '^[[:space:]]*(#|$)' "$lock" > "$entries"

while read -r entry url digest; do
    jar="$catalogue/$entry.jar"
    if [ "$check_only" = true ]; then
        if [ ! -f "$jar" ]; then
            echo "Missing plugin catalogue entry $entry: no $jar" >&2
            exit 1
        fi
    else
        fetch "$entry" "$url" "$jar" "${entry#*/}"
    fi
    if ! echo "$digest  $jar" | sha256sum -c - >/dev/null; then
        echo "sha256 mismatch for $entry ($url)" >&2
        exit 1
    fi
    echo "$jar: OK"
done < "$entries"

found=$(find "$catalogue" -name '*.jar' -type f | wc -l)
expected=$(wc -l < "$entries")
if [ "$found" -ne "$expected" ]; then
    echo "Expected $expected jars in $catalogue, found $found" >&2
    exit 1
fi
