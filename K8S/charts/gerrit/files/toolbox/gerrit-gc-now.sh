#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

# Usage: gerrit-gc-now [--force] [git gc option ...]
#
# Run the chart's git-gc.sh once over every repository of the site at
# $GERRIT_SITE, passing each git gc option on, so with none it runs a
# full gc. Refuses while the Gerrit pod exists, as
# gerrit-require-offline reports, unless given --force.

set -eu

usage() {
  sed -n '5,10s/^# \{0,1\}//p' "$0"
}

force=
case ${1:-} in
  --force) force=--force; shift ;;
  -h | --help) usage; exit 0 ;;
esac

script=${GERRIT_GC_SCRIPT:-/scripts/git-gc.sh}
if [ ! -f "$script" ]; then
  echo "ERROR: no gc script at $script" >&2
  exit 1
fi
gerrit-require-offline ${force:+"$force"} || exit 1

exec sh "$script" "$@"
