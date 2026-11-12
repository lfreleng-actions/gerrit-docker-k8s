# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Report on an imported site's configuration for site-import.

site-init replaces the imported configuration at its first start, so
the migration report reads it first: gerrit.config keys the image's
release removed, and the H2 file an imported etc/events-log.config
kept its store in, with the plugins.events-log.storeFile that keeps
its history. It also names the paths an export's ``--exclude`` left
out, which the imported site does not hold, and where the chart takes
the lfs plugin's S3 keys from, which no archive brings to the volume.
``site-import.py`` is the entry point.
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path, PurePosixPath
from typing import Any

from site_import_archive import removed_keys_report
from site_import_common import SiteImportError, config_get, config_names

# What plugins.events-log.storeFile accepts
STORE_FILE = re.compile(r"[A-Za-z0-9._-]+")
# An H2 database's file: MVStore's, or the older PageStore's
H2_SUFFIXES = (".mv.db", ".h2.db")
# The backup CronJob exports the site from its own mount of the volume
# (SITE_BACKUP_SITE in backup-cronjob.yaml), which the manifest records,
# while the site's files name the Gerrit container's path for it
CHART_BACKUP_MOUNTS = {"/mnt/site": "/var/gerrit"}
# The paths the export tool always leaves out, EXCLUDED in
# scripts/gerrit-site-export.py; a manifest's excluded array lists an
# operator's --exclude paths after them
BUILTIN_EXCLUDED = (
    "cache",
    "index",
    "logs",
    "tmp",
    "bin",
    ".gerritcodereview/tmp",
    ".chart",
    ".ssh",
    ".config/gerrit_to_platform",
    "etc/lfs.secure.config",
)


def imported_config_report(site: Path, manifest: dict[str, Any]) -> list[str]:
    """Report on the imported site's configuration before site-init replaces it."""
    exported = manifest.get("site")
    path = exported.get("path") if isinstance(exported, dict) else None
    source_site = str(path or "")
    roots = [source_site]
    if manifest.get("mode") == "backup" and source_site in CHART_BACKUP_MOUNTS:
        roots.append(CHART_BACKUP_MOUNTS[source_site])
    return [
        *removed_keys_report(config_names(site / "etc" / "gerrit.config")),
        *events_log_report(site, *roots),
        *excluded_report(manifest),
        *lfs_report(site),
    ]


def excluded_report(manifest: dict[str, Any]) -> list[str]:
    """Name the paths the export left out beyond the tool's own."""
    excluded = manifest.get("excluded")
    if not isinstance(excluded, list):
        return []
    extra = [str(p) for p in excluded if p not in BUILTIN_EXCLUDED]
    if not extra:
        return []
    return [f"the export left out {', '.join(extra)} (--exclude); the site lacks them"]


def lfs_report(site: Path) -> list[str]:
    """Say where the chart takes the lfs plugin's S3 keys from.

    No archive brings etc/lfs.secure.config to the volume: the export
    tool leaves it out and unpack() skips it in an older archive. The
    plugin reads the keys only for an S3 backend, which the imported
    etc/lfs.config names as storage.backend or in an [s3] section, a
    named one included; one that cannot be read may name it too.
    """
    path = site / "etc" / "lfs.config"
    if not path.is_file():
        return []
    try:
        s3 = config_get(path, "storage.backend").lower() == "s3" or any(
            n.startswith("s3.") for n in config_names(path)
        )
    except SiteImportError:
        s3 = True
    if not s3:
        return []
    return [
        "the imported etc/lfs.config uses the lfs plugin's S3 backend, whose keys "
        "in etc/lfs.secure.config no archive brings in; with plugins.lfs.enabled "
        "on the s3 backend the chart writes them from the accessKey and "
        "secretKey of plugins.lfs.existingSecret"
    ]


def in_site_data(store: str, source_site: str) -> bool:
    """Whether an H2 path is data/<name> under the exported site's path.

    Without the manifest's site path, or with a relative store path,
    the report cannot tell which database the URL names. Nor can it
    with a '..' component: the source site's links decide where that
    leads, and normalising it away could name another directory.
    """
    if not source_site.startswith("/") or not store.startswith("/"):
        return False
    if ".." in store.split("/"):
        return False
    root = PurePosixPath(posixpath.normpath(source_site))
    return PurePosixPath(posixpath.normpath(store)).parent == root / "data"


def events_log_report(site: Path, *source_sites: str) -> list[str]:
    """Name the H2 file an imported events-log.config keeps its store in.

    site-init replaces the file with one whose store is
    data/<plugins.events-log.storeFile>. The report says that setting
    keeps the history only when storeUrl names the exported site's own
    data/<name>, at one of ``source_sites`` from the manifest, and the
    archive holds that database there. It gives the file name alone,
    never the source site's path or a remote store's URL.
    """
    path = site / "etc" / "events-log.config"
    prefix = "the imported etc/events-log.config"
    try:
        url = config_get(path, "events-log.storeUrl") if path.is_file() else ""
    except SiteImportError:
        return [f"{prefix} cannot be read; site-init replaces it at every start"]
    if not url:
        return []
    if not url.startswith("jdbc:h2:"):
        return [f"{prefix} names a store other than a local H2 file; the chart's is H2"]
    location = url.removeprefix("jdbc:h2:").removeprefix("file:").split(";")[0]
    location = location.replace("\\", "/")
    store = PurePosixPath(location)
    if re.match(r"(mem|tcp|ssl|zip):", location):
        return [f"{prefix} names a store other than a local H2 file; the chart's is H2"]
    if not STORE_FILE.fullmatch(store.name) or store.name in (".", ".."):
        return [f"{prefix} names an H2 file plugins.events-log.storeFile cannot name"]
    found = f"{prefix} keeps the events-log store in the H2 file {store.name}"
    moved = (
        "the chart's store starts empty unless that database is moved into "
        "data/ on the site, with plugins.events-log.storeFile naming it"
    )
    if not any(in_site_data(location, root) for root in source_sites):
        elsewhere = "which storeUrl does not place in the exported site's data/"
        return [f"{found}, {elsewhere}; {moved}"]
    data = site / "data"
    if not any((data / f"{store.name}{end}").is_file() for end in H2_SUFFIXES):
        return [f"{found}, which the imported site does not hold in data/; {moved}"]
    if store.name == "db":
        keep = "the default plugins.events-log.storeFile, db, keeps its history"
    else:
        # Quoted, so a name such as null, false or 0 stays a YAML string;
        # STORE_FILE admits no quote to escape
        keep = f"set plugins.events-log.storeFile: '{store.name}' to keep its history"
    return [
        f"{found}; {keep}, as site-init replaces that file with the chart's at "
        "every start"
    ]
