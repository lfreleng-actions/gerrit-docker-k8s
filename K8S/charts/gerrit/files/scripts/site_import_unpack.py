# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Unpack an export's archives for site-import, refusing unsafe links.

``site-import.py`` is the entry point; see its docstring.
"""

from __future__ import annotations

import os
import subprocess
import tarfile
from pathlib import Path

from site_import_common import SiteImportError, config_get, say

# Never taken from an archive: the state belongs to this run, the
# import rebuilds the index and caches, and the chart's image supplies
# the binaries, plugins and libraries, as ones built for the source
# release can stop the new one starting
SKIPPED = (
    "site/.chart",
    "site/index",
    "site/cache",
    "site/lost+found",
    "site/bin",
    "site/plugins",
    "site/lib",
)
# The site's path inside the Gerrit pod, which absolute paths in a
# chart site's gerrit.config and links start with
POD_SITE = "/var/gerrit"
# The link site-init makes for gerrit-to-platform to find the remotes
G2P_REPLICATION_LINK = (
    "site/.config/gerrit_to_platform/replication.config",
    f"{POD_SITE}/etc/replication.config",
)


def skipped(name: str) -> bool:
    return any(name == s or name.startswith(s + "/") for s in SKIPPED)


def stays_in(dest: Path, top: str, path: str) -> bool:
    """Whether path, through the links unpacked so far, stays in top.

    top is ``site`` or ``git``; the path must also miss SKIPPED. A link
    resolving anywhere else would point outside the site once it is
    placed, or at ``.chart/``, which the import must never write.
    """
    rel = os.path.relpath(os.path.realpath(path), os.path.realpath(dest))
    parts = Path(rel).parts
    return bool(parts) and parts[0] == top and ".." not in parts and not skipped(rel)


def member_problem(member: tarfile.TarInfo, name: str, dest: Path) -> str:
    """Why a member may not be unpacked, or "" when it may."""
    top = Path(name).parts[0]
    parent = os.path.realpath(dest / os.path.dirname(name))
    if not stays_in(dest, top, os.path.join(parent, os.path.basename(name))):
        return f"{name} lies outside {top}/ or in a skipped path"
    if member.issym():
        if os.path.isabs(member.linkname):
            return f"{name} is a symlink to an absolute path"
        if not stays_in(dest, top, os.path.join(parent, member.linkname)):
            return f"{name} is a symlink outside {top}/ or into a skipped path"
    if member.islnk():
        target = os.path.normpath(member.linkname)
        if os.path.isabs(target) or not stays_in(dest, top, str(dest / target)):
            return f"{name} is a hard link outside {top}/ or into a skipped path"
    return ""


def link_problems(dest: Path) -> list[str]:
    """Symlinks that, with every member in place, leave their tree.

    A link checked while unpacking can change meaning once a later
    member adds a link on its path, so every link is checked again.
    """
    found = []
    for top, dirs, files in os.walk(dest):
        for name in (*dirs, *files):
            path = os.path.join(top, name)
            if not os.path.islink(path):
                continue
            rel = os.path.relpath(path, dest)
            if not stays_in(dest, Path(rel).parts[0], path):
                found.append(rel)
    return sorted(found)


def unpack(
    archive: Path, dest: Path, absolute: list[tuple[str, str]] | None = None
) -> None:
    """Unpack a zstd tar under dest, skipping SKIPPED and unsafe members.

    A symlink under site/ to an absolute path is refused, unless
    absolute is a list: then it is left out and its name and target
    appended, for skip_regenerated() to judge once gerrit.config is in
    place.
    """
    dest.mkdir(parents=True, exist_ok=True)
    zstd = subprocess.Popen(
        ["zstd", "-dcq", str(archive)], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    failure = ""
    finished = False
    try:
        with tarfile.open(fileobj=zstd.stdout, mode="r|") as tar:
            for member in tar:
                name = os.path.normpath(member.name)
                parts = Path(name).parts
                if not parts or parts[0] not in ("site", "git") or ".." in parts:
                    failure = f"{archive.name} holds a member outside site/ and git/"
                    break
                if skipped(name):
                    continue
                if not (
                    member.isfile()
                    or member.isdir()
                    or member.issym()
                    or member.islnk()
                ):
                    failure = f"{archive.name} holds a special file"
                    break
                if (
                    absolute is not None
                    and parts[0] == "site"
                    and member.issym()
                    and os.path.isabs(member.linkname)
                ):
                    absolute.append((name, member.linkname))
                    continue
                if problem := member_problem(member, name, dest):
                    failure = f"{archive.name}: {problem}"
                    break
                member.name = name
                tar.extract(member, dest, filter="data")
            else:
                finished = True
    except (OSError, tarfile.TarError) as exc:
        failure = failure or f"cannot unpack {archive.name}: {exc}"
    finally:
        # A refused archive is not read to its end: draining it would
        # buffer the rest of the decompressed stream in memory
        if not finished:
            zstd.kill()
        _, err = zstd.communicate()
    if zstd.returncode and not failure:
        failure = (
            f"zstd failed on {archive.name}: {err.decode(errors='replace').strip()}"
        )
    if not failure and (links := link_problems(dest)):
        failure = (
            f"{archive.name}: symlinks resolve outside their tree or into a "
            f"skipped path: {', '.join(links[:10])}"
        )
    if failure:
        raise SiteImportError(failure)


def hooks_dir(config: Path) -> str:
    """The site's hooks directory, relative to the site.

    hooks.path in gerrit.config, which a chart site sets to a path
    under /var/gerrit, else Gerrit's default, hooks.
    """
    value = config_get(config, "hooks.path").rstrip("/") or "hooks"
    if os.path.isabs(value):
        value = os.path.relpath(os.path.normpath(value), POD_SITE)
    return os.path.normpath(value)


def skip_regenerated(
    links: list[tuple[str, str]], config: Path, tools_bin: Path
) -> None:
    """Accept the absolute links site-init recreates at every start.

    A chart site with gerrit-to-platform hooks holds links from its
    hooks directory to the entry points in tools_bin, and one from
    gerrit-to-platform's configuration directory to replication.config.
    They are left out of the import, since site-init makes them again
    when hooks are enabled. Any other absolute link is refused.
    """
    hooks = os.path.normpath(os.path.join("site", hooks_dir(config)))
    refused = []
    for name, target in links:
        clean = os.path.normpath(target)
        g2p_hook = os.path.dirname(name) == hooks and clean == os.path.join(
            tools_bin, os.path.basename(name)
        )
        if g2p_hook or (name, clean) == G2P_REPLICATION_LINK:
            say(f"skipping {name} -> {target}, which site-init recreates")
        else:
            refused.append(name)
    if refused:
        raise SiteImportError(
            "site.tar.zst: symlinks to absolute paths: " + ", ".join(refused[:10])
        )
