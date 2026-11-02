#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
#
# Pre-commit hook: validate (and on request refresh) docker/plugins.lock,
# the sha256 pins for the plugin catalogue Dockerfile.chart installs.
#
# Lock format
# -----------
#   One artefact per line, three whitespace-separated fields:
#
#     <dir>/<name>  <url>  <sha256>
#
#   <dir> is "plugins" or "lib", the catalogue directory the jar lands
#   in as <name>.jar. <url> is a GerritForge CI artefact under a pinned
#   build number, never lastSuccessfulBuild, whose file name is
#   <name>.jar. Lines starting with '#' and blank lines are ignored.
#
# Modes
# -----
#   (default)          Check the format only; no network access.
#   --verify           Also download every artefact and compare digests.
#   --refresh [NAME..] Move each named entry (all when none is named) to
#                      its CI job's last successful build, then rewrite
#                      the URL and digest in place. GerritForge discards
#                      old builds, so this is how the lock moves on.
#
# Exit codes
# ----------
#   0  The lock is valid (and, with --verify, every digest matches).
#   1  The lock is malformed or a digest does not match.
#   2  A hard error occurred (missing file, bad usage, network failure).

set -euo pipefail

CI_HOST="gerrit-ci.gerritforge.com"
URL_RE="^https://${CI_HOST//./\\.}/job/([A-Za-z0-9._-]+)/([^/]+)/artifact/(.+)$"

if [[ -t 2 ]]; then
    RED='\033[0;31m'
    GREEN='\033[0;32m'
    CYAN='\033[0;36m'
    RESET='\033[0m'
else
    RED='' GREEN='' CYAN='' RESET=''
fi

info() { printf "${CYAN}ℹ${RESET}  %s\n" "$*" >&2; }
ok()   { printf "${GREEN}✔${RESET}  %s\n" "$*" >&2; }
err()  { printf "${RED}✖${RESET}  %s\n" "$*" >&2; }

usage() {
    cat <<'USAGE'
Usage: check-plugin-lock.sh [--lock PATH] [--verify | --refresh [NAME...]]

Validate docker/plugins.lock, the sha256 pins for the image's plugin
catalogue.

Options:
  --lock PATH   Lock file to check (default: docker/plugins.lock in the
                repository holding this script)
  --verify      Download every artefact and compare its sha256
  --refresh     Re-pin the named entries (default: all) to their CI
                job's last successful build and rewrite their digests
  -h, --help    Show this help message
USAGE
}

# Print the sha256 of a URL's content, downloading it to a temp file
fetch_digest() {
    local url="$1" tmp digest
    tmp="$(mktemp)"
    if ! curl -fsSL --retry 3 --retry-delay 3 -o "$tmp" "$url"; then
        rm -f "$tmp"
        err "Failed to download ${url}"
        return 2
    fi
    digest="$(sha256sum "$tmp" | awk '{print $1}')"
    rm -f "$tmp"
    echo "$digest"
}

