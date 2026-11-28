# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Report on an imported site's configuration for site-import.

site-init replaces the imported configuration at its first start, so
the migration report reads it first: each [auth] and [saml] key the
chart's render sets differently, gerrit.config keys the image's
release removed, and the H2 file an imported etc/events-log.config
kept its store in, with the plugins.events-log.storeFile that keeps
its history. It also names the paths an export's ``--exclude`` left
out, which the imported site does not hold, and where the chart takes
the lfs plugin's S3 keys from, which no archive brings to the volume.
``site-import.py`` is the entry point.
"""

from __future__ import annotations

import json
import posixpath
import re
import string
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from site_import_archive import removed_keys_report
from site_import_common import SiteImportError, config_get, config_names, run

# The keys of the gerrit.config sections that decide who signs in, and
# as whom: unqualified [auth] and [saml], as git names them, lower case
AUTH_KEYS = r"^(auth|saml)\.[^.]+$"
GIT_CONFIG = ["git", "-c", "safe.directory=*", "config"]
# The chart mounts its own keystore, and check_keystore compares it
AUTH_UNCOMPARED = ("saml.keystorepath",)
# A file path cannot be compared with a URL, or with a path on another
# host, so this key is compared only between two URLs
METADATA_PATH = "saml.metadatapath"
METADATA_URL = re.compile(r"https?://", re.IGNORECASE)
# Key names whose values the report never prints
SECRET_SUFFIXES = ("password", "secret", "privatekey")
# A value's runs of text between whitespace; a URL reference runs to
# the end of its run: a URL may hold an apostrophe, and a stray quote or
# angle bracket must not end it before a credential
TOKEN = re.compile(r"\S+")
SCHEME_CHARS = frozenset(string.ascii_letters + string.digits + "+.-")
SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:(?=//)")
WHITESPACE = re.compile(r"\s")
# A URL's path is shown in full, so it must not hold another URL whose
# user-info would show with it: a nested authority, user-info, a scheme
# or any of them percent-encoded
NESTED = re.compile(r"//|[@:%]")
HIDDEN = "(not shown)"

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


def imported_config_report(
    site: Path, manifest: dict[str, Any], rendered: Path | None = None
) -> list[str]:
    """Report on the imported site's configuration before site-init replaces it.

    With ``rendered``, the directory holding the chart's render, it
    names the [auth] and [saml] keys the render sets differently.
    """
    exported = manifest.get("site")
    path = exported.get("path") if isinstance(exported, dict) else None
    source_site = str(path or "")
    roots = [source_site]
    if manifest.get("mode") == "backup" and source_site in CHART_BACKUP_MOUNTS:
        roots.append(CHART_BACKUP_MOUNTS[source_site])
    config = site / "etc" / "gerrit.config"
    return [
        *(auth_report(config, rendered / "gerrit.config") if rendered else []),
        *removed_keys_report(config_names(config)),
        *events_log_report(site, *roots),
        *excluded_report(manifest),
        *lfs_report(site),
    ]


def auth_settings(path: Path) -> dict[str, list[str | None]]:
    """The unqualified [auth] and [saml] keys of a git config file.

    Each name, which git reports in lower case, maps to the list of its
    values in file order; a key written without '=' has the value None.
    Bytes that are not UTF-8 are replaced, so a value cannot fail the
    import that only reports on it.
    """
    if not path.is_file():
        raise SiteImportError(f"cannot read {path}")
    result = run(
        [*GIT_CONFIG, "-f", str(path), "-z", "--get-regexp", AUTH_KEYS],
        capture_output=True,
        errors="replace",
    )
    if result.returncode > 1:
        raise SiteImportError(f"cannot parse {path}")
    found: dict[str, list[str | None]] = {}
    for entry in filter(None, result.stdout.split("\0")):
        name, newline, value = entry.partition("\n")
        found.setdefault(name, []).append(value if newline else None)
    return found


def url_start(token: str) -> int:
    """Where a URL reference starts in a run of text, or -1.

    An absolute or scheme-relative reference starts at its scheme or its
    '//'; a relative one with a query or a fragment starts the run. Its
    user-info, query and fragment may carry a credential. Each step is a
    single pass, so a long value costs linear time.
    """
    slashes = token.find("//")
    delimiters = [i for i in (token.find("?"), token.find("#")) if i >= 0]
    if delimiters and (slashes < 0 or min(delimiters) < slashes):
        return 0
    if slashes < 1 or token[slashes - 1] != ":":
        return slashes
    colon = first = slashes - 1
    while first and token[first - 1] in SCHEME_CHARS:
        first -= 1
    letters = (i for i in range(first, colon) if token[i] in string.ascii_letters)
    return next(letters, slashes)


def url_spans(value: str) -> list[tuple[int, int]]:
    """The start and end of each URL reference in a value."""
    spans = []
    for token in TOKEN.finditer(value):
        start = url_start(token.group(0))
        if start >= 0:
            spans.append((token.start() + start, token.end()))
    return spans


def hide_url(url: str) -> str:
    """A URL reference with its user-info, query and fragment hidden.

    The query is hidden whole: a bare token may hold '=', as Base64
    padding does, so no part of a field is safe to show. Call it only
    on a URL unsafe_url clears, which urlsplit can read.
    """
    parts = urlsplit(url)
    _, at, host = parts.netloc.rpartition("@")
    netloc = f"{HIDDEN}@{host}" if at else host
    query = HIDDEN if parts.query else ""
    fragment = HIDDEN if parts.fragment else ""
    return urlunsplit((parts.scheme, netloc, parts.path, query, fragment))


def unsafe_url(url: str) -> bool:
    """Whether hiding a URL reference's parts may still show a credential.

    Its path is shown in full, so it must not hold another URL. An '@'
    in its query or fragment may close user-info whose password held the
    '?' or '#', which ended the authority early and would show the user
    and password as the host. A URL urlsplit cannot read may hide an '@'
    the report needs to see, so it is unsafe too.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return True
    return bool(NESTED.search(parts.path)) or "@" in parts.query + parts.fragment


