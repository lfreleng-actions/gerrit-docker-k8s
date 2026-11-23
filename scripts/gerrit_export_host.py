# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Fit ``gerrit-site-export.py`` to a host the chart did not build.

``foreign_links`` finds, before anything is written, the symlinks the
export would copy from the site that ``site-import`` refuses: a link to
an absolute path, or a relative one that climbs above the site, or
resolves, through the relative links the archive keeps, into a tree
the import skips, such as ``plugins``, either once unpacked or as it
unpacks, through the links before it in the archive. Leaving a link
out can change where another resolves, so the rest are checked again
without it. Links within those trees pass, as the import never reads
them. It follows ``site_import_unpack.py``, including its one
exception: a link in the site's hooks directory to the
gerrit-to-platform entry point of the same name in TOOLS_BIN, which
``site-init`` recreates. The hooks directory is ``hooks.path`` from
``etc/gerrit.config``, an absolute one taken relative to the pod's
site, as the import does.

``refuse_structure`` stops every mode before anything is written
unless each path the import needs, such as ``etc/gerrit.config``,
``data/replication`` or the SAML keystore, is a real file or directory
the copy holds: a link on it, above it or anywhere beneath it, or the
path lying in one the export leaves out, such as ``logs``, a ``*.lock``
directory or the repository store, which leaves the site for ``git/``,
or in one the import skips, such as ``plugins``, stops it, whatever the
options, as does a configured SAML keystore that is not a regular file.

``zstd_method`` picks the packer for ``--zstd``, and ``pack`` writes
``site.tar.zst`` or ``git.tar.zst`` with it, owner-only, and reads it
back to its end: the ``zstd`` program, or Python's ``compression.zstd``
(3.14 and later), so a host without the program can still pack.

``refuse_running`` stops ``final`` while a Gerrit on this host serves
the site, or may: it reads ``/proc``, or ``GERRIT_SITE_EXPORT_PROC``,
which only the tests set.

Failures raise HostError, an OSError the export tool reports like its
other failures. The tool loads this module, from its own directory, in
every mode. Standard library only, Python 3.8 or newer.
"""

from __future__ import annotations

import importlib
import os
import shlex
import shutil
import subprocess
import tarfile
from pathlib import Path
from subprocess import PIPE
from types import ModuleType
from typing import Callable

POD_SITE = "/var/gerrit"
# What site-import skips, as site_import_unpack.SKIPPED: no link may point
# into it, and links within TREES pass. The tool checks the links on or in
# the credential paths that follow them.
TREES = (".chart", "index", "cache", "lost+found", "bin", "plugins", "lib")
SKIPPED = (*TREES, ".ssh", ".config/gerrit_to_platform", "etc/lfs.secure.config")
TOOLS_BIN = "/opt/gerrit-tools/bin"
KEYSTORE = "etc/samlKeystore.jks"  # As site_import_archive.DEFAULT_KEYSTORE.
ZSTD = ["zstd", "-q", "-T0", "-c"]
PYTHON_ZSTD = "compression.zstd"  # Stands for the Python packer in dry runs.
NEW_FILE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
CHOICES = ("auto", "tool", "python")
HINT = "stop writes and pass --i-stopped-writes"
ELSEWHERE = "another filesystem namespace, so its site cannot be compared"
REMEDIES = (
    "install the zstd program, or run the tool with Python 3.14 or newer, as"
    " uv run --no-project --python 3.14 gerrit-site-export.py ... does"
)


class HostError(OSError):
    """This host or its site cannot give an export site-import accepts."""


def python_zstd() -> ModuleType | None:
    """Python's compression.zstd, or None before Python 3.14."""
    try:
        return importlib.import_module(PYTHON_ZSTD)
    except ImportError:
        return None


def zstd_method(choice: str, packing: bool) -> str:
    """The packer --zstd choice names: "tool", "python", or "" to not pack.

    "tool" is returned without the program too: the tool's check for
    the programs it needs reports it missing.
    """
    if choice not in CHOICES:
        raise HostError(f"--zstd takes {', '.join(CHOICES)}, not {choice!r}")
    if not packing:
        return ""
    if choice == "tool" or (choice == "auto" and shutil.which("zstd")):
        return "tool"
    if python_zstd() is None:
        lacking = "Python 3.14 or newer" if choice == "python" else "zstd"
        raise HostError(f"--zstd {choice}: packing needs {lacking}; {REMEDIES}")
    return "python"


