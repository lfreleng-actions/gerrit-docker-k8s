# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Fetch, check and unpack an export for site-import, and report on it.

The export is the one ``scripts/gerrit-site-export.py final --pack``
writes: ``manifest.json``, ``git.tar.zst`` and ``site.tar.zst``.
``site-import.py`` is the entry point; see its docstring.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any

from site_import_common import (
    Settings,
    SiteImportError,
    config_get,
    git,
    normalise_name,
    run,
    say,
    sha256_of,
)
from site_import_s3 import fetch_s3

ARCHIVES = ("git.tar.zst", "site.tar.zst")
ENCRYPTED = (".age", ".gpg")
IMPORT_MODES = ("final", "backup")
# The oldest release whose site the image's init upgrades in one step
OLDEST_SOURCE = (3, 7, 0)
RELEASE = re.compile(r"v?(\d+)\.(\d+)(?:\.(\d+))?")
CURL_RETRY = [
    "--retry",
    "5",
    "--retry-delay",
    "3",
    "--retry-connrefused",
    "--retry-max-time",
    "120",
]
# The first request and every redirect: never fetch an export without TLS
CURL_HTTPS_ONLY = ["--proto", "=https", "--proto-redir", "=https"]
FOR_EACH_REF = ["for-each-ref", "--format=%(refname) %(objectname) %(*objectname)"]
# gerrit.config keys a release in the import's range removed, lower-case
REMOVED_KEYS = {"auth.cookiesecure": "3.13"}
# The repositories holding Gerrit's own data, and their default names
SYSTEM_REPOSITORIES = {
    "gerrit.allProjects": "All-Projects",
    "gerrit.allUsers": "All-Users",
}
# The keystore the chart mounts from the SAML Secret, relative to the site
DEFAULT_KEYSTORE = "etc/samlKeystore.jks"


def fetch(settings: Settings, name: str) -> Path:
    """Return a local path to one export file, fetching it when remote."""
    override = settings.urls / name if settings.urls else None
    if settings.source == "pvc":
        path = Path(settings.url) / name
        if not path.is_file():
            raise SiteImportError(f"{path} does not exist")
        return path
    if settings.source == "s3":
        return fetch_s3(settings, name, os.environ)
    if override and override.is_file():
        url, shown = (
            override.read_text(encoding="utf-8").strip(),
            "the URL from the Secret",
        )
    else:
        url = f"{settings.url.rstrip('/')}/{name}"
        shown = url
    if not url.startswith("https://"):
        raise SiteImportError(f"{name}: only https:// URLs are accepted")
    dest = settings.scratch / "download" / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    say(f"downloading {name} from {shown}")
    # The URL goes through curl's stdin, so pre-signed signatures stay
    # out of the process list
    quoted = url.replace("\\", "\\\\").replace('"', '\\"')
    result = run(
        [
            "curl",
            "--fail",
            "--silent",
            "--show-error",
            "--location",
            *CURL_HTTPS_ONLY,
            *CURL_RETRY,
            "--config",
            "-",
            "--output",
            str(dest),
        ],
        input=f'url = "{quoted}"\n',
    )
    if result.returncode:
        raise SiteImportError(
            f"downloading {name} failed with curl exit status {result.returncode}"
        )
    return dest


