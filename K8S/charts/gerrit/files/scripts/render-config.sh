#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

# Write etc/secure.config from the environment, which the chart fills
# from Secrets, at every start. The file is replaced whole, so a value
# removed from the Secret leaves the site too. etc/lfs.secure.config is
# written the same way when the chart passes the LFS S3 keys, which it
# does only with plugins.lfs enabled on the s3 backend, and removed
# otherwise.

set -eu

site=${GERRIT_SITE:?}
secure="$site/etc/secure.config"
tmp="$secure.new"

umask 077
# git config writes through <file>.lock and fails while one exists, so a
# lock a killed run left would stop every later start
rm -f "$tmp" "$tmp.lock"
: >"$tmp"
chmod 0600 "$tmp"
git config -f "$tmp" auth.registerEmailPrivateKey \
  "${GERRIT_REGISTER_EMAIL_PRIVATE_KEY:?}"
# Optional keys: an empty or absent Secret key leaves the entry out
if [ -n "${GERRIT_SMTP_PASS:-}" ]; then
  git config -f "$tmp" sendemail.smtpPass "$GERRIT_SMTP_PASS"
fi
if [ -n "${GERRIT_SAML_KEYSTORE_PASSWORD:-}" ]; then
  git config -f "$tmp" saml.keystorePassword "$GERRIT_SAML_KEYSTORE_PASSWORD"
fi
if [ -n "${GERRIT_SAML_PRIVATE_KEY_PASSWORD:-}" ]; then
  git config -f "$tmp" saml.privateKeyPassword \
    "$GERRIT_SAML_PRIVATE_KEY_PASSWORD"
fi
if [ -n "${GERRIT_ITS_JIRA_USERNAME:-}" ]; then
  git config -f "$tmp" its-jira.username "$GERRIT_ITS_JIRA_USERNAME"
fi
if [ -n "${GERRIT_ITS_JIRA_PASSWORD:-}" ]; then
  git config -f "$tmp" its-jira.password "$GERRIT_ITS_JIRA_PASSWORD"
fi
if [ -n "${GERRIT_METRICS_BEARER_TOKEN:-}" ]; then
  git config -f "$tmp" plugin.metrics-reporter-prometheus.prometheusBearerToken \
    "$GERRIT_METRICS_BEARER_TOKEN"
fi
mv -f "$tmp" "$secure"
echo "Wrote $secure"

lfs="$site/etc/lfs.secure.config"
# Staged under .chart/, on the same volume, which exports and imports
# leave out, so a run stopped before the rename leaves no copy of the
# keys an archive could carry
tmp="$site/.chart/lfs.secure.config.new"
rm -f "$lfs.new" "$lfs.new.lock" "$tmp" "$tmp.lock"
if [ -n "${GERRIT_LFS_S3_ACCESS_KEY+set}" ] || [ -n "${GERRIT_LFS_S3_SECRET_KEY+set}" ]; then
  mkdir -p "$site/.chart"
  : >"$tmp"
  chmod 0600 "$tmp"
  if [ -n "${GERRIT_LFS_S3_ACCESS_KEY:-}" ]; then
    git config -f "$tmp" s3.accessKey "$GERRIT_LFS_S3_ACCESS_KEY"
  fi
  if [ -n "${GERRIT_LFS_S3_SECRET_KEY:-}" ]; then
    git config -f "$tmp" s3.secretKey "$GERRIT_LFS_S3_SECRET_KEY"
  fi
  # A link at the path is replaced, not followed: mv into a link to a
  # directory would write the keys inside its target
  rm -f "$lfs"
  mv -f "$tmp" "$lfs"
  echo "Wrote $lfs"
else
  rm -f "$lfs"
fi
