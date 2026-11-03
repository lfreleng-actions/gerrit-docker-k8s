# shellcheck shell=sh
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
#
# Fetch the plugin catalogue docker/plugins.lock pins and check each
# jar against its sha256. scripts/fetch-plugin-catalogue.sh runs it
# when the lock has no GHCR mirror; the tests run it against a local
# HTTP server.
#
# Usage: sh fetch-plugins.sh [--check] <lock> <catalogue-dir>
#
# Each lock line is "<dir>/<name> <url> <sha256>"; '#' lines and blank
# lines are skipped. The jar lands at <catalogue-dir>/<dir>/<name>.jar,
# and <catalogue-dir>/<dir> must already exist.
#
# --check downloads nothing: it checks the jars already in
# <catalogue-dir> against the lock, as Dockerfile.chart does with the
# catalogue the build context holds. A missing jar, a digest mismatch
# or a jar the lock does not pin exits 1.
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

# Download one entry's jar, or explain why it could not
fetch() {
    entry=$1 url=$2 jar=$3
    echo "Fetching $entry"
    rc=0
    status=$(curl -fsSL --retry 5 --retry-delay 3 --retry-connrefused \
        --retry-max-time 120 -w '%{http_code}' -o "$jar" "$url") || rc=$?
    if [ "$rc" -ne 0 ]; then
        {
            echo "------------------------------------------------------------"
            echo "Failed to fetch plugin catalogue entry: $entry"
            echo "URL: $url"
            echo "HTTP status: ${status:-000} (curl exit $rc)"
            if [ "$status" = 404 ]; then
                echo "GerritForge CI keeps only each job's latest build, so"
                echo "the build this entry pins has been discarded. Re-pin"
                echo "it in docker/plugins.lock with:"
                echo "  bash scripts/check-plugin-lock.sh --refresh ${entry#*/}"
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
    if [ "$check_only" = false ]; then
        fetch "$entry" "$url" "$jar"
    elif [ ! -f "$jar" ]; then
        echo "Missing jar for $entry: $jar" >&2
        exit 1
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
