#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

# Prepare the Gerrit site on the persistent volume. Every start
# replaces etc/gerrit.config, jgit.config, notedb.config,
# log4j.properties, replication.config, its/actions-its-jira.config and
# healthcheck.config with the files the chart rendered, refreshes the
# binaries and enabled plugins from the image, and installs the hooks
# and gerrit-to-platform configuration. A site without
# .chart/initialised also gets "gerrit init" on the rendered
# configuration, then that marker and .chart/backup-lock, the backup
# CronJob's sign that the site is ready. With GERRIT_SITE_PREPARE_ONLY
# set it stops after the binaries, jars and configuration, for
# site-import. The volume is mounted at GERRIT_SITE so the image's
# /var/gerrit stays visible.

set -eu

site=${GERRIT_SITE:?}
image_bin=${GERRIT_IMAGE_BIN:-/var/gerrit/bin}
config="$site/etc/gerrit.config"
rendered=${GERRIT_RENDERED_CONFIG:?}
catalogue=${GERRIT_CATALOGUE:-/var/gerrit-catalogue}
tools_bin=${GERRIT_TOOLS_BIN:-/opt/gerrit-tools/bin}
hook_scripts=${GERRIT_HOOK_SCRIPTS:-/hook-scripts}

# Create the directories the pod mounts by subPath before the kubelet
# does: it would create any missing one owned by root
# shellcheck disable=SC2086
(cd "$site" && mkdir -p bin plugins lib ${GERRIT_SITE_DIRS:-})

cp "$image_bin/gerrit.war" "$image_bin/gerrit.sh" "$site/bin/"

# The site's plugins and libraries are exactly the ones named: clear
# every entry, hidden ones too, since Gerrit loads a plugin named
# .jar, then copy each from the image catalogue. An absent catalogue
# is fine while nothing is named.
find "$site/plugins" "$site/lib" -mindepth 1 -maxdepth 1 -exec rm -rf {} +

copy_jars() {
  kind=$1
  dest=$2
  shift 2
  for name in "$@"; do
    src="$catalogue/$kind/$name.jar"
    if [ ! -f "$src" ]; then
      echo "ERROR: $src is not in the image catalogue" >&2
      exit 1
    fi
    cp "$src" "$dest/"
  done
}

# The lists are space-separated jar names: split them, never glob
set -f
# shellcheck disable=SC2086
copy_jars plugins "$site/plugins" ${GERRIT_PLUGINS:-}
# shellcheck disable=SC2086
copy_jars lib "$site/lib" ${GERRIT_LIBS:-}
set +f

# Print a key from a config file, or nothing when the key is absent.
# Any other failure, such as an unreadable or malformed file, stops
# the script: reading it as absent could discard the site's identity.
get_key() {
  if [ ! -r "$1" ]; then
    echo "ERROR: cannot read $1" >&2
    exit 1
  fi
  rc=0
  git config -f "$1" --get "$2" || rc=$?
  if [ "$rc" -gt 1 ]; then
    echo "ERROR: cannot read $2 from $1" >&2
    exit 1
  fi
}

# gerrit.serverId is site identity: Gerrit refuses changes stamped
# with another ID, so no render may change it. An empty value keeps
# the site's own ID.
want_id=$(get_key "$rendered/gerrit.config" gerrit.serverId)
site_id() {
  if [ -e "$config" ]; then
    get_key "$config" gerrit.serverId
  fi
}
have_id=$(site_id)
if [ -n "$want_id" ] && [ -n "$have_id" ] && [ "$want_id" != "$have_id" ]; then
  echo "ERROR: gerrit.serverId $want_id does not match the site's $have_id" >&2
  exit 1
fi

install_config() {
  cp "$rendered/gerrit.config" "$config.new"
  if [ -z "$want_id" ] && [ -n "$have_id" ]; then
    git config -f "$config.new" gerrit.serverId "$have_id"
  fi
  mv -f "$config.new" "$config"
  for name in jgit.config notedb.config log4j.properties; do
    cp "$rendered/$name" "$site/etc/$name"
  done
  # Rendered only with replication enabled; the chart owns the file,
  # so a disabled render removes the site's copy
  if [ -e "$rendered/replication.config" ]; then
    cp "$rendered/replication.config" "$site/etc/replication.config"
  else
    rm -f "$site/etc/replication.config"
  fi
  # Rendered only with its-jira enabled, and owned by the chart in the
  # same way
  if [ -e "$rendered/actions-its-jira.config" ]; then
    mkdir -p "$site/etc/its"
    cp "$rendered/actions-its-jira.config" "$site/etc/its/actions-its-jira.config"
  else
    rm -f "$site/etc/its/actions-its-jira.config"
  fi
  # Rendered only with healthcheck enabled, and owned by the chart in
  # the same way
  if [ -e "$rendered/healthcheck.config" ]; then
    cp "$rendered/healthcheck.config" "$site/etc/healthcheck.config"
  else
    rm -f "$site/etc/healthcheck.config"
  fi
}

