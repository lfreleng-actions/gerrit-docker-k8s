#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
#
# Prepare the plugin catalogue docker/plugins.lock pins, for
# Dockerfile.chart to copy from the build context. Run it before
# `docker build -f Dockerfile.chart`:
#
#   bash scripts/fetch-plugin-catalogue.sh build/plugin-catalogue
#
# Sources
# -------
#   mirror    ghcr.io/<owner>/<repo>/plugin-catalogue:<sha256 of lock>,
#             which the plugin-catalogue workflow publishes. The script
#             resolves the tag to a digest, checks the digest's cosign
#             signature against that workflow's identity on main, then
#             pulls it, anonymously, with oras. A failed check fails.
#   upstream  The lock's own GerritForge URLs, through
#             docker/fetch-plugins.sh and its retries. Used only when
#             the mirror has no tag for the lock, as in a pull request
#             that changes the lock, or with --upstream.
#
# Either way every jar is checked against the lock's sha256, and the
# result is DEST/plugins/<name>.jar and DEST/lib/<name>.jar, files
# mode 0644 and directories 0755, with nothing else in DEST. The last
# line of the log names the source used:
#
#   Plugin catalogue source: mirror <ref>@<digest>
#   Plugin catalogue source: upstream
#
# Options
# -------
#   --lock PATH          Lock file (default: docker/plugins.lock in the
#                        repository holding this script)
#   --repository O/R     Repository whose mirror to use (default:
#                        $GITHUB_REPOSITORY, else
#                        lfreleng-actions/gerrit-docker-k8s)
#   --upstream           Skip the mirror; no oras or cosign needed
#
# Exit codes
# ----------
#   0  DEST holds the catalogue.
#   1  The lock is malformed, the mirror's signature or contents do not
#      check out, or a jar does not match the lock.
#   2  A hard error occurred (bad usage, DEST not empty, a missing
#      tool, a registry error other than a missing tag).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DEFAULT_REPOSITORY="lfreleng-actions/gerrit-docker-k8s"
WORKFLOW=".github/workflows/plugin-catalogue.yaml"
ISSUER="https://token.actions.githubusercontent.com"

err() { printf '✖  %s\n' "$*" >&2; }
info() { printf 'ℹ  %s\n' "$*" >&2; }

usage() {
    cat <<'USAGE'
Usage: fetch-plugin-catalogue.sh [--lock PATH] [--repository OWNER/REPO]
                                 [--upstream] DEST

Lay out the plugin catalogue docker/plugins.lock pins in DEST, from
the signed GHCR mirror, or from GerritForge when the mirror has no
tag for the lock.
USAGE
}

# True when oras reported ref itself as not found, the line ending in
# "<ref>: not found" as in "Error: failed to resolve digest: <ref>: not
# found". Any other failure, even one that says "not found" about a
# credential helper or another reference, is not a missing tag.
tag_missing() {
    local ref="$1" file="$2" line
    while IFS= read -r line || [[ -n "$line" ]]; do
        line="${line%$'\r'}"
        if [[ "$line" == "${ref}: not found" || "$line" == *" ${ref}: not found" ]]; then
            return 0
        fi
    done <"$file"
    return 1
}

# Check the signature on repo@digest, pull it into out, and check the
# lock it carries is the one we hold
from_mirror() {
    local repo="$1" digest="$2" identity="$3" out="$4" lock="$5"
    info "Checking the signature of ${repo}@${digest}"
    if ! cosign verify "${repo}@${digest}" \
        --certificate-identity "${identity}" \
        --certificate-oidc-issuer "${ISSUER}" >/dev/null; then
        err "${repo}@${digest} carries no valid signature from ${identity}"
        return 1
    fi
    info "Pulling ${repo}@${digest}"
    if ! oras pull "${repo}@${digest}" -o "$out"; then
        err "Failed to pull ${repo}@${digest}"
        return 2
    fi
    if ! cmp -s "$lock" "${out}/plugins.lock"; then
        err "The mirror's plugins.lock does not match ${lock}"
        return 1
    fi
    rm -f "${out}/plugins.lock"
}

