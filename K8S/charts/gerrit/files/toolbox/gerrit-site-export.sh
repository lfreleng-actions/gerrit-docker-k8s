#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

# Usage: gerrit-site-export <seed|final|backup> --out <dir> [option ...]
#
# Run the site export tool the image carries; see its --help. The tool's
# own check that Gerrit has stopped reads /proc, which shows only this
# pod's processes, so a final export first runs gerrit-require-offline
# and refuses while the Gerrit pod exists. --i-stopped-writes skips both
# checks. A seed or backup export runs while Gerrit serves.

set -eu

tool=${GERRIT_SITE_EXPORT:-/opt/gerrit-site-export/gerrit-site-export.py}

# Find the mode as the tool does: skip the value after an option that
# takes one, unless it is given as --option=value
final=
stopped=
expect_value=
for arg in "$@"; do
  if [ -n "$expect_value" ]; then
    expect_value=
    case $arg in
      --*) ;;
      *) continue ;;
    esac
  fi
  case $arg in
    -h | --help) exec python3 "$tool" "$@" ;;
    final) final=1 ;;
    --i-stopped-writes) stopped=1 ;;
    --site | --git | --out | --encrypt | --upload) expect_value=1 ;;
  esac
done

if [ -n "$final" ] && [ -z "$stopped" ]; then
  if ! gerrit-require-offline; then
    echo "ERROR: a final export needs Gerrit stopped; scale it to zero," \
      "or stop writes another way and pass --i-stopped-writes" >&2
    exit 1
  fi
fi
exec python3 "$tool" "$@"
