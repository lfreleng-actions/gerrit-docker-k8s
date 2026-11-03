#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
#
# Prepare the plugin catalogue Dockerfile.chart copies from the build
# context, from the GHCR mirror the plugin-catalogue workflow publishes
# when it holds the lock, and from the lock's upstream URLs otherwise.
#
# The mirror's tag is the lock's sha256 (scripts/plugin-catalogue.sh
# tag). When the tag exists, the script verifies the artifact with
# cosign against the publishing workflow's exact identity, pulls that
# digest anonymously with oras, and takes each jar from it. When the
# tag does not exist, expected only in a pull request that changes the
# lock, before it merges, it says so and runs docker/fetch-plugins.sh,
# which retries and explains a failed download. Any other registry
# error fails: a mirror that should be there is never silently skipped.
#
# Either way docker/fetch-plugins.sh --check then checks every jar
# against the lock's sha256, and DEST holds plugins/<name>.jar and
# lib/<name>.jar and nothing else. The last line of the log names the
# source: "Plugin catalogue source: mirror" or "... upstream".
#
# Usage
# -----
#   fetch-plugin-catalogue.sh [--lock PATH] [--repository OWNER/REPO] DEST
#
#   --lock PATH               Lock file (default: docker/plugins.lock in
#                             the repository holding this script)
#   --repository OWNER/REPO   Repository whose mirror and workflow
#                             identity to use (default:
#                             lfreleng-actions/gerrit-docker-k8s)
#   DEST                      Output directory; must be absent or empty
#
# Exit codes
# ----------
#   0  Success.
#   1  Verification failed, or a jar is missing or does not match.
#   2  A hard error occurred (bad usage, registry error).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
FETCH="${ROOT}/docker/fetch-plugins.sh"
# Scratch directory, removed on exit
WORK=""
ISSUER="https://token.actions.githubusercontent.com"

err() { printf '✖  %s\n' "$*" >&2; }
info() { printf 'ℹ  %s\n' "$*" >&2; }

notice() {
    if [[ "${GITHUB_ACTIONS:-}" == "true" ]]; then
        printf '::notice::%s\n' "$*"
    else
        info "$*"
    fi
}

usage() {
    cat <<'USAGE'
Usage: fetch-plugin-catalogue.sh [--lock PATH] [--repository OWNER/REPO] DEST

Prepare the plugin catalogue for Dockerfile.chart in DEST, from the
GHCR mirror when it holds docker/plugins.lock, else from upstream.
USAGE
}

# Lay out the lock's entries from a pulled artifact in DEST
from_mirror() {
    local lock="$1" mirror="$2" pulled="$3" dest="$4"
    local line entry
    if ! cmp -s "$lock" "${pulled}/plugins.lock"; then
        err "The plugins.lock in ${mirror} differs from ${lock}"
        return 1
    fi
    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ -z "${line//[[:space:]]/}" || "$line" =~ ^[[:space:]]*# ]] && continue
        read -r entry _ <<<"$line"
        if [[ ! -f "${pulled}/${entry}.jar" ]]; then
            err "${mirror} has no jar for ${entry}"
            return 1
        fi
        cp "${pulled}/${entry}.jar" "${dest}/${entry}.jar"
    done <"$lock"
}

main() {
    local lock="${ROOT}/docker/plugins.lock"
    local repository="lfreleng-actions/gerrit-docker-k8s"
    local dest=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --lock|--repository)
                [[ $# -ge 2 ]] || { err "$1 needs a value"; exit 2; }
                if [[ "$1" == "--lock" ]]; then lock="$2"; else repository="$2"; fi
                shift 2
                ;;
            -h|--help) usage; exit 0 ;;
            -*) err "Unknown argument: $1"; usage >&2; exit 2 ;;
            *)
                [[ -z "$dest" ]] || { err "Expected one DEST"; exit 2; }
                dest="$1"
                shift
                ;;
        esac
    done
    [[ -n "$dest" ]] || { usage >&2; exit 2; }
    if [[ ! "$repository" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
        err "--repository '${repository}' is not OWNER/REPO"
        exit 2
    fi
    if [[ -e "$dest" && -n "$(ls -A "$dest")" ]]; then
        err "${dest} is not empty"
        exit 2
    fi

    local tag mirror identity ref
    tag="$(bash "${SCRIPT_DIR}/plugin-catalogue.sh" --lock "$lock" tag)"
    # GHCR repository names are lowercase
    mirror="ghcr.io/${repository,,}/plugin-catalogue"
    identity="https://github.com/${repository}/.github/workflows/plugin-catalogue.yaml@refs/heads/main"
    ref="${mirror}:${tag}"

    local digest source
    WORK="$(mktemp -d)"
    trap 'rm -rf "${WORK:?}"' EXIT
    mkdir -p "${dest}/plugins" "${dest}/lib"

    if digest="$(oras resolve "$ref" 2>"${WORK}/resolve.err")"; then
        if [[ ! "$digest" =~ ^sha256:[0-9a-f]{64}$ ]]; then
            err "oras resolved ${ref} to '${digest}'"
            exit 2
        fi
        info "Verifying ${mirror}@${digest}"
        if ! cosign verify "${mirror}@${digest}" \
            --certificate-identity "$identity" \
            --certificate-oidc-issuer "$ISSUER" >/dev/null; then
            err "Signature verification failed for ${mirror}@${digest}"
            err "Expected identity: ${identity}"
            exit 1
        fi
        info "Pulling ${mirror}@${digest}"
        oras pull "${mirror}@${digest}" -o "${WORK}/pulled" >&2
        from_mirror "$lock" "${mirror}@${digest}" "${WORK}/pulled" "$dest"
        source="mirror (${mirror}@${digest})"
    elif grep -qi 'not found' "${WORK}/resolve.err"; then
        notice "Plugin catalogue tag ${tag} is not mirrored in ${mirror};" \
            "fetching from the lock's upstream URLs. Expected only while a" \
            "pull request changes docker/plugins.lock."
        sh "$FETCH" "$lock" "$dest"
        source="upstream (${tag} not mirrored)"
    else
        cat "${WORK}/resolve.err" >&2
        err "Cannot tell whether ${ref} exists"
        exit 2
    fi

    sh "$FETCH" --check "$lock" "$dest"
    echo "Plugin catalogue source: ${source}"
}

main "$@"
