#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

# Usage: gerrit-index-prune [--force]
#
# Remove the index/<name>_NNNN directories that past upgrades left in
# the site at $GERRIT_SITE. index/gerrit_index.config marks the version
# of each index Gerrit uses as ready, and those are kept, as is any
# index without a ready version. Refuses while the Gerrit pod exists,
# as gerrit-require-offline reports, unless given --force.

set -eu

usage() {
  sed -n '5,11s/^# \{0,1\}//p' "$0"
}

force=
case $# in
  0) ;;
  1)
    case $1 in
      --force) force=--force ;;
      -h | --help) usage; exit 0 ;;
      *) usage >&2; exit 2 ;;
    esac
    ;;
  *) usage >&2; exit 2 ;;
esac

site=${GERRIT_SITE:?}
index="$site/index"
config="$index/gerrit_index.config"
if [ ! -f "$config" ]; then
  echo "ERROR: no $config, so the versions in use are unknown" >&2
  exit 1
fi

# Read the config on its own, not in a pipeline, so that a failed read,
# such as a value that is not a boolean, stops the prune rather than
# leaving a partial list. Status 1 means no ready key at all.
status=0
entries=$(git config -f "$config" --type=bool --get-regexp '^index\..*\.ready$') ||
  status=$?
if [ "$status" -ne 0 ] && [ "$status" -ne 1 ]; then
  echo "ERROR: cannot read $config (git config exit $status)" >&2
  exit 1
fi
# One "<name>_NNNN" line per index version the config marks ready
ready=$(printf '%s\n' "$entries" |
  awk '$2 == "true" { sub(/^index\./, "", $1); sub(/\.ready$/, "", $1); print $1 }')
if [ -z "$ready" ]; then
  echo "ERROR: $config marks no index version ready" >&2
  exit 1
fi
gerrit-require-offline ${force:+"$force"} || exit 1

removed=0
for dir in "$index"/*_[0-9][0-9][0-9][0-9]; do
  [ -d "$dir" ] || continue
  version=${dir##*/}
  name=${version%_*}
  # Keep the versions in use, and every version of an index none of
  # whose versions is marked ready
  if printf '%s\n' "$ready" | grep -qxF -- "$version" ||
    ! printf '%s\n' "$ready" | grep -q "^${name}_[0-9][0-9][0-9][0-9]\$"; then
    echo "kept $dir"
    continue
  fi
  rm -rf -- "$dir"
  echo "removed $dir"
  removed=$((removed + 1))
done
echo "removed $removed stale index versions from $index"