def hooks_dir(site: str) -> str:
    """The site's hooks directory relative to it, as site-import reads it."""
    config = os.path.join(site, "etc", "gerrit.config")
    command = ["git", "config", "-f", config, "--get", "hooks.path"]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode > 1:
        raise HostError(f"cannot read hooks.path from {config}")
    value = result.stdout.strip().rstrip("/") or "hooks"
    if os.path.isabs(value):
        value = os.path.relpath(os.path.normpath(value), POD_SITE)
    return os.path.normpath(value)


def left_out(parts: list[str], names: list[list[str]], globs: list[list[str]]) -> bool:
    """Whether rsync leaves out the site path parts: any part a *.lock
    name, or one under a path in names, or a pattern in globs, whose last
    component may end in * for a name prefix.
    """

    def under(pattern: list[str], glob: bool) -> bool:
        last = len(pattern) - 1
        return len(pattern) <= len(parts) and all(
            a == b or (glob and i == last and a[-1:] == "*" and b.startswith(a[:-1]))
            for i, (a, b) in enumerate(zip(pattern, parts))
        )

    return (
        any(part.endswith(".lock") for part in parts)
        or any(under(n, False) for n in names)
        or any(under(g, True) for g in globs)
    )


def in_skipped(parts: list[str]) -> bool:
    rel = "/".join(parts)
    return any(rel == s or rel.startswith(f"{s}/") for s in SKIPPED)


def landing(
    site: str, name: str, target: str, kept: Callable[[list[str]], bool]
) -> str:
    """Where the relative link name -> target resolves in the imported
    site, relative to it, or "" when it leaves the site.

    A path that climbs above the site leaves it, even to come back: the
    import unpacks the site as site/, but places it at /var/gerrit, where
    ../../site/data would point at /var/site/data. It follows the site's
    links only where kept(parts) says the import holds them, and never
    an absolute one, which the import sets aside before it checks any
    link: it lacks the others, such as a plugins link it skips, so a
    path through one stays on it and nothing lies below it, as with
    os.path.realpath on the imported site.
    """
    resolved, pending = ["site", *Path(name).parts[:-1]], target.split("/")
    hops = absent = 0  # absent: how many trailing parts the import lacks.
    while pending:
        part = pending.pop(0)
        if part == "..":
            if len(resolved) < 2:
                return ""
            resolved.pop()
            absent = max(absent - 1, 0)
        elif part not in ("", "."):
            resolved.append(part)
            path = os.path.join(site, *resolved[1:])
            if absent:
                absent += 1
            elif not os.path.islink(path):
                continue
            elif not kept(resolved[1:]) or os.path.isabs(link := os.readlink(path)):
                absent = 1
            else:
                hops += 1
                if hops > 40:
                    return ""
                resolved.pop()
                pending[:0] = link.split("/")
    return "/".join(resolved[1:]) or "."


def refused(
    site: str, name: str, target: str, hooks: str, kept: Callable[[list[str]], bool]
) -> bool:
    """Whether site-import refuses the link name, relative to site; hooks
    is the hooks directory as the archive names it, under site/, and
    kept(parts) whether the archive holds the site path parts.

    The import checks a relative link twice: as it unpacks it, through
    the links unpacked before it, and with every member in place. The
    archive holds its members in sorted path order, as tarfile adds a
    directory's entries.
    """
    if os.path.isabs(target):
        entry = os.path.join(TOOLS_BIN, os.path.basename(name))
        in_hooks = os.path.dirname(f"site/{name}") == hooks
        return not (in_hooks and os.path.normpath(target) == entry)
    order = name.split("/")

    def before(parts: list[str]) -> bool:
        return parts < order and kept(parts)

    return any(
        not rel or in_skipped(rel.split("/"))
        for rel in (
            landing(site, name, target, kept),
            landing(site, name, target, before),
        )
    )