main() {
    local lock="${ROOT}/docker/plugins.lock" upstream=false dest=""
    local repository="${GITHUB_REPOSITORY:-${DEFAULT_REPOSITORY}}"
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --lock)
                [[ $# -ge 2 ]] || { err "--lock needs a path"; exit 2; }
                lock="$2"
                shift 2
                ;;
            --repository)
                [[ $# -ge 2 ]] || { err "--repository needs OWNER/REPO"; exit 2; }
                repository="$2"
                shift 2
                ;;
            --upstream) upstream=true; shift ;;
            -h|--help) usage; exit 0 ;;
            -*) err "Unknown argument: $1"; usage >&2; exit 2 ;;
            *)
                [[ -z "$dest" ]] || { err "Expected one DEST"; exit 2; }
                dest="$1"
                shift
                ;;
        esac
    done
    [[ -n "$dest" ]] || { err "Expected a DEST"; usage >&2; exit 2; }
    [[ "$dest" == / ]] || dest="${dest%/}"
    if [[ ! "$repository" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
        err "Repository '${repository}' is not OWNER/REPO"
        exit 2
    fi
    if [[ ! -f "$lock" ]]; then
        err "Lock file not found: ${lock}"
        exit 2
    fi
    if [[ -e "$dest" && ! -d "$dest" ]] || [[ -d "$dest" && -n "$(ls -A "$dest")" ]]; then
        err "${dest} is not an empty directory"
        exit 2
    fi
    local rc=0
    bash "${SCRIPT_DIR}/check-plugin-lock.sh" --lock "$lock" || rc=$?
    [[ "$rc" -eq 0 ]] || exit "$rc"

    local parent staging
    parent="$(dirname "$dest")"
    mkdir -p "$parent"
    staging="$(mktemp -d "${parent}/.plugin-catalogue.XXXXXX")"
    # shellcheck disable=SC2064  # expand now: staging is local
    trap "rm -rf '${staging}'" EXIT

    local tag repo ref digest source="upstream"
    tag="$(sha256sum "$lock" | awk '{print $1}')"
    repo="ghcr.io/${repository,,}/plugin-catalogue"
    ref="${repo}:${tag}"
    if [[ "$upstream" == false ]]; then
        local tool
        for tool in oras cosign; do
            if ! command -v "$tool" >/dev/null; then
                err "${tool} is not installed; install it, or pass --upstream"
                exit 2
            fi
        done
        if digest="$(oras resolve "$ref" 2>"${staging}/resolve.err")"; then
            rm -f "${staging}/resolve.err"
            if [[ ! "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
                err "oras resolved ${ref} to '${digest}'"
                exit 2
            fi
            local identity
            identity="${GITHUB_SERVER_URL:-https://github.com}/${repository}/${WORKFLOW}@refs/heads/main"
            rc=0
            from_mirror "$repo" "$digest" "$identity" "$staging" "$lock" || rc=$?
            [[ "$rc" -eq 0 ]] || exit "$rc"
            source="mirror ${repo}@${digest}"
        elif tag_missing "$ref" "${staging}/resolve.err"; then
            rm -f "${staging}/resolve.err"
            info "${ref} has no mirror (expected only for a lock not yet on main); fetching from the lock's upstream URLs"
        else
            cat "${staging}/resolve.err" >&2
            err "Cannot tell whether ${ref} exists"
            exit 2
        fi
    fi
    mkdir -p "${staging}/plugins" "${staging}/lib"
    if [[ "$source" == upstream ]]; then
        sh "${ROOT}/docker/fetch-plugins.sh" "$lock" "$staging" || exit 1
    fi

    local extra
    extra="$(find "$staging" -mindepth 1 \( \
        \( -type d ! -path "${staging}/plugins" ! -path "${staging}/lib" \) -o \
        \( ! -type d ! \( -type f -name '*.jar' \) \) \) -print)"
    if [[ -n "$extra" ]]; then
        err "Unexpected entries in the catalogue: ${extra//$'\n'/ }"
        exit 1
    fi
    sh "${ROOT}/docker/fetch-plugins.sh" --check "$lock" "$staging" || exit 1
    find "$staging" -type d -exec chmod 0755 {} +
    find "$staging" -type f -exec chmod 0644 {} +
    if [[ -d "$dest" ]]; then
        rmdir "$dest"
    fi
    mv "$staging" "$dest"
    trap - EXIT
    echo "Plugin catalogue source: ${source}"
}

main "$@"
