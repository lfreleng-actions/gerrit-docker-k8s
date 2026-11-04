# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Adopt an existing Gerrit site before Gerrit starts.

The ``site-import`` init container runs this script, standard library
only, on every start of the Gerrit pod. It reads the state file
``<site>/.chart/site-state.json`` and decides, from
``siteImport.mode``, ``siteImport.forceToken`` and the source, whether
to do nothing, record a fresh site, or import an archive written by
``scripts/gerrit-site-export.py final --pack``
(``K8S/docs/DESIGN.md`` sections 1.7 and 3).

An import writes ``in-progress`` first, fetches and checks the archive
in the scratch volume, replaces the site, installs the chart's
plugins, libraries and configuration through ``site-init.sh``, runs
Gerrit's ``init`` and a full ``reindex``, checks the result against
the manifest and records a migration report. Only then does it write
``complete``. Any failure writes ``failed`` and exits non-zero, so
Gerrit never starts on a partial site. Archive contents and secret
values are never logged.

``site_import_common.py`` holds the settings and helpers,
``site_import_archive.py`` the fetch, checks and migration report, and
``site_import_unpack.py`` the unpacking and its link checks; the chart
ships all four in its scripts ConfigMap.

Configuration comes from the environment the chart sets:

- ``GERRIT_SITE``: the site volume (``/mnt/site``)
- ``GERRIT_RENDERED_CONFIG``: the rendered configuration (``/config``)
- ``GERRIT_IMAGE_BIN``: the image's ``bin`` holding ``gerrit.war``
- ``SITE_IMPORT_MODE``: ``auto``, ``force`` or ``skip``
- ``SITE_IMPORT_FORCE_TOKEN``: required with ``force``
- ``SITE_IMPORT_SOURCE_TYPE``: ``none``, ``pvc`` or ``https``
- ``SITE_IMPORT_SOURCE_URL``: a directory, or an HTTPS base URL
- ``SITE_IMPORT_URLS``: a directory whose files, named after the
  archive files, each hold a full URL that replaces the derived one
- ``SITE_IMPORT_EXPECTED_MANIFEST_SHA256``: optional manifest pin
- ``SITE_IMPORT_REINDEX``, ``SITE_IMPORT_VERIFY``: ``true`` or ``false``
- ``SITE_IMPORT_SCRATCH``: the scratch volume
- ``SITE_IMPORT_SAML_KEYSTORE``: the keystore from the SAML Secret
- ``SITE_IMPORT_SECURE_KEYS``: the ``secure.config`` names the chart
  writes, space separated
- ``SITE_IMPORT_JAVA_HEAP``: heap options for ``init`` and ``reindex``,
  space separated, the same the Gerrit container gets
- ``SITE_IMPORT_CPU_LIMIT``: the container's CPU limit, which sets
  ``reindex --threads``; Gerrit's default when empty
- ``SITE_INIT_SCRIPT``: ``site-init.sh``, run in prepare-only mode
- ``GERRIT_TOOLS_BIN``: the image's gerrit-to-platform entry points,
  ``/opt/gerrit-tools/bin`` by default, as for ``site-init.sh``
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from site_import_archive import (
    ARCHIVES,
    check_archive,
    check_host_keys,
    check_identity,
    check_repositories,
    fetch,
    load_manifest,
    project_config_report,
    removed_keys_report,
    repositories,
    secure_config_report,
)
from site_import_common import (
    PROG,
    Settings,
    SiteImportError,
    config_get,
    config_names,
    empty_dir,
    gerrit_version,
    run,
    say,
    warn,
)
from site_import_unpack import skip_regenerated, unpack

NOTHING, FRESH, ADOPT, IMPORT = "nothing", "fresh", "adopt", "import"

STATE_FILE = Path(".chart") / "site-state.json"
# Kept on the volume across an import: the state, and ext4's own directory
KEEP = (".chart", "lost+found")
JAVA_OPTS = [
    "-Duser.home=/tmp",
    "--add-opens",
    "java.base/java.net=ALL-UNNAMED",
    "--add-opens",
    "java.base/java.lang.invoke=ALL-UNNAMED",
]


def state_path(site: Path) -> Path:
    return site / STATE_FILE


