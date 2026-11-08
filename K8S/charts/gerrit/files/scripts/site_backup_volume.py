# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""The site volume, as the chart's backup CronJob, ``site-backup.py``,
sees it: the lock that serialises its runs, and the git store.
"""

from __future__ import annotations

import fcntl
import os
import posixpath
import subprocess
from collections.abc import Mapping
from pathlib import Path

# The site volume's path in the Gerrit container, which an absolute
# gerrit.basePath names
GERRIT_SITE = "/var/gerrit"


class VolumeError(Exception):
    """A site volume the backup cannot lock or read its git store from."""


class LockMissing(Exception):
    """The site has no backup lock: no Gerrit pod has finished
    initialising it under a chart version whose site-init creates one,
    as its last step.
    """


def hold_lock(path: Path) -> int | None:
    """Take the backup lock and return its descriptor, or None when
    another run holds it. Closing the descriptor, or the process
    exiting however it does, releases it.

    site-init creates the lock file; the backup only opens it, read-only
    as its mount of the site is, since flock needs no write access.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    except FileNotFoundError as exc:
        raise LockMissing(f"{path} does not exist") from exc
    except OSError as exc:
        raise VolumeError(f"cannot open the backup lock {path}: {exc}") from exc
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except OSError as exc:
        os.close(fd)
        raise VolumeError(f"cannot lock {path}: {exc}") from exc
    return fd


def git_store(env: Mapping[str, str], site: Path) -> Path:
    """The repository store gerrit.basePath names, on the site volume.

    A relative basePath is under the site; an absolute one names a path
    in the Gerrit container, where the site volume is at /var/gerrit,
    and must lie on that volume.
    """
    config = site / "etc" / "gerrit.config"
    if not config.is_file():
        raise VolumeError(f"cannot read {config}")
    command = ["git", "config", "-f", str(config), "--get", "gerrit.basePath"]
    result = subprocess.run(
        command, env=dict(env), stdout=subprocess.PIPE, text=True, check=False
    )
    if result.returncode > 1:
        raise VolumeError(f"cannot read gerrit.basePath from {config}")
    base = result.stdout.strip() or "git"
    if posixpath.isabs(base):
        normal = posixpath.normpath(base)
        if not normal.startswith(f"{GERRIT_SITE}/"):
            raise VolumeError(
                f"gerrit.basePath {base} is not on the site volume; the chart "
                f"backs up a git store only under {GERRIT_SITE}"
            )
        base = normal.removeprefix(f"{GERRIT_SITE}/")
    elif posixpath.normpath(base).split("/")[0] == "..":
        raise VolumeError(f"gerrit.basePath {base} is not on the site volume")
    return site / base