def foreign_links(
    site: str, names: list[str], globs: list[str]
) -> list[tuple[str, str]]:
    """The links site-import refuses among those the export copies from
    site, resolved, as sorted (path relative to site, target) pairs.

    names are the paths the export leaves out, globs its --exclude
    patterns. Leaving a link out can change where another resolves, so
    the links left are checked again without those found until no more
    are. Unreadable directories pass: rsync fails on them.
    """
    hooks = os.path.normpath(os.path.join("site", hooks_dir(site)))
    out = [n.split("/") for n in names], [g.split("/") for g in globs]
    found: dict[str, str] = {}

    def kept(parts: list[str]) -> bool:
        rel = "/".join(parts)
        return not (rel in found or in_skipped(parts) or left_out(parts, *out))

    links = []
    for top, dirs, files in os.walk(site):
        base = Path(os.path.relpath(top, site)).parts
        for name in [*dirs, *files]:
            parts = [*base, name]
            if left_out(parts, *out) or "/".join(parts) in TREES:
                if name in dirs:
                    dirs.remove(name)
                continue
            path = os.path.join(top, name)
            if os.path.islink(path):
                links.append(("/".join(parts), os.readlink(path)))
    while more := {
        rel: target
        for rel, target in links
        if rel not in found and refused(site, rel, target, hooks, kept)
    }:
        found.update(more)
    return sorted(found.items())


def refuse_structure(
    site: str, needed: list[str], names: list[str], store: list[str], globs: list[str]
) -> None:
    """Raise HostError unless each site path in needed, those site-import
    needs, is a real file or directory, or absent, that the copy holds:
    no link on it, above it or beneath it, and not left out with names,
    the --exclude patterns globs, or as a *.lock path. The last of them,
    the SAML keystore, must be a regular file unless it is KEYSTORE,
    which site-import lets a site without SAML leave out.

    store, among names, are the repository store's site paths: the store
    itself moves to git/ and passes, with the links in it, which the tool
    refuses itself; a path within it does not.
    """
    out, moved, wild = ([n.split("/") for n in ns] for ns in (names, store, globs))
    replace = "; replace the link with the file or directory it points to"
    for rel in needed:
        parts = rel.split("/")
        if parts in moved:
            continue
        for depth, _ in enumerate(parts, 1):
            if os.path.islink(os.path.join(site, *parts[:depth])):
                link = "/".join(parts[:depth])
                where = "is" if depth == len(parts) else f"lies beneath {link},"
                raise HostError(
                    f"site-import needs {rel}, which {where} a link{replace}"
                )
        if left_out(parts, out, wild) or in_skipped(parts):
            raise HostError(
                f"site-import needs {rel}, which lies in a path the export leaves"
                " out or site-import skips; move it to a path both keep"
            )
        top = os.path.join(site, *parts)
        for root, dirs, files in os.walk(top) if os.path.isdir(top) else ():
            base = Path(*parts, os.path.relpath(root, top))
            dirs[:] = [d for d in dirs if (base / d).parts not in map(tuple, moved)]
            for name in (n for n in dirs + files if os.path.islink(Path(root, n))):
                link = (base / name).as_posix()
                raise HostError(
                    f"site-import needs {link}, a link under {rel}{replace}"
                )
    keystore = needed[-1]
    if keystore != KEYSTORE and not os.path.isfile(os.path.join(site, keystore)):
        raise HostError(
            f"site-import needs the SAML keystore {keystore}, which is not a"
            " regular file; put the keystore there, or point saml.keystorePath"
            " at it"
        )


def pack(source: Path, archive: Path, method: str, dry_run: bool) -> None:
    """Pack source into archive, a new owner-only zstd tar, and verify it.

    The members start with source's name; method is a zstd_method().
    """
    tool = method == "tool"
    check = ["zstd", "-tq"] if tool else [PYTHON_ZSTD, "--test"]
    if dry_run:
        packer = ZSTD if tool else [PYTHON_ZSTD]
        for line in (
            ["tar", f"{source.name}/", "|", *packer, ">", str(archive)],
            [*check, str(archive)],
        ):
            print("+ " + shlex.join(line), flush=True)
        return
    (pack_tool if tool else pack_python)(source, archive)
    if tool and (status := subprocess.run([*check, str(archive)]).returncode):
        raise HostError(
            f"integrity check of {archive.name} failed with exit status {status}"
        )


def pack_tool(source: Path, archive: Path) -> None:
    """Pack source into archive through the zstd program."""
    failure = ""
    with os.fdopen(os.open(archive, NEW_FILE, 0o600), "wb") as dest:
        zstd = subprocess.Popen(ZSTD, stdin=PIPE, stdout=dest, bufsize=0)
        try:
            with tarfile.open(fileobj=zstd.stdin, mode="w|") as tar:
                tar.add(str(source), arcname=source.name)
        except (OSError, tarfile.TarError) as exc:
            failure = str(exc)
        zstd.communicate()
    if status := zstd.returncode:  # First, as zstd failing breaks the pipe.
        raise HostError(f"zstd failed with exit status {status} packing {archive.name}")
    if failure:
        raise HostError(f"cannot pack {source.name}/: {failure}")


