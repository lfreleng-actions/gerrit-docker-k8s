#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

# Run "git gc" over every repository under the site's git directory,
# for the chart's gc CronJob. The script's arguments are the git gc
# options; gc.aggressive in etc/gerrit.config adds "--aggressive", and
# etc/jgit.config is read as the system git config, so the [gc],
# [pack] and [core] settings Gerrit's own gc uses apply here too.
# gc.autoDetach is off, so "git gc --auto" finishes, and reports its
# status, before the script moves on.
# A per-repository lock skips a repository another run still holds;
# any other locking error counts as a failed repository.
# A repository that fails, or a directory find cannot read, does not
# stop the others; the script exits non-zero after the last one.

set -eu

site=${GERRIT_SITE:?}
store="$site/git"

if ! command -v flock >/dev/null 2>&1; then
  echo "ERROR: flock is not installed" >&2
  exit 1
fi
if [ ! -d "$store" ]; then
  echo "ERROR: no git directory at $store" >&2
  exit 1
fi

if [ -f "$site/etc/jgit.config" ]; then
  GIT_CONFIG_SYSTEM="$site/etc/jgit.config"
  export GIT_CONFIG_SYSTEM
fi
if [ -f "$site/etc/gerrit.config" ] &&
  [ "$(git config -f "$site/etc/gerrit.config" --type=bool --default=false gc.aggressive)" = true ]; then
  set -- "$@" --aggressive
fi

found=$(mktemp)
list=$(mktemp)
trap 'rm -f "$found" "$list"' EXIT
listed=true
# A "*.git" directory holding HEAD, objects and refs is a repository,
# and find does not descend into it. Any other "*.git" directory is a
# namespace when something found sits below it; otherwise it is kept
# as a broken repository, so git gc reports it.
if ! find "$store" -type d -name '*.git' -exec sh -c '
    if [ -f "$1/HEAD" ] && [ -d "$1/objects" ] && [ -d "$1/refs" ]; then
      printf "r %s\n" "$1"
      exit 0
    fi
    printf "n %s\n" "$1"
    exit 1
  ' sh {} \; -prune > "$found"; then
  echo "ERROR: could not list every repository under $store" >&2
  listed=false
fi
awk '
  { kind[NR] = substr($0, 1, 1); path[NR] = substr($0, 3) }
  END {
    for (i = 1; i <= NR; i++) {
      if (kind[i] == "n") {
        for (j = 1; j <= NR; j++) {
          if (index(path[j], path[i] "/") == 1) break
        }
        if (j <= NR) continue
      }
      print path[i]
    }
  }
' "$found" > "$list"
sort -o "$list" "$list"

total=0
failed=0
while IFS= read -r repo; do
  total=$((total + 1))
  echo "gc $repo"
  rc=0
  (
    # flock -n exits 1 only when another process holds the lock; any
    # other status is a locking error and fails the repository.
    lock=0
    flock -n 9 || lock=$?
    if [ "$lock" -eq 1 ]; then
      echo "skip $repo: locked by another gc"
      exit 0
    fi
    if [ "$lock" -ne 0 ]; then
      echo "ERROR: could not lock $repo: flock exited $lock" >&2
      exit "$lock"
    fi
    git -c gc.autoDetach=false --git-dir="$repo" gc "$@"
  ) 9>"$repo/chart-gc.lock" || rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "ERROR: git gc failed with $rc in $repo" >&2
    failed=$((failed + 1))
  fi
done < "$list"

echo "git gc ran over $total repositories, $failed failed"
[ "$failed" -eq 0 ] && "$listed"