# Check the lock's format; print each entry as "entry url digest"
# on stdout for the caller, report every problem on stderr
parse_lock() {
    local lock="$1" lineno=0 bad=0 count=0
    local -A seen=()
    local line entry url digest extra dir name build path
    while IFS= read -r line || [[ -n "$line" ]]; do
        lineno=$((lineno + 1))
        [[ -z "${line//[[:space:]]/}" || "$line" =~ ^[[:space:]]*# ]] && continue
        read -r entry url digest extra <<<"$line"
        local where="${lock}:${lineno}"
        if [[ -z "$digest" || -n "${extra:-}" ]]; then
            err "${where}: expected three fields: <dir>/<name> <url> <sha256>"
            bad=1
            continue
        fi
        count=$((count + 1))
        dir="${entry%%/*}"
        name="${entry#*/}"
        if [[ "$entry" != */* || ! "$dir" =~ ^(plugins|lib)$ ]]; then
            err "${where}: '${entry}' must start with plugins/ or lib/"
            bad=1
        fi
        if [[ ! "$name" =~ ^[a-z0-9][a-z0-9._-]*$ ]]; then
            err "${where}: '${name}' is not a valid jar name"
            bad=1
        elif [[ -n "${seen[$name]:-}" ]]; then
            err "${where}: '${name}' is listed more than once"
            bad=1
        fi
        seen[$name]=1
        if [[ ! "$url" =~ $URL_RE ]]; then
            err "${where}: URL must be an artefact on https://${CI_HOST}"
            bad=1
        else
            build="${BASH_REMATCH[2]}"
            path="${BASH_REMATCH[3]}"
            if [[ ! "$build" =~ ^[0-9]+$ ]]; then
                err "${where}: URL needs a pinned build number, not '${build}'"
                bad=1
            fi
            if [[ "${path##*/}" != "${name}.jar" ]]; then
                err "${where}: URL must name ${name}.jar, not '${path##*/}'"
                bad=1
            fi
        fi
        if [[ ! "$digest" =~ ^[0-9a-f]{64}$ ]]; then
            err "${where}: '${digest}' is not a lowercase sha256 digest"
            bad=1
        fi
        echo "$entry $url $digest"
    done <"$lock"
    if [[ "$count" -eq 0 ]]; then
        err "${lock}: no entries"
        bad=1
    fi
    return "$bad"
}

verify_lock() {
    local entries="$1" entry url digest actual bad=0
    while read -r entry url digest; do
        info "Verifying ${entry}"
        actual="$(fetch_digest "$url")" || return 2
        if [[ "$actual" != "$digest" ]]; then
            err "${entry}: sha256 mismatch: lock ${digest}, artefact ${actual}"
            bad=1
        fi
    done <<<"$entries"
    return "$bad"
}

refresh_lock() {
    local lock="$1" entries="$2"
    shift 2
    local entry url digest job build path latest new_url new_digest
    local tmp
    tmp="$(mktemp)"
    cp "$lock" "$tmp"
    while read -r entry url digest; do
        if [[ $# -gt 0 ]] && ! printf '%s\n' "$@" | grep -qxF -e "$entry" -e "${entry#*/}"; then
            continue
        fi
        [[ "$url" =~ $URL_RE ]]
        job="${BASH_REMATCH[1]}"
        build="${BASH_REMATCH[2]}"
        path="${BASH_REMATCH[3]}"
        if ! latest="$(curl -fsSL --retry 3 --retry-delay 3 \
            "https://${CI_HOST}/job/${job}/lastSuccessfulBuild/api/json?tree=number")"; then
            err "${entry}: cannot read the last successful build of ${job}"
            rm -f "$tmp"
            return 2
        fi
        latest="$(sed -n 's/.*"number"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' <<<"$latest")"
        if [[ ! "$latest" =~ ^[0-9]+$ ]]; then
            err "${entry}: ${job} reported build '${latest}'"
            rm -f "$tmp"
            return 2
        fi
        new_url="https://${CI_HOST}/job/${job}/${latest}/artifact/${path}"
        new_digest="$(fetch_digest "$new_url")" || { rm -f "$tmp"; return 2; }
        if [[ "$new_url" == "$url" && "$new_digest" == "$digest" ]]; then
            ok "${entry}: build ${build} is current"
            continue
        fi
        if [[ "$latest" == "$build" ]]; then
            info "${entry}: build ${build} digest corrected"
        else
            info "${entry}: build ${build} -> ${latest}"
        fi
        ENTRY="$entry" NEW="${entry} ${new_url} ${new_digest}" \
            awk '$1 == ENVIRON["ENTRY"] { print ENVIRON["NEW"]; next } { print }' \
            "$tmp" >"${tmp}.new"
        mv "${tmp}.new" "$tmp"
    done <<<"$entries"
    cat "$tmp" >"$lock"
    rm -f "$tmp"
}

main() {
    local lock="" mode="check"
    local -a names=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --lock)
                [[ $# -ge 2 ]] || { err "--lock needs a path"; exit 2; }
                lock="$2"
                shift 2
                ;;
            --verify) mode="verify"; shift ;;
            --refresh) mode="refresh"; shift ;;
            -h|--help) usage; exit 0 ;;
            -*) err "Unknown argument: $1"; usage >&2; exit 2 ;;
            *)
                [[ "$mode" == "refresh" ]] || { err "Unexpected argument: $1"; exit 2; }
                names+=("$1")
                shift
                ;;
        esac
    done
    if [[ -z "$lock" ]]; then
        lock="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/docker/plugins.lock"
    fi
    if [[ ! -f "$lock" ]]; then
        err "Lock file not found: ${lock}"
        exit 2
    fi

    local entries name
    if ! entries="$(parse_lock "$lock")"; then
        exit 1
    fi
    ok "${lock} is well formed"
    for name in "${names[@]}"; do
        if ! awk '{print $1; sub(/^[^\/]*\//, "", $1); print $1}' <<<"$entries" \
            | grep -qxF -e "$name"; then
            err "No lock entry named '${name}'"
            exit 2
        fi
    done

    case "$mode" in
        verify)
            local rc=0
            verify_lock "$entries" || rc=$?
            [[ "$rc" -eq 0 ]] && ok "Every artefact matches its sha256"
            exit "$rc"
            ;;
        refresh)
            refresh_lock "$lock" "$entries" "${names[@]}"
            ok "Refreshed ${lock}; review and commit the diff"
            ;;
    esac
}

main "$@"