def load_manifest(path: Path, pin: str) -> tuple[dict[str, Any], str]:
    """Parse and check manifest.json; return it with its SHA-256."""
    digest = sha256_of(path)
    if pin and digest != pin:
        raise SiteImportError(
            f"manifest.json has sha256 {digest}, not the expected {pin}"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise SiteImportError("manifest.json is not JSON") from exc
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise SiteImportError("manifest.json is not a schema 1 manifest")
    if data.get("mode") not in IMPORT_MODES:
        raise SiteImportError(
            f"manifest.json is a {data.get('mode')!r} export; import needs a final or backup one"
        )
    archives = data.get("archives")
    if not isinstance(archives, dict):
        raise SiteImportError("manifest.json has no archives")
    if encrypted := [n for n in archives if n.endswith(ENCRYPTED)]:
        raise SiteImportError(
            f"encrypted archives are not supported yet ({', '.join(sorted(encrypted))}); "
            "decrypt them and write a manifest for the plaintext archives first"
        )
    if sorted(archives) != sorted(ARCHIVES):
        raise SiteImportError(
            f"manifest.json must list exactly {' and '.join(ARCHIVES)}; export with --pack"
        )
    git_part = data.get("git")
    repos = git_part.get("repositories") if isinstance(git_part, dict) else None
    if not isinstance(repos, dict) or not isinstance(data.get("ssh_host_keys"), dict):
        raise SiteImportError("manifest.json lacks git.repositories or ssh_host_keys")
    return data, digest


def check_source_version(manifest: dict[str, Any], allow_unknown: bool) -> None:
    """Refuse an export of a release older than site import supports."""
    version = str(manifest.get("gerrit_version") or "")
    found = RELEASE.match(version)
    oldest = ".".join(map(str, OLDEST_SOURCE))
    if not found:
        if allow_unknown:
            return
        raise SiteImportError(
            f"manifest.json names no Gerrit version site import recognises ({version!r}); "
            f"if the source ran Gerrit {oldest} or newer, set "
            "siteImport.allowUnknownSourceVersion to import it anyway"
        )
    release = tuple(int(part or 0) for part in found.groups())
    if release < OLDEST_SOURCE:
        raise SiteImportError(
            f"the export is of Gerrit {version}, older than {oldest}, the oldest "
            f"release site import supports; upgrade the source site to {oldest} "
            "or newer and export it again"
        )


def check_archive(path: Path, expected: str) -> None:
    digest = sha256_of(path)
    if digest != expected:
        raise SiteImportError(
            f"{path.name} has sha256 {digest}, but manifest.json records {expected}"
        )


def repositories(root: Path) -> dict[str, tuple[int, str | None]]:
    """Map each repository under root to its ref count and refs/meta/config."""
    found: dict[str, tuple[int, str | None]] = {}
    for top, dirs, _files in os.walk(root):
        for name in [d for d in dirs if d.endswith(".git")]:
            dirs.remove(name)
            rel = Path(top, name).relative_to(root).as_posix()
            result = git(f"--git-dir={Path(top, name)}", *FOR_EACH_REF)
            if result.returncode:
                raise SiteImportError(f"cannot read the refs of {rel}")
            refs = [line.split(" ") for line in result.stdout.splitlines()]
            meta = [r[2] or r[1] for r in refs if r[0] == "refs/meta/config"]
            found[rel] = (len(refs), next(iter(meta), None))
    return found


def check_repositories(root: Path, manifest: dict[str, Any]) -> None:
    """Check the repositories, ref counts and refs/meta/config heads."""
    expected = manifest["git"]["repositories"]
    found = repositories(root)
    problems = []
    if missing := sorted(set(expected) - set(found)):
        problems.append(f"missing repositories: {', '.join(missing[:10])}")
    if extra := sorted(set(found) - set(expected)):
        problems.append(f"repositories not in the manifest: {', '.join(extra[:10])}")
    for name in sorted(set(found) & set(expected)):
        refs, meta = found[name]
        if refs != expected[name].get("refs"):
            problems.append(
                f"{name} has {refs} refs, the manifest {expected[name].get('refs')}"
            )
        if meta != expected[name].get("meta_config"):
            problems.append(
                f"{name} refs/meta/config is {meta}, the manifest {expected[name].get('meta_config')}"
            )
    if problems:
        raise SiteImportError("verification failed: " + "; ".join(problems[:20]))
    say(f"verified {len(found)} repositories and their refs against the manifest")


def host_key_fingerprints(etc: Path) -> dict[str, str]:
    keys = {}
    for key in sorted(etc.glob("ssh_host_*")):
        if key.suffix == ".pub" or key.is_symlink() or not key.is_file():
            continue
        result = run(
            ["ssh-keygen", "-l", "-E", "sha256", "-f", str(key)], capture_output=True
        )
        fields = result.stdout.split()
        if result.returncode or len(fields) < 2 or not fields[1].startswith("SHA256:"):
            raise SiteImportError(f"cannot fingerprint SSH host key {key.name}")
        keys[key.name] = fields[1]
    return keys


def check_host_keys(etc: Path, manifest: dict[str, Any], exact: bool) -> None:
    """Check host key fingerprints; with exact, no other key may exist."""
    expected = manifest["ssh_host_keys"]
    found = host_key_fingerprints(etc)
    if exact:
        wrong = found != expected
    else:
        wrong = any(found.get(name) != print_ for name, print_ in expected.items())
    if wrong:
        raise SiteImportError(
            f"SSH host key fingerprints {found} do not match the manifest's {expected}"
        )


def check_system_repositories(imported: Path, rendered: Path) -> None:
    """The render must name the archive's All-Projects and All-Users.

    site-init installs the rendered gerrit.config over the archive's, and
    init would create empty repositories under any other names, leaving
    the imported permissions and accounts unused.
    """
    problems = []
    for key, default in SYSTEM_REPOSITORIES.items():
        have = config_get(imported, key) or default
        want = config_get(rendered, key) or default
        if have != want:
            problems.append(f"the archive's {key} is {have}, the chart's {want}")
    if problems:
        raise SiteImportError(
            "; ".join(problems) + "; set the archive's names in gerrit.extraConfig"
        )


def imported_keystore(site: Path, source_site: str) -> Path | None:
    """The SAML keystore the archive's gerrit.config selects, if it has one.

    saml.keystorePath is relative to the site, or absolute on the source
    host, where it must lie in the exported site. Without the key, or
    with the chart's own path, a missing file means a site without SAML.
    Any other path must name a file in the archive, or the import could
    not tell whether the Secret's keystore keeps the site's identity.
    """
    configured = config_get(site / "etc" / "gerrit.config", "saml.keystorePath")
    shown = configured or DEFAULT_KEYSTORE
    name = PurePosixPath(shown)
    if name.is_absolute():
        roots = [r for r in (source_site, "/var/gerrit") if r]
        root = next((r for r in roots if name.is_relative_to(r)), None)
        if root is None:
            raise SiteImportError(
                f"the archive's saml.keystorePath {configured} lies outside its "
                "site; the import cannot compare it with the SAML Secret's keystore"
            )
        name = name.relative_to(root)
    path = site / name
    if not path.resolve().is_relative_to(site.resolve()):
        raise SiteImportError(
            f"the archive's SAML keystore {shown} leads outside its site"
        )
    if path.is_file():
        return path
    if name != PurePosixPath(DEFAULT_KEYSTORE):
        raise SiteImportError(
            f"the archive's saml.keystorePath {configured} names a keystore the "
            "archive does not carry"
        )
    return None


def check_keystore(imported: Path | None, mounted: Path | None) -> None:
    """With SAML, the archive's keystore must be the Secret's."""
    if mounted is None or imported is None or not imported.is_file():
        return
    if not mounted.is_file():
        raise SiteImportError(f"the SAML keystore {mounted} from the Secret is missing")
    have, want = sha256_of(imported), sha256_of(mounted)
    if have != want:
        raise SiteImportError(
            f"the archive's SAML keystore (sha256 {have}) differs from the "
            f"SAML Secret's samlKeystore.jks (sha256 {want}); load the source's "
            "keystore into the Secret"
        )


def check_identity(
    site: Path, rendered: Path, manifest: dict[str, Any], saml: Path | None
) -> str:
    """Check the unpacked site keeps its identity under the chart's render.

    The server ID, the system repositories' names and, with SAML, the
    keystore must survive the chart's configuration. Return the
    archive's server ID.
    """
    config = site / "etc" / "gerrit.config"
    archive_id = config_get(config, "gerrit.serverId")
    want_id = config_get(rendered, "gerrit.serverId")
    if want_id and archive_id and want_id != archive_id:
        raise SiteImportError(
            f"gerrit.serverId {want_id} does not match the archive's {archive_id}"
        )
    check_system_repositories(config, rendered)
    if saml:
        exported = manifest.get("site")
        source_site = exported.get("path") if isinstance(exported, dict) else None
        check_keystore(imported_keystore(site, str(source_site or "")), saml)
    return archive_id


def secure_config_report(names: list[str], chart_keys: list[str]) -> list[str]:
    """Names in the imported secure.config the chart will not write."""
    chart = {normalise_name(k) for k in chart_keys}
    warnings = []
    if dropped := [n for n in names if normalise_name(n) not in chart]:
        warnings.append(
            "secure.config entries the chart does not write, dropped at start: "
            + ", ".join(dropped)
        )
    if "auth.registeremailprivatekey" in {normalise_name(n) for n in names}:
        warnings.append(
            "auth.registerEmailPrivateKey now comes from the chart's Secret; unless it "
            "holds the source's key, outstanding email verification links stop working"
        )
    return warnings


def removed_keys_report(names: list[str]) -> list[str]:
    return [
        f"gerrit.config sets {name}, which Gerrit {release} removed"
        for name in names
        if (release := REMOVED_KEYS.get(normalise_name(name)))
    ]


def project_config_report(store: Path, all_projects: str) -> list[str]:
    """Report behaviour changes the jump to the image's release brings."""
    warnings = []
    repo = f"--git-dir={store / (all_projects + '.git')}"
    blob = ["config", "--blob", "refs/meta/config:project.config"]
    merges = git(repo, *blob, "--get", "receive.rejectImplicitMerges")
    if merges.returncode or not merges.stdout.strip():
        warnings.append(
            f"{all_projects} does not set receive.rejectImplicitMerges; Gerrit 3.13 "
            "rejects implicit merges by default"
        )
    html = git(repo, *blob, "--name-only", "--get-regexp", r"^commentlink\..*\.html$")
    if html.returncode == 0 and html.stdout.strip():
        warnings.append(
            f"{all_projects} has commentlink html entries, removed in Gerrit 3.8: "
            + ", ".join(html.stdout.split())
        )
    prolog = []
    for name in sorted(repositories(store)):
        result = git(
            f"--git-dir={store / name}", "cat-file", "-e", "refs/meta/config:rules.pl"
        )
        if result.returncode == 0:
            prolog.append(name)
    if prolog:
        warnings.append(
            f"{len(prolog)} projects carry rules.pl, and Gerrit 3.12 disables Prolog "
            f"rules by default: {', '.join(prolog[:20])}"
        )
    return warnings