def pack_python(source: Path, archive: Path) -> None:
    """Pack source into archive with compression.zstd, then read it all
    back, as zstd -t does.
    """
    zstd = python_zstd()
    if zstd is None:
        raise HostError(f"packing needs Python 3.14 or newer; {REMEDIES}")
    try:
        dest = os.fdopen(os.open(archive, NEW_FILE, 0o600), "wb")
        with dest, tarfile.open(fileobj=dest, mode="w|zst") as tar:
            tar.add(str(source), arcname=source.name)
    except (OSError, tarfile.TarError, zstd.ZstdError) as exc:
        raise HostError(f"cannot pack {source.name}/: {exc}") from exc
    try:
        with zstd.open(archive, "rb") as stream:
            while stream.read(1 << 20):
                pass
    except (OSError, EOFError, zstd.ZstdError) as exc:
        raise HostError(f"integrity check of {archive.name} failed: {exc}") from exc


def served_site(argv: list[str]) -> str | None:
    """The -d/--site-path of a '*.war ... daemon' command line, "." if none."""
    wars = [i for i, arg in enumerate(argv) if arg.endswith(".war")]
    if not wars or "daemon" not in argv[wars[0] + 1 :]:
        return None
    site, args = ".", iter(argv[argv.index("daemon", wars[0] + 1) + 1 :])
    for arg in args:
        if arg in ("-d", "--site-path"):
            site = next(args, "")
        elif arg.startswith("--site-path="):
            site = arg.partition("=")[2]
        elif arg.startswith("-d") and not arg.startswith("--"):
            site = arg[2:]
    return site


def fs_view(proc: str, pid: str) -> tuple[int, int, str] | None:
    try:  # The root and mount namespace that pid resolves paths, such as -d, in.
        root = os.stat(f"{proc}/{pid}/root")
        return root.st_dev, root.st_ino, os.readlink(f"{proc}/{pid}/ns/mnt")
    except OSError:
        return None


def refuse_running(site: str) -> None:
    """Raise HostError while a Gerrit serves site; reads /proc, as ps truncates."""
    proc = os.environ.get("GERRIT_SITE_EXPORT_PROC") or "/proc"  # Set by tests.
    running = f"Gerrit is running on {site} (pid %s); stop it, or {HINT}"
    try:
        pids = sorted((e for e in os.listdir(proc) if e.isdigit()), key=int)
    except OSError as exc:
        message = f"cannot list processes in {proc} to check for Gerrit; {HINT}"
        raise HostError(message) from exc
    if os.path.lexists(pidfile := Path(site, "logs", "gerrit.pid")):
        pid = "".join(c for c in pidfile.read_text() if c.isdigit())
        if pid and os.path.isdir(os.path.join(proc, pid)):
            raise HostError(running % pid)
    if "1" not in pids:
        raise HostError(
            f"{proc} hides pid 1 (hidepid?), so it may hide Gerrit too; {HINT}"
        )
    doubt, site_stat, own = "", os.stat(site), fs_view(proc, "self")
    for pid in (p for p in pids if p != str(os.getpid())):
        base = os.path.join(proc, pid)
        vague = f"cannot tell which site Gerrit (pid {pid}) serves"
        try:
            with open(os.path.join(base, "cmdline"), "rb") as handle:
                raw = handle.read().rstrip(b"\0")
            served = served_site([os.fsdecode(a) for a in raw.split(b"\0")])
            if served is not None and (own is None or fs_view(proc, pid) != own):
                vague, served = f"Gerrit (pid {pid}) runs in {ELSEWHERE}", ""
            if served and not os.path.isabs(served):
                cwd = os.readlink(os.path.join(base, "cwd"))
                served = os.path.join(base, cwd, served)
        except OSError:  # Gone since the listing, or there but not inspectable.
            doubt = doubt or (vague if os.path.isdir(base) else "")
            continue
        try:  # stat, unlike realpath, fails where it cannot resolve a path.
            same = bool(served and os.path.samestat(os.stat(served), site_stat))
        except (FileNotFoundError, NotADirectoryError):
            same = False  # Nothing there, so not this site.
        except OSError:
            same, served = False, ""
        if same:  # Raised out here: HostError is an OSError.
            raise HostError(running % pid)
        if served == "":
            doubt = doubt or vague
    if doubt:
        raise HostError(f"{doubt}; stop it, or {HINT}")