install_config

# site-import runs this far on an imported site before its own
# "gerrit init", so init and reindex load the image's jars and the
# configuration the site will run with
if [ -n "${GERRIT_SITE_PREPARE_ONLY:-}" ]; then
  echo "Prepared the Gerrit site in $site"
  exit 0
fi

# The launcher unpacks into ~/.gerritcodereview, and the gerrit
# user's home is the image's read-only /var/gerrit, so point it at /tmp
gerrit_war() {
  java -Duser.home=/tmp \
    --add-opens java.base/java.net=ALL-UNNAMED \
    --add-opens java.base/java.lang.invoke=ALL-UNNAMED \
    -jar "$site/bin/gerrit.war" "$@"
}

# .chart/initialised records that "gerrit init" succeeded on this
# site: site-init writes it after its own init, site-import after an
# import's init and reindex. Without it, init runs, again if an
# earlier start failed part way: it is idempotent on a partial site,
# whatever repositories that start left. Init runs on the rendered
# configuration, so a set serverId becomes the site's ID; the render
# is then installed again over whatever init added.
initialised="$site/.chart/initialised"
if [ ! -f "$initialised" ]; then
  echo "Initialising the Gerrit site in $site"
  gerrit_war init --batch --no-auto-start -d "$site"
  have_id=$(site_id)
  install_config
  version=$(gerrit_war version | sed -n 's/^gerrit version //p')
  (
    umask 077
    mkdir -p "$site/.chart"
    printf '{"gerritVersion": %s, "initialisedAt": "%s"}\n' \
      "$([ -n "$version" ] && printf '"%s"' "$version" || echo null)" \
      "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$initialised.new"
  )
  mv -f "$initialised.new" "$initialised"
fi

# Next to the marker, so its presence says the site is initialised:
# the backup CronJob serialises its runs with a flock on this file,
# which it opens through its read-only mount of the site, and skips a
# site without it. It is owned by the Gerrit user, which a
# kubelet-created subPath would not be.
lock_dir="$site/.chart/backup-lock"
(umask 077 && mkdir -p "$lock_dir" && touch "$lock_dir/lock")
chmod 0700 "$lock_dir"

# The hooks directory holds exactly the hooks the values name: links
# to the gerrit-to-platform entry points, then hooks.scripts. Hooks
# disabled leave the directory alone, since the plugin is not loaded.
install_hooks() {
  dir="$site/$GERRIT_HOOKS_PATH"
  mkdir -p "$dir"
  find "$dir" -mindepth 1 -maxdepth 1 ! -type d -exec rm -f {} +
  set -f
  # shellcheck disable=SC2086
  for name in ${GERRIT_G2P_HOOKS:-}; do
    target="$tools_bin/$name"
    if [ ! -f "$target" ]; then
      echo "ERROR: $target is not in the image" >&2
      exit 1
    fi
    ln -s "$target" "$dir/$name"
  done
  set +f
  if [ -d "$hook_scripts" ]; then
    for src in "$hook_scripts"/*; do
      [ -f "$src" ] || continue
      cp "$src" "$dir/${src##*/}"
      chmod 0755 "$dir/${src##*/}"
    done
  fi
}

# gerrit-to-platform reads its INI, and the replication.config it
# finds remotes in, from $XDG_CONFIG_HOME/gerrit_to_platform, which
# the Gerrit container points at the site. The INI holds credentials:
# it is written at mode 0600 and removed once g2p is disabled.
install_g2p() {
  g2p_dir="$site/.config/gerrit_to_platform"
  ini="$g2p_dir/gerrit_to_platform.ini"
  if [ -z "${GERRIT_G2P_INI:-}" ]; then
    rm -f "$ini" "$g2p_dir/replication.config"
    return
  fi
  mkdir -p "$g2p_dir"
  chmod 0700 "$g2p_dir"
  rm -f "$ini.new"
  (umask 077 && cp "$GERRIT_G2P_INI" "$ini.new")
  chmod 0600 "$ini.new"
  mv -f "$ini.new" "$ini"
  ln -sfn /var/gerrit/etc/replication.config "$g2p_dir/replication.config"
}

if [ -n "${GERRIT_HOOKS_PATH:-}" ]; then
  install_hooks
fi
install_g2p

echo "Gerrit site in $site is ready; backups may run"
