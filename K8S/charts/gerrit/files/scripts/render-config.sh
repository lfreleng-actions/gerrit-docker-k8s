#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

# Write etc/secure.config from the environment, which the chart fills
# from Secrets, at every start. The file is replaced whole, so a value
# removed from the Secret leaves the site too.

set -eu

site=${GERRIT_SITE:?}
secure="$site/etc/secure.config"
tmp="$secure.new"

umask 077
rm -f "$tmp"
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
