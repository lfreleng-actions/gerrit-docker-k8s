# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Compare the site's Gerrit release with the image's, and upgrade.

``site-init.sh`` runs this script, standard library only, on a site
that holds ``.chart/initialised`` or an unfinished upgrade, before it
replaces the site's binaries or configuration, so a refusal leaves
both as they were. It reads
the site's release from the marker's ``gerritVersion`` and the image's
from its ``gerrit.war``, compares them as releases, and acts on
``siteUpgrade.mode`` (``K8S/docs/DESIGN.md`` section 1.3):

- ``off``: logs a warning when they differ, and lets Gerrit start;
- ``refuse``: exits non-zero when they differ, so Gerrit never starts;
- ``auto``: refuses an older image; for a newer one it takes the
  backup lock, records the upgrade in ``.chart/upgrade-pending``,
  removes the marker, installs the image's binaries and the rendered
  configuration through ``site-init.sh`` in prepare-only mode, runs
  ``gerrit init``, installs the rendered configuration again, runs an
  offline reindex with ``siteUpgrade.reindex``, and only then writes
  the marker with the new release and ``upgradedFrom``.

It then exits with status 3, ``UPGRADED``, so ``site-init.sh`` does
not install the binaries and configuration again outside the lock.
A failure leaves the marker absent and the pending record in place,
so the next start in ``auto`` retries the upgrade and no start runs
Gerrit on a half-upgraded site. A retry needs an image at least as new
as the release the record names as its target, since ``init`` may
already have migrated the site to it, and the backup CronJob skips the
site while the record exists. A refusal's message also goes to the
container's termination message.

Configuration comes from the environment the chart sets:

- ``GERRIT_SITE``: the site volume (``/mnt/site``)
- ``GERRIT_IMAGE_BIN``: the image's ``bin`` holding ``gerrit.war``
- ``SITE_UPGRADE_MODE``: ``off``, ``refuse`` or ``auto``
- ``SITE_UPGRADE_REINDEX``: ``true`` or ``false``
- ``SITE_UPGRADE_JAVA_HEAP``: heap options for ``init`` and ``reindex``
- ``SITE_UPGRADE_CPU_LIMIT``: the container's CPU limit, which sets
  ``reindex --threads``; Gerrit's default when empty
- ``GERRIT_RUNTIME_OVERRIDES``: the operator's overrides ConfigMap,
  whose ``chart.siteUpgrade.mode`` replaces ``SITE_UPGRADE_MODE``
- ``GERRIT_TERMINATION_LOG``: where a refusal's message goes,
  ``/dev/termination-log`` by default
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from site_import_common import (
    SiteImportError,
    cpu_threads,
    flag,
    gerrit_version,
    utc_now,
    write_json,
)
from site_upgrade_release import (
    MODES,
    OFF,
    STOP,
    UPGRADE,
    WARN,
    UpgradeError,
    decide,
)

PROG = "site-upgrade"
# The exit status after an upgrade, which has installed everything
# site-init would: site-init then skips its own installs, which would
# otherwise rewrite the site after the backup lock is released
UPGRADED = 3
OVERRIDE_KEY = "chart.siteUpgrade.mode"

MARKER = Path(".chart") / "initialised"
PENDING = Path(".chart") / "upgrade-pending"
LOCK = Path(".chart") / "backup-lock" / "lock"
JAVA_OPTS = [
    "-Duser.home=/tmp",
    "--add-opens",
    "java.base/java.net=ALL-UNNAMED",
    "--add-opens",
    "java.base/java.lang.invoke=ALL-UNNAMED",
]


def say(message: str) -> None:
    print(f"{PROG}: {message}", flush=True)


def warn(message: str) -> None:
    print(f"{PROG}: warning: {message}", file=sys.stderr, flush=True)


@dataclass
class Settings:
    """What the chart asked for, from the environment."""

    site: Path
    image_bin: Path
    site_init: Path
    mode: str = OFF
    reindex: bool = True
    java_heap: list[str] = field(default_factory=list)
    reindex_threads: int | None = None
    overrides: Path | None = None
    termination_log: Path = Path("/dev/termination-log")
    java: str = "java"

    @classmethod
    def from_env(cls, env: dict[str, str]) -> Settings:
        site = env.get("GERRIT_SITE", "")
        if not site:
            raise UpgradeError("GERRIT_SITE is not set")
        overrides = env.get("GERRIT_RUNTIME_OVERRIDES", "")
        try:
            reindex = flag(env.get("SITE_UPGRADE_REINDEX", "true"))
            threads = cpu_threads(env.get("SITE_UPGRADE_CPU_LIMIT", ""))
        except SiteImportError as exc:
            raise UpgradeError(str(exc)) from exc
        return cls(
            site=Path(site),
            image_bin=Path(env.get("GERRIT_IMAGE_BIN", "/var/gerrit/bin")),
            site_init=Path(__file__).with_name("site-init.sh"),
            mode=env.get("SITE_UPGRADE_MODE", OFF).strip() or OFF,
            reindex=reindex,
            java_heap=env.get("SITE_UPGRADE_JAVA_HEAP", "").split(),
            reindex_threads=threads,
            overrides=Path(overrides) if overrides else None,
            termination_log=Path(
                env.get("GERRIT_TERMINATION_LOG", "/dev/termination-log")
            ),
        )


