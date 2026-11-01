#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

# Usage: gerrit-cache-purge [--force]
#
# Delete Gerrit's persistent cache files, cache/*.h2.db and
# cache/*.mv.db, from the site at $GERRIT_SITE; Gerrit rebuilds them as
# it needs them. Refuses while the Gerrit pod exists, as
# gerrit-require-offline reports, unless given --force.

set -eu

usage() {
  sed -n '5,10s/^# \{0,1\}//p' "$0"
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
cache="$site/cache"
if [ ! -d "$cache" ]; then
  echo "ERROR: no cache directory at $cache" >&2
  exit 1
fi
gerrit-require-offline ${force:+"$force"} || exit 1

removed=0
for f in "$cache"/*.h2.db "$cache"/*.mv.db; do
  [ -f "$f" ] || continue
  rm -f -- "$f"
  echo "removed $f"
  removed=$((removed + 1))
done
echo "removed $removed cache files from $cache"