def read_state(site: Path) -> dict[str, Any] | None:
    """Return the state, or None without a file; an unreadable one fails."""
    path = state_path(site)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise SiteImportError(f"cannot read {path}: {exc.strerror}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise SiteImportError(f"{path} is not JSON; repair or remove it") from exc
    if not isinstance(data, dict) or data.get("state") not in (
        "in-progress",
        "complete",
        "failed",
    ):
        raise SiteImportError(f"{path} holds no valid state; repair or remove it")
    return data


def write_state(site: Path, data: dict[str, Any]) -> None:
    """Write the state atomically: a temporary file renamed over the old."""
    path = state_path(site)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    record = {
        "schema": 1,
        **data,
        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(temp, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(record, indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def occupied(site: Path) -> bool:
    """Whether the volume holds anything but the state and lost+found."""
    return any(entry.name not in KEEP for entry in site.iterdir())


def decide(
    state: dict[str, Any] | None,
    mode: str,
    token: str,
    source: str,
    holds_site: bool,
    chart_site: bool = False,
) -> str:
    """Return NOTHING, FRESH, ADOPT or IMPORT; raise on a refusal.

    chart_site says the volume holds a site an earlier chart version
    created, before site-import existed; auto with no source adopts it.
    A refusal writes no state: nothing on the volume has changed.
    """
    if mode == "skip":
        return NOTHING
    recorded = state.get("forceToken") if state else None
    if mode == "force":
        if not token:
            raise SiteImportError("siteImport.mode force needs a siteImport.forceToken")
        if source == "none":
            raise SiteImportError(
                "siteImport.mode force needs a siteImport.source to import"
            )
        if token != recorded:
            return IMPORT
    # auto, or force with the token already honoured
    if state is None:
        if holds_site and mode == "auto" and source == "none" and chart_site:
            return ADOPT
        if holds_site:
            raise SiteImportError(
                "the volume holds a site no state file accounts for; repair the volume, "
                "set siteImport.mode skip to keep it, or force an import over it"
            )
        return FRESH if source == "none" else IMPORT
    if state["state"] == "complete":
        return NOTHING
    if source == "none":
        raise SiteImportError(
            f"an earlier import left state {state['state']}; set siteImport.source "
            "to retry it, or repair the volume"
        )
    return IMPORT


def git_store(settings: Settings) -> Path:
    """The repository store the chart's gerrit.basePath names, on the volume."""
    base = config_get(settings.rendered / "gerrit.config", "gerrit.basePath") or "git"
    if os.path.isabs(base):
        if not base.startswith("/var/gerrit/"):
            raise SiteImportError(f"gerrit.basePath {base} is not on the site volume")
        base = base[len("/var/gerrit/") :]
    return settings.site / base


def chart_created(settings: Settings) -> bool:
    """Whether the volume holds a site the chart created and initialised.

    Chart 0.6 sites carry no state file: they have etc/gerrit.config,
    and the All-Projects repository it names in the chart's git store.
    """
    config = settings.site / "etc" / "gerrit.config"
    if not config.is_file() or config.is_symlink():
        return False
    all_projects = config_get(config, "gerrit.allProjects") or "All-Projects"
    return (git_store(settings) / f"{all_projects}.git").is_dir()


@dataclass
class Imported:
    """What the import learns from the archive before it replaces the site."""

    manifest: dict[str, Any]
    digest: str
    server_id: str
    secure_names: list[str]
    config_names: list[str]


def stage(settings: Settings) -> tuple[Imported, Path]:
    """Fetch, check and unpack the archive in the scratch volume."""
    empty_dir(settings.scratch)
    manifest, digest = load_manifest(fetch(settings, "manifest.json"), settings.pin)
    say(
        f"manifest.json sha256 {digest}, {manifest['mode']} export of Gerrit "
        f"{manifest.get('gerrit_version') or 'unknown'}"
    )
    unpacked = settings.scratch / "unpacked"
    unpacked.mkdir()
    absolute: list[tuple[str, str]] = []
    for name in ARCHIVES:
        archive = fetch(settings, name)
        check_archive(archive, manifest["archives"][name])
        say(f"{name} matches its manifest sha256")
        unpack(archive, unpacked, absolute)
        if archive.is_relative_to(settings.scratch):
            archive.unlink()
    site, store = unpacked / "site", unpacked / "git"
    config = site / "etc" / "gerrit.config"
    if not config.is_file() or not store.is_dir():
        raise SiteImportError("the archives hold no site/etc/gerrit.config or git/")
    skip_regenerated(absolute, config, settings.tools_bin)
    archive_id = check_identity(
        site, settings.rendered / "gerrit.config", manifest, settings.saml_keystore
    )
    if settings.verify:
        check_repositories(store, manifest)
        check_host_keys(site / "etc", manifest, exact=True)
    imported = Imported(
        manifest=manifest,
        digest=digest,
        server_id=archive_id,
        secure_names=config_names(site / "etc" / "secure.config"),
        config_names=config_names(config),
    )
    return imported, unpacked


def place(settings: Settings, unpacked: Path) -> Path:
    """Replace the site with the unpacked one; return the repository store."""
    say(f"replacing the site in {settings.site}")
    empty_dir(settings.site, keep=KEEP)
    for entry in (unpacked / "site").iterdir():
        shutil.move(str(entry), str(settings.site / entry.name))
    store = git_store(settings)
    if store.exists():
        if store.is_dir() and not store.is_symlink() and not any(store.iterdir()):
            store.rmdir()
        else:
            raise SiteImportError(f"the site archive already holds {store.name}")
    store.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(unpacked / "git"), str(store))
    return store


def prepare(settings: Settings) -> None:
    """Install the chart's binaries, plugins, libraries and configuration."""
    env = {
        **os.environ,
        "GERRIT_SITE": str(settings.site),
        "GERRIT_SITE_PREPARE_ONLY": "1",
    }
    if run(["sh", str(settings.site_init)], env=env).returncode:
        raise SiteImportError("site-init.sh failed to prepare the site")


def gerrit(settings: Settings, *args: str) -> None:
    war = settings.site / "bin" / "gerrit.war"
    command = [
        settings.java,
        *JAVA_OPTS,
        *settings.java_heap,
        "-jar",
        str(war),
        *args,
        "-d",
        str(settings.site),
    ]
    say(f"running gerrit.war {args[0]}")
    if status := run(command).returncode:
        raise SiteImportError(f"gerrit.war {args[0]} failed with exit status {status}")


def run_import(settings: Settings) -> dict[str, Any]:
    """Import the archive; return what the complete state records."""
    imported, unpacked = stage(settings)
    store = place(settings, unpacked)
    empty_dir(settings.scratch)
    prepare(settings)
    gerrit(settings, "init", "--batch", "--no-auto-start")
    prepare(settings)
    if settings.reindex:
        threads = settings.reindex_threads
        gerrit(settings, "reindex", *(["--threads", str(threads)] if threads else []))
    config = settings.site / "etc" / "gerrit.config"
    if settings.verify:
        missing = set(imported.manifest["git"]["repositories"]) - set(
            repositories(store)
        )
        if missing:
            raise SiteImportError(
                f"repositories gone after init: {', '.join(sorted(missing)[:10])}"
            )
        check_host_keys(settings.site / "etc", imported.manifest, exact=False)
        site_id = config_get(config, "gerrit.serverId")
        if imported.server_id and site_id != imported.server_id:
            raise SiteImportError(
                f"gerrit.serverId is {site_id} after init, not the archive's {imported.server_id}"
            )
        say("verified SSH host keys and gerrit.serverId")
    all_projects = config_get(config, "gerrit.allProjects") or "All-Projects"
    warnings = [
        *project_config_report(store, all_projects),
        *removed_keys_report(imported.config_names),
        *secure_config_report(imported.secure_names, settings.secure_keys),
    ]
    for line in warnings:
        warn(line)
    return {"manifestSha256": imported.digest, "warnings": warnings}


def main(
    env: dict[str, str], importer: Callable[[Settings], dict[str, Any]] = run_import
) -> int:
    try:
        settings = Settings.from_env(env)
        state = read_state(settings.site)
        holds_site = occupied(settings.site)
        chart_site = (
            state is None
            and holds_site
            and settings.mode == "auto"
            and settings.source == "none"
            and chart_created(settings)
        )
        action = decide(
            state,
            settings.mode,
            settings.token,
            settings.source,
            holds_site,
            chart_site,
        )
    except SiteImportError as exc:
        print(f"{PROG}: error: {exc}", file=sys.stderr, flush=True)
        return 1
    current = state["state"] if state else "none"
    say(f"mode {settings.mode}, source {settings.source}, state {current}: {action}")
    if action == NOTHING:
        return 0
    recorded = state.get("forceToken") if state else None
    honoured = settings.token if settings.mode == "force" else recorded
    record = {
        "source": settings.source_label,
        "gerritVersion": gerrit_version(settings.image_bin / "gerrit.war"),
        "forceToken": honoured or None,
    }
    if action == FRESH:
        write_state(
            settings.site, {"state": "complete", **record, "manifestSha256": None}
        )
        say("recorded a fresh site; site-init initialises it")
        return 0
    if action == ADOPT:
        write_state(
            settings.site,
            {"state": "complete", **record, "manifestSha256": None, "adopted": True},
        )
        say("adopted the site an earlier chart version created, with no state file")
        return 0
    write_state(
        settings.site, {"state": "in-progress", **record, "manifestSha256": None}
    )
    try:
        result = importer(settings)
    except (SiteImportError, OSError, subprocess.SubprocessError) as exc:
        reason = str(exc)
        write_state(
            settings.site,
            {"state": "failed", **record, "manifestSha256": None, "reason": reason},
        )
        print(f"{PROG}: error: {reason}", file=sys.stderr, flush=True)
        return 1
    write_state(settings.site, {"state": "complete", **record, **result})
    say("import complete")
    return 0


if __name__ == "__main__":
    sys.exit(main(dict(os.environ)))