def override_mode(settings: Settings) -> str | None:
    """Return the overrides ConfigMap's mode, when it sets a valid one.

    An absent or empty entry is no override; any other value is logged,
    without the value, and ignored.
    """
    if settings.overrides is None:
        return None
    path = settings.overrides / OVERRIDE_KEY
    if not path.is_file():
        return None
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        return None
    if value not in MODES:
        print(
            f"Runtime override {OVERRIDE_KEY} is not off, refuse or auto; ignored",
            file=sys.stderr,
            flush=True,
        )
        return None
    print(f"Runtime override: set {OVERRIDE_KEY} = {value}", flush=True)
    return value


def read_json(path: Path) -> dict[str, Any] | None:
    """Return a JSON object from *path*, None without the file.

    A file that holds no JSON object reads as an empty one, which
    records no release: site-init only tests that the marker exists.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except ValueError:
        return {}
    except OSError as exc:
        raise UpgradeError(f"cannot read {path}: {exc.strerror}") from exc
    return data if isinstance(data, dict) else {}


def recorded_release(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) and value.strip() else None


def marker_record(image: str, site: str, now: str) -> dict[str, Any]:
    """The marker an upgrade writes once init and reindex succeed."""
    return {"gerritVersion": image, "initialisedAt": now, "upgradedFrom": site}


@contextmanager
def backup_lock(site: Path) -> Iterator[None]:
    """Hold the backup CronJob's flock, waiting for a running backup."""
    path = site / LOCK
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            say(f"waiting for the backup that holds {LOCK}")
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def timed(label: str, command: list[str], env: dict[str, str] | None = None) -> None:
    """Run one step of the upgrade, logging it and the time it took."""
    say(f"{label}: started")
    start = time.monotonic()
    try:
        status = subprocess.run(command, env=env, check=False).returncode
    except FileNotFoundError as exc:
        raise UpgradeError(f"{label}: {command[0]} not found") from exc
    elapsed = time.monotonic() - start
    if status:
        raise UpgradeError(
            f"{label} failed with exit status {status} after {elapsed:.0f}s"
        )
    say(f"{label}: done in {elapsed:.0f}s")


def gerrit(settings: Settings, *args: str) -> list[str]:
    war = settings.site / "bin" / "gerrit.war"
    return [
        settings.java,
        *JAVA_OPTS,
        *settings.java_heap,
        "-jar",
        str(war),
        *args,
        "-d",
        str(settings.site),
    ]


def upgrade(settings: Settings, site: str, image: str) -> None:
    """Install the image's binaries and the render, run init, install
    the render again, reindex, then rewrite the marker, all under the
    backup lock.
    """
    start = time.monotonic()
    prepare = {
        **os.environ,
        "GERRIT_SITE": str(settings.site),
        "GERRIT_SITE_PREPARE_ONLY": "1",
    }
    install = ["sh", str(settings.site_init)]
    with backup_lock(settings.site):
        write_json(
            settings.site / PENDING,
            {"from": site, "to": image, "startedAt": utc_now()},
        )
        (settings.site / MARKER).unlink(missing_ok=True)
        timed("installing the image's binaries and configuration", install, prepare)
        timed("gerrit.war init", gerrit(settings, "init", "--batch", "--no-auto-start"))
        timed("installing the rendered configuration", install, prepare)
        if settings.reindex:
            threads = settings.reindex_threads
            extra = ["--threads", str(threads)] if threads else []
            timed("gerrit.war reindex", gerrit(settings, "reindex", *extra))
        else:
            say("siteUpgrade.reindex is false; skipping the offline reindex")
        write_json(settings.site / MARKER, marker_record(image, site, utc_now()))
        (settings.site / PENDING).unlink(missing_ok=True)
    elapsed = time.monotonic() - start
    say(f"upgraded the site from Gerrit {site} to {image} in {elapsed:.0f}s")


def refuse(settings: Settings, message: str) -> int:
    """Log *message* and leave it as the container's termination message."""
    print(f"{PROG}: error: {message}", file=sys.stderr, flush=True)
    if settings.termination_log.exists():
        try:
            settings.termination_log.write_text(message + "\n", encoding="utf-8")
        except OSError as exc:
            warn(f"cannot write {settings.termination_log}: {exc.strerror}")
    return 1


def main(env: dict[str, str]) -> int:
    try:
        settings = Settings.from_env(env)
    except UpgradeError as exc:
        print(f"{PROG}: error: {exc}", file=sys.stderr, flush=True)
        return 1
    try:
        mode = override_mode(settings) or settings.mode
        marker = read_json(settings.site / MARKER)
        pending_record = read_json(settings.site / PENDING)
        if marker is not None:
            # The marker is the site's state; a record beside it is stale
            (settings.site / PENDING).unlink(missing_ok=True)
            site = recorded_release(marker, "gerritVersion")
            target = None
            pending = False
        elif pending_record is not None:
            site = recorded_release(pending_record, "from")
            target = recorded_release(pending_record, "to")
            pending = True
        else:
            return 0
        image = None
        if site is not None or mode != OFF or pending:
            image = gerrit_version(settings.image_bin / "gerrit.war")
        decision = decide(mode, site, image, pending, target)
        if decision.action == WARN:
            warn(decision.message)
        elif decision.action == STOP:
            return refuse(settings, decision.message)
        elif decision.action == UPGRADE and site and image:
            say(decision.message)
            upgrade(settings, site, image)
            return UPGRADED
    except (UpgradeError, OSError) as exc:
        return refuse(settings, f"the Gerrit upgrade stopped: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main(dict(os.environ)))
