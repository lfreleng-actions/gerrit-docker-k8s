# shellcheck shell=sh
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
#
# Install rclone from a release archive, for site-import's s3 source.
# Dockerfile.chart runs it at build time; the tests run it against a
# local HTTP server.
#
# Usage: sh fetch-rclone.sh <version> <archive-sha256> <dest-dir> [<base-url>]
#
# Downloads rclone-<version>-linux-amd64.zip and the release's
# SHA256SUMS from <base-url>/<version>/ (the GitHub release by
# default). The archive must match <archive-sha256>, the pin in
# Dockerfile.chart, and SHA256SUMS must list it with that digest, in
# any whitespace layout and with LF or CRLF line endings;
# either mismatch exits 1 before anything is installed. The binary
# lands at <dest-dir>/rclone with mode 0755.
#
# curl retries connection failures and transient HTTP errors, as the
# plugin catalogue fetch does. Exit codes: 0 installed, 1 a failed
# download or a mismatch, 2 bad usage.

set -eu

if [ "$#" -lt 3 ] || [ "$#" -gt 4 ]; then
    echo "Usage: sh fetch-rclone.sh <version> <archive-sha256> <dest-dir> [<base-url>]" >&2
    exit 2
fi
version=$1
pinned=$2
dest=$3
base=${4:-https://github.com/rclone/rclone/releases/download}
name="rclone-${version}-linux-amd64"

if ! printf '%s\n' "$pinned" | grep -Eqx '[0-9a-f]{64}'; then
    echo "The rclone archive sha256 must be 64 lower-case hex digits" >&2
    exit 2
fi

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# fetch <url> <file>: download one release file, or exit 1 naming it
fetch() {
    echo "Fetching $1"
    if ! curl -fsSL --retry 5 --retry-delay 3 --retry-connrefused \
        --retry-max-time 120 -o "$2" "$1"; then
        echo "Failed to fetch $1" >&2
        exit 1
    fi
}

fetch "$base/$version/$name.zip" "$work/$name.zip"
fetch "$base/$version/SHA256SUMS" "$work/SHA256SUMS"

actual=$(sha256sum "$work/$name.zip" | cut -d' ' -f1)
if [ "$actual" != "$pinned" ]; then
    echo "$name.zip has sha256 $actual, not the pinned $pinned" >&2
    exit 1
fi
# SHA256SUMS is a clear-signed text of "<sha256>  <file>" lines. The
# pin above is the authority; this second check tolerates CRLF line
# endings, any run of blanks and sha256sum's "*" binary marker, and
# compares the digest and the file name as whole fields.
if ! tr -d '\r' <"$work/SHA256SUMS" | awk -v sum="$pinned" -v file="$name.zip" '
    { f = $2; sub(/^\*/, "", f) }
    NF == 2 && $1 == sum && f == file { found = 1 }
    END { exit !found }'; then
    echo "SHA256SUMS does not list $name.zip with sha256 $pinned" >&2
    exit 1
fi
echo "$name.zip: OK"

python3 -m zipfile -e "$work/$name.zip" "$work/unpacked"
install -m 0755 "$work/unpacked/$name/rclone" "$dest/rclone"
echo "Installed $dest/rclone"
