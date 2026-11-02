#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

# Delete the rotated logs in the site's logs/ directory that were last
# written more than LOG_RETENTION_DAYS days ago, for the chart's log
# cleanup CronJob. Gerrit's daily rotation renames each log with the
# date, as in httpd_log.2026-11-01.gz; a log Gerrit still writes has
# no date in its name and is never deleted, however old.

set -eu

site=${GERRIT_SITE:?}
days=${LOG_RETENTION_DAYS:?}
logs="$site/logs"

case $days in
  '' | *[!0-9]* | 0*)
    echo "ERROR: LOG_RETENTION_DAYS must be a positive whole number, not '$days'" >&2
    exit 1
    ;;
esac
if [ ! -d "$logs" ]; then
  echo "No log directory at $logs; nothing to delete"
  exit 0
fi

date_glob='[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
find "$logs" -maxdepth 1 -type f -name "*.$date_glob*" \
  -mmin +"$((days * 1440))" -print -delete
echo "Deleted the rotated logs older than $days days from $logs"
