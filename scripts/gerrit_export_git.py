# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Copy a live repository store for ``gerrit-site-export.py backup``.

rsync races a running Gerrit: a concurrent ``pack-refs`` can hide refs
from it, a concurrent gc can remove objects a copied ref reaches, and
it copies lock files. git's own transport does not race. ``git bundle
create --all`` reads a repository's refs once and packs every object
they reach, never a lock file, so each bare repository (each ``*.git``
directory) is bundled, in a private scratch directory under the export
directory, and fetched into a fresh bare repository, which then gets
the source's ``config``, ``description``, ``info/`` and ``HEAD``, which
must not change while the copy runs. A detached ``HEAD`` keeps its
commit, fetched from the source, since ``refs/*`` may not reach it.
Files take the tool's 0077 umask, never a source mode, so the copy is
owner-only; git runs with ``core.sharedRepository=umask``, so a copied
config that shares the repository with a group keeps that setting
without applying it to the copy.
``git rev-list --objects --all`` must find every object the copy's
refs reach, and the copy must hold exactly the refs the bundle
recorded. Anything else in the store goes through rsync, without lock
files.

Gerrit reserves change numbers from ``refs/sequences/changes``, in
All-Projects, before it creates a change in any repository, so the
repositories holding that ref are copied last: their copy then reads a
sequence at least as new as every change copied before it. The copied
sequence must exceed every change number in the copies, or a restored
site would hand out a number twice.

Any failure raises RepositoryCopyError, an OSError the export tool
reports like its other failures, and nothing is published. The export
tool loads this module only in backup mode, from its own directory;
seed and final need only the tool itself. Standard library only,
Python 3.8 or newer.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROG = "gerrit-site-export.py"
GIT = ["git", "-c", "safe.directory=*", "-c", "core.sharedRepository=umask"]
REF_LINE = "--format=%(objectname) %(refname)"
SEQUENCE = "refs/sequences/changes"  # The next change number, as a decimal blob.
CHANGES = "refs/changes/"  # refs/changes/<last two digits>/<number>/...
BUNDLE = "repository.bundle"  # In a private scratch directory under the export one.
SCRATCH = ".scratch-"
RSYNC = [
    "rsync",
    "-a",
    "--chmod=go-rwx",
    "--delete",
    "--delete-excluded",
    "--exclude=*.lock",
]
VANISHED = 24  # rsync: files vanished mid-copy, expected while Gerrit writes.


class RepositoryCopyError(OSError):
    """A repository could not be copied, or its copy failed a check."""


def repositories(root: str | Path) -> list[str]:
    """The bare repositories under root, the *.git directories, relative."""
    found = []

    def fail(exc: OSError) -> None:
        raise RepositoryCopyError(f"cannot list repositories: {exc}")

    for top, dirs, _files in os.walk(root, onerror=fail):
        for name in [d for d in dirs if d.endswith(".git")]:
            dirs.remove(name)
            found.append(Path(top, name).relative_to(root).as_posix())
    return sorted(found)


def git(repo: Path | None, *args: str, failure: str = "") -> str:
    """Run git on repo; raise with failure and git's stderr if it fails."""
    command = [*GIT, *([f"--git-dir={repo}"] if repo else []), *args]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if failure and result.returncode:
        raise RepositoryCopyError(f"{failure}: {result.stderr.strip()}")
    return result.stdout


def copy_info(src: Path, dest: Path) -> None:
    """Copy the info/ tree without lock files; modes come from the umask.

    A repository without info/ copies none; any other listing error fails.
    """

    def fail(exc: OSError) -> None:
        if isinstance(exc, FileNotFoundError) and Path(exc.filename) == src:
            return
        raise RepositoryCopyError(f"cannot read {exc.filename}: {exc.strerror}")

    for top, _dirs, files in os.walk(src, onerror=fail):
        target = dest / Path(top).relative_to(src)
        target.mkdir(exist_ok=True)
        for name in (f for f in files if not f.endswith(".lock")):
            shutil.copyfile(Path(top, name), target / name)


def read_head(repo: Path) -> tuple[str, bool]:
    """HEAD of repo: the ref it names and False, the commit it is
    detached at and True, or "" and True when it resolves to nothing.
    """
    if name := git(repo, "symbolic-ref", "-q", "HEAD").strip():
        return name, False
    return git(repo, "rev-parse", "--verify", "-q", "HEAD^0").strip(), True


def copy_repository(rel: str, src: Path, dest: Path, bundle: Path) -> None:
    """Copy the repository src to dest, which must not exist yet."""
    git(None, "init", "-q", "--bare", "--template=", str(dest), failure=rel)
    # Read before the bundle, so a branch it names is in the bundle or unborn.
    head, detached = read_head(src)
    heads = ""
    if git(src, "for-each-ref", "--count=1", failure=f"cannot read refs of {rel}"):
        create = ["bundle", "create", str(bundle), "--all"]
        git(src, *create, failure=f"cannot bundle repository {rel}")
        heads = git(dest, "bundle", "list-heads", str(bundle), failure=rel)
        fetch = ["fetch", "-q", str(bundle), "+refs/*:refs/*"]
        git(dest, *fetch, failure=f"cannot fetch the bundle of {rel}")
        bundle.unlink()
        (dest / "FETCH_HEAD").unlink(missing_ok=True)
    for name in ("config", "description"):  # Either may be absent.
        if (src / name).is_file():
            shutil.copyfile(src / name, dest / name)
    copy_info(src / "info", dest / "info")
    if read_head(src) != (head, detached):
        raise RepositoryCopyError(f"HEAD of {rel} changed during the copy; run again")
    if head and detached:
        fetch = ["fetch", "-q", "--no-tags", str(src), head]
        git(dest, *fetch, failure=f"cannot fetch the detached HEAD of {rel}")
        (dest / "FETCH_HEAD").unlink(missing_ok=True)
        set_head = ["update-ref", "--no-deref", "HEAD", head]
        git(dest, *set_head, failure=f"cannot set HEAD of {rel}")
    elif head:
        git(dest, "symbolic-ref", "HEAD", head, failure=f"cannot set HEAD of {rel}")
    # Not fsck, which fails on a valid HEAD outside refs/heads, as All-Projects' is.
    check = ["rev-list", "--objects", "--all", "--quiet"]
    git(dest, *check, failure=f"connectivity check failed for the copy of {rel}")
    snapshot = sorted(h for h in heads.splitlines() if not h.endswith(" HEAD"))
    if sorted(git(dest, "for-each-ref", REF_LINE).splitlines()) != snapshot:
        raise RepositoryCopyError(f"the copy of {rel} differs from its bundle's refs")
    if read_head(dest) != (head, detached):
        raise RepositoryCopyError(f"the copy of {rel} has another HEAD than its source")


def holds_sequence(repo: Path) -> bool:
    """Whether repo holds the change sequence, as All-Projects does."""
    return bool(git(repo, "for-each-ref", "--count=1", SEQUENCE).strip())


def check_sequence(store: Path, repos: list[str]) -> None:
    """Fail unless the copied change sequence exceeds every copied change."""
    newest, owner, holders = 0, "", []
    for rel in repos:
        repo = store / rel
        for ref in git(repo, "for-each-ref", "--format=%(refname)", CHANGES).split():
            number = (ref.split("/") + [""] * 4)[3]
            if number.isdigit() and int(number) > newest:
                newest, owner = int(number), rel
        if holds_sequence(repo):
            holders.append(rel)
    if newest and not holders:
        raise RepositoryCopyError(f"change {newest} in {owner} has no {SEQUENCE}")
    for rel in holders:
        value = git(store / rel, "cat-file", "blob", SEQUENCE, failure=rel).strip()
        if not value.isdigit():
            raise RepositoryCopyError(f"{SEQUENCE} of {rel} is not a number: {value!r}")
        if int(value) <= newest:
            raise RepositoryCopyError(
                f"{SEQUENCE} of {rel} is {value}, not above change {newest} in"
                f" {owner}; Gerrit created changes during the copy, run again"
            )


def copy(root: str, out: Path, dry_run: bool) -> None:
    """Copy the repository store root into out/git; dry_run only prints."""
    rsync = [*RSYNC, "--exclude=*.git/", f"{root}/", f"{out}/git/"]
    repos = repositories(root)
    # Stable: the change sequence's holders go last, the rest keep their order.
    repos.sort(key=lambda rel: holds_sequence(Path(root, rel)))
    if dry_run:
        print("+ " + shlex.join(rsync), flush=True)
        bundle = out / f"{SCRATCH}XXXXXXXX" / BUNDLE
        for rel in repos:
            create = [*GIT, f"--git-dir={Path(root, rel)}", "bundle", "create"]
            print("+ " + shlex.join([*create, str(bundle), "--all"]), flush=True)
        return
    # --delete-excluded removes the copies an earlier run left, repositories too.
    if (status := subprocess.run(rsync, check=False).returncode) == VANISHED:
        print(f"{PROG}: warning: files vanished during the copy", file=sys.stderr)
    elif status:
        raise RepositoryCopyError(f"rsync failed with exit status {status}")
    scratch = Path(tempfile.mkdtemp(prefix=SCRATCH, dir=out))  # Mode 0700.
    try:
        for rel in repos:
            copy_repository(rel, Path(root, rel), out / "git" / rel, scratch / BUNDLE)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    check_sequence(out / "git", repos)