def outside_credential(text: str) -> bool:
    """Whether a value as the report prints it may hold a credential.

    ``text`` is the value with each URL in it hidden and its scheme left
    out. A ':' before an '@' may be a user and password, whose password
    may hold whitespace, or a '//' that starts a URL the matcher splits
    from the user and password before it; a '%' may encode a URL with
    its user-info.
    """
    return "%" in text or -1 < text.find(":") < text.rfind("@")


def without_scheme(url: str) -> str:
    """A hidden URL reference without the scheme and ':' it starts with."""
    scheme = SCHEME.match(url)
    return url[scheme.end() :] if scheme else url


def shown_value(value: str | None) -> str:
    """One value as the report prints it.

    A URL ends at whitespace, so text after a space may still be its
    query, as in a quoted ``?label=Sign in&token=...``: a value holding
    both a URL and any whitespace is hidden whole. So is a value holding
    a URL that unsafe_url names, or one that outside_credential names
    once its URLs are hidden.
    """
    if value is None:
        return "(no value)"
    spans = url_spans(value)
    found = [value[a:b] for a, b in spans]
    starts, ends = [a for a, _ in spans], [b for _, b in spans]
    bounds = zip([0, *ends], [*starts, len(value)], strict=True)
    gaps = [value[a:b] for a, b in bounds]
    if (found and WHITESPACE.search(value)) or any(unsafe_url(u) for u in found):
        return "(value not shown)"
    hidden = [hide_url(url) for url in found]
    pieces = list(zip(hidden, gaps[1:], strict=True))
    checked = gaps[0] + "".join(without_scheme(h) + g for h, g in pieces)
    if outside_credential(checked):
        return "(value not shown)"
    text = gaps[0] + "".join(h + g for h, g in pieces)
    return json.dumps(text, ensure_ascii=False)


def shown(values: list[str | None]) -> str:
    quoted = [shown_value(v) for v in values]
    return quoted[0] if len(quoted) == 1 else f"[{', '.join(quoted)}]"


def auth_line(name: str, archive: list[str | None], render: list[str | None]) -> str:
    """One report line for a key the archive and the render set differently.

    An empty list stands for a key the file does not set.
    """
    theirs, ours = "the archive", "the chart's render"
    if name.endswith(SECRET_SUFFIXES):
        if not render:
            where = f"in {theirs} and not in {ours}"
        elif not archive:
            where = f"in {ours} and not in {theirs}"
        else:
            where = f"in {theirs} and in {ours}, to different values"
        return (
            f"gerrit.config sets {name} {where}; the value is not shown, and the "
            "chart takes it from a Secret"
        )
    if not render:
        return f"gerrit.config sets {name} to {shown(archive)} in {theirs} and not in {ours}"
    if not archive:
        return f"gerrit.config sets {name} to {shown(render)} in {ours} and not in {theirs}"
    have, want = shown(archive), shown(render)
    if have == want:
        return (
            f"gerrit.config sets {name} to {have} in {theirs} and in "
            f"{ours}, with values that differ in a part of a URL not shown"
        )
    return f"gerrit.config sets {name} to {have} in {theirs} and {want} in {ours}"


def urls(values: list[str | None]) -> bool:
    return bool(values) and all(v is not None and METADATA_URL.match(v) for v in values)


def auth_report(imported: Path, rendered: Path) -> list[str]:
    """Name each [auth] and [saml] key the render changes from the archive's.

    The chart's render replaces the archive's gerrit.config, so a key
    left out of the values, or given another value, changes who can sign
    in and as whom. Runtime overrides apply later and are not compared.
    """
    archive, render = auth_settings(imported), auth_settings(rendered)
    lines = []
    for name in sorted(set(archive) | set(render)):
        have, want = archive.get(name, []), render.get(name, [])
        if have == want or name in AUTH_UNCOMPARED:
            continue
        if name == METADATA_PATH and not (urls(have) and urls(want)):
            continue
        lines.append(auth_line(name, have, want))
    return lines


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
