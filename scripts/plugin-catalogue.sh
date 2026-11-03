#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
#
# Build the GHCR mirror of the plugin catalogue docker/plugins.lock
# pins. The plugin-catalogue workflow pushes what this script stages as
# one OCI artifact, ghcr.io/<owner>/<repo>/plugin-catalogue:<tag>.
#
# The tag is the lowercase hex sha256 of the lock file's bytes, so a
# consumer derives it from the lock it holds and needs no recorded
# digest. Any byte change to the lock, comments included, names a new
# tag. Consumers still check each jar against the lock's sha256; the
# mirror is a copy, not a trust anchor.
#
# Commands
# --------
#   tag                Print the mirror tag for the lock.
#   stage DIR          Check the lock's format, download every artefact
#                      it pins into DIR/<dir>/<name>.jar and verify its
#                      sha256, copy the lock to DIR/plugins.lock, and
#                      write DIR/annotations.json (an oras annotation
#                      file) and DIR/files.txt (one "path:media-type"
#                      per line, the oras push arguments). DIR must be
#                      absent or empty.
#
# Options
# -------
#   --lock PATH                Lock file (default: docker/plugins.lock
#                              in the repository holding this script)
#   --annotation KEY=VALUE     stage: add a manifest annotation; repeat
#                              for more than one
#
# Exit codes
# ----------
#   0  Success.
#   1  The lock is malformed or an artefact's sha256 does not match.
#   2  A hard error occurred (missing file, bad usage, network failure).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Annotation key for each layer's lock entry, <dir>/<name>
ENTRY_KEY="org.linuxfoundation.releng.plugin-lock.entry"
URL_KEY="org.linuxfoundation.releng.plugin-lock.url"
LOCK_KEY="org.linuxfoundation.releng.plugin-lock.sha256"
JAR_TYPE="application/java-archive"
LOCK_TYPE="text/plain"

err() { printf '✖  %s\n' "$*" >&2; }
info() { printf 'ℹ  %s\n' "$*" >&2; }

usage() {
    cat <<'USAGE'
Usage: plugin-catalogue.sh [--lock PATH] tag
       plugin-catalogue.sh [--lock PATH] [--annotation KEY=VALUE...] stage DIR

Print the plugin catalogue mirror tag for docker/plugins.lock, or stage
the artefacts it pins for an oras push.
USAGE
}

lock_tag() {
    sha256sum "$1" | awk '{print $1}'
}

stage() {
    local lock="$1" out="$2"
    shift 2
    local -a annotations=("$@")

    if [[ -e "$out" && -n "$(ls -A "$out")" ]]; then
        err "${out} is not empty"
        return 2
    fi
    local rc=0
    bash "${SCRIPT_DIR}/check-plugin-lock.sh" --lock "$lock" || rc=$?
    [[ "$rc" -eq 0 ]] || return "$rc"
    mkdir -p "$out"

    local tag manifest kv
    tag="$(lock_tag "$lock")"
    manifest="$(jq -n --arg k "$LOCK_KEY" --arg v "$tag" '{($k): $v}')"
    for kv in "${annotations[@]}"; do
        if [[ "$kv" != *=* || -z "${kv%%=*}" ]]; then
            err "Annotation '${kv}' is not KEY=VALUE"
            return 2
        fi
        manifest="$(jq --arg k "${kv%%=*}" --arg v "${kv#*=}" \
            '. + {($k): $v}' <<<"$manifest")"
    done
    local doc
    doc="$(jq -n --argjson m "$manifest" '{"$manifest": $m}')"

    local line entry url digest path actual bad=0
    : >"${out}/files.txt"
    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ -z "${line//[[:space:]]/}" || "$line" =~ ^[[:space:]]*# ]] && continue
        read -r entry url digest <<<"$line"
        path="${entry}.jar"
        mkdir -p "${out}/${path%/*}"
        info "Downloading ${entry}"
        if ! curl -fsSL --retry 3 --retry-delay 3 -o "${out}/${path}" "$url"; then
            err "Failed to download ${url}"
            return 2
        fi
        actual="$(sha256sum "${out}/${path}" | awk '{print $1}')"
        if [[ "$actual" != "$digest" ]]; then
            err "${entry}: sha256 mismatch: lock ${digest}, artefact ${actual}"
            bad=1
            continue
        fi
        doc="$(jq --arg p "$path" --arg ek "$ENTRY_KEY" --arg e "$entry" \
            --arg uk "$URL_KEY" --arg u "$url" \
            '. + {($p): {($ek): $e, ($uk): $u}}' <<<"$doc")"
        printf '%s:%s\n' "$path" "$JAR_TYPE" >>"${out}/files.txt"
    done <"$lock"
    [[ "$bad" -eq 0 ]] || return 1

    cp "$lock" "${out}/plugins.lock"
    printf '%s:%s\n' "plugins.lock" "$LOCK_TYPE" >>"${out}/files.txt"
    printf '%s\n' "$doc" >"${out}/annotations.json"
    info "Staged $(wc -l <"${out}/files.txt") files in ${out} for tag ${tag}"
}

main() {
    local lock="" cmd=""
    local -a args=() annotations=()
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --lock)
                [[ $# -ge 2 ]] || { err "--lock needs a path"; exit 2; }
                lock="$2"
                shift 2
                ;;
            --annotation)
                [[ $# -ge 2 ]] || { err "--annotation needs KEY=VALUE"; exit 2; }
                annotations+=("$2")
                shift 2
                ;;
            -h|--help) usage; exit 0 ;;
            -*) err "Unknown argument: $1"; usage >&2; exit 2 ;;
            *)
                if [[ -z "$cmd" ]]; then
                    cmd="$1"
                else
                    args+=("$1")
                fi
                shift
                ;;
        esac
    done
    if [[ -z "$lock" ]]; then
        lock="$(cd "${SCRIPT_DIR}/.." && pwd)/docker/plugins.lock"
    fi
    if [[ ! -f "$lock" ]]; then
        err "Lock file not found: ${lock}"
        exit 2
    fi

    case "$cmd" in
        tag)
            [[ "${#args[@]}" -eq 0 ]] || { err "tag takes no arguments"; exit 2; }
            lock_tag "$lock"
            ;;
        stage)
            [[ "${#args[@]}" -eq 1 ]] || { err "stage needs one DIR"; exit 2; }
            # Called bare: under || or if, errexit would be off inside
            # stage and a failed jq, cp or write would go unnoticed
            stage "$lock" "${args[0]}" "${annotations[@]}"
            ;;
        *)
            err "Expected a command: tag or stage"
            usage >&2
            exit 2
            ;;
    esac
}

main "$@"
