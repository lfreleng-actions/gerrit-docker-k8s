# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Back up the Gerrit site to S3: the chart's backup CronJob.

Each run:

0. takes an exclusive ``flock`` on ``<site>/.chart/backup-lock/lock``,
   which the Gerrit pod's site-init creates last, once the site is
   initialised, and holds it until the process exits, so runs are
   serialised: a run that finds the lock held logs that a backup is
   already running and exits 0 without touching the store, as does
   one that finds no lock file, on a site not yet initialised. Every
   backup pod runs on the Gerrit pod's node and mounts the same
   volume, so the kernel lock spans scheduled and manual Jobs, and a
   killed run never leaves a stale lock;
1. reads ``<site>/.chart/site-state.json``, the state site-import
   records, and skips the run, exiting 0, unless it records
   ``complete``: a site with no state file, or with an import in
   progress or failed, is not one to back up;
2. runs ``gerrit-site-export.py backup`` on the site volume, mounted
   read-only, into the scratch volume, with ``--git`` naming the git
   store ``gerrit.basePath`` names on that volume and the
   ``--exclude=<path>`` arguments the script takes, the chart's
   ``backup.exclude``, passed on; it then reads the state file again
   and discards the export, exiting 0, if it changed, as an import
   started meanwhile;
3. uploads ``site.tar.zst``, ``git.tar.zst`` and then
   ``manifest.json`` with ``rclone copyto`` to
   ``<url>/<UTC timestamp>-<run id>/``, the timestamp as
   ``%Y%m%dT%H%M%SZ`` and the run id eight random hex digits, so no
   two runs share a prefix, even started in the same second;
4. verifies the upload with ``rclone check --one-way``;
5. if ``latest`` names a backup without a valid ``published`` marker,
   as its run stopped before the marker, writes it; no other marker
   is rewritten, so one with no valid time leaves its prefix unpublished;
6. reads its own ``manifest.json`` back from the store, checks it is
   the one it uploaded, and checks again with ``rclone check`` that
   the archives it names are there with their sizes and checksums;
7. only then writes this run's prefix to ``<url>/latest``, which a
   site import from ``s3://<bucket>/<prefix>/latest`` restores, and
   then ``<url>/<prefix>/published``, the run's last write: a small
   JSON object, ``{"publishedAt": "<RFC 3339 UTC, to the
   microsecond>"}``, recording when the backup was published. As the
   only run in flight, it writes ``latest`` unconditionally;
8. prunes the backup prefixes under ``<url>`` as
   ``site_backup_retention.retain`` describes, with
   ``SITE_BACKUP_KEEP_DAILY``, ``SITE_BACKUP_KEEP_WEEKLY``,
   ``SITE_BACKUP_MIN_AGE_HOURS`` and ``SITE_BACKUP_MAX_RUN_HOURS``.

A run that fails before step 7 deletes its own prefix and exits 1
with ``latest`` unmoved, so a restore from ``latest`` always gets a
complete, verified backup. A run that stops after moving ``latest``
leaves its backup published all the same, and the next run writes
the missing marker in step 5. The CronJob's ``activeDeadlineSeconds``
stops any run after ``SITE_BACKUP_MAX_RUN_HOURS``; retention deletes
an unpublished upload, which only an interrupted run leaves, once it
is older than that.

rclone, as for site-import's s3 source, is configured only through
environment variables, with ``env_auth`` reading the AWS credentials
or the web identity token from the container's environment; no
credential reaches a command line or the log. Configuration, besides
those ``--exclude=<path>`` arguments, all from the environment:

- ``SITE_BACKUP_URL``: ``s3://<bucket>/<prefix>``
- ``SITE_BACKUP_S3_ENDPOINT``, ``SITE_BACKUP_S3_REGION``,
  ``SITE_BACKUP_S3_PROVIDER``: as ``siteImport.source.s3``
- ``SITE_BACKUP_KEEP_DAILY``, ``SITE_BACKUP_KEEP_WEEKLY``,
  ``SITE_BACKUP_MIN_AGE_HOURS``: retention, the last at least 1
- ``SITE_BACKUP_MAX_RUN_HOURS``: the Job's deadline in hours, at
  least 1
- ``SITE_BACKUP_SITE``: the site, default ``/mnt/site``
- ``SITE_BACKUP_SCRATCH``: scratch directory, default ``/scratch``
- ``SITE_BACKUP_EXPORTER``: the export tool, default
  ``/opt/gerrit-site-export/gerrit-site-export.py``
"""

from __future__ import annotations

import datetime
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from site_backup_retention import (
    RFC3339,
    STAMP_FORMAT,
    Listing,
    is_stamp,
    published_at,
    retain,
)
from site_backup_volume import LockMissing, VolumeError, git_store, hold_lock

PROG = "site-backup"
REMOTE = "sitebackup"
LATEST = "latest"
MANIFEST = "manifest.json"
PUBLISHED = "published"
STATE_FILE = Path(".chart") / "site-state.json"
LOCK_FILE = Path(".chart") / "backup-lock" / "lock"
UPLOADS = ("site.tar.zst", "git.tar.zst", MANIFEST)
URL = re.compile(r"s3://([^/]+)(/.*)?")
EXPORTER = "/opt/gerrit-site-export/gerrit-site-export.py"
RCLONE_FLAGS = ["--stats", "0", "--retries", "5", "--retries-sleep", "3s"]
RCLONE_FLAGS += ["--low-level-retries", "10"]


class BackupError(Exception):
    """A failure the job reports and exits 1 on."""


@dataclass(frozen=True)
class Settings:
    path: str  # <bucket>/<prefix>, without s3:// or a trailing /
    endpoint: str
    region: str
    provider: str
    keep_daily: int
    keep_weekly: int
    min_age_hours: int
    max_run_hours: int
    site: Path
    scratch: Path
    exporter: str

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> Settings:
        url = env.get("SITE_BACKUP_URL", "")
        match = URL.fullmatch(url)
        if not match or ".." in url.split("/"):
            raise BackupError(
                f"SITE_BACKUP_URL must be s3://<bucket>/<prefix>: {url!r}"
            )
        provider = env.get("SITE_BACKUP_S3_PROVIDER", "")
        if not provider:
            raise BackupError("SITE_BACKUP_S3_PROVIDER must name an rclone S3 provider")
        return cls(
            path=url.removeprefix("s3://").rstrip("/"),
            endpoint=env.get("SITE_BACKUP_S3_ENDPOINT", ""),
            region=env.get("SITE_BACKUP_S3_REGION", ""),
            provider=provider,
            keep_daily=count(env, "SITE_BACKUP_KEEP_DAILY"),
            keep_weekly=count(env, "SITE_BACKUP_KEEP_WEEKLY"),
            min_age_hours=count(env, "SITE_BACKUP_MIN_AGE_HOURS", 1),
            max_run_hours=count(env, "SITE_BACKUP_MAX_RUN_HOURS", 1),
            site=Path(env.get("SITE_BACKUP_SITE", "/mnt/site")),
            scratch=Path(env.get("SITE_BACKUP_SCRATCH", "/scratch")),
            exporter=env.get("SITE_BACKUP_EXPORTER", EXPORTER),
        )

    @property
    def lock(self) -> Path:
        return self.site / LOCK_FILE

    def remote(self, *names: str) -> str:
        return f"{REMOTE}:{'/'.join([self.path, *names])}"

    def url(self, *names: str) -> str:
        return f"s3://{'/'.join([self.path, *names])}"


def count(env: Mapping[str, str], name: str, least: int = 0) -> int:
    value = env.get(name, "")
    if not value.isdigit() or int(value) < least:
        raise BackupError(
            f"{name} must be a whole number of at least {least}, not {value!r}"
        )
    return int(value)


def say(message: str) -> None:
    print(f"{PROG}: {message}", flush=True)


def rclone_env(settings: Settings, base: Mapping[str, str]) -> dict[str, str]:
    """The environment rclone runs with: base, less its own RCLONE_*."""
    env = {k: v for k, v in base.items() if not k.startswith("RCLONE_")}
    prefix = f"RCLONE_CONFIG_{REMOTE.upper()}_"
    env["RCLONE_CONFIG"] = "/notfound"
    env[f"{prefix}TYPE"] = "s3"
    env[f"{prefix}PROVIDER"] = settings.provider
    env[f"{prefix}ENV_AUTH"] = "true"
    if settings.endpoint:
        env[f"{prefix}ENDPOINT"] = settings.endpoint
    if settings.region:
        env[f"{prefix}REGION"] = settings.region
    return env


def run(command: list[str], env: Mapping[str, str], what: str) -> str:
    """Run a command, returning its output; raise when it fails."""
    result = subprocess.run(
        command, env=dict(env), stdout=subprocess.PIPE, text=True, check=False
    )
    if result.returncode != 0:
        raise BackupError(f"{what} failed with exit status {result.returncode}")
    return result.stdout


def rclone(*args: str) -> list[str]:
    return ["rclone", args[0], *RCLONE_FLAGS, *args[1:]]


def export(settings: Settings, env: Mapping[str, str], argv: Sequence[str]) -> Path:
    """Run the export tool's backup mode; return its output directory."""
    if bad := [arg for arg in argv if not arg.startswith("--exclude=")]:
        raise BackupError(f"{bad[0]}: site-backup takes only --exclude=<path>")
    out = settings.scratch / "export"
    # The export tool needs no S3 credential
    clean = {k: v for k, v in env.items() if not k.startswith(("AWS_", "RCLONE_"))}
    git = git_store(clean, settings.site)
    command = [sys.executable, settings.exporter, "backup", *argv]
    command += ["--site", str(settings.site), "--git", str(git), "--out", str(out)]
    run(command, clean, "the site export")
    return out


def upload(settings: Settings, env: Mapping[str, str], out: Path, stamp: str) -> None:
    """Upload one backup and verify it."""
    for name in UPLOADS:
        say(f"uploading {name} to {settings.url(stamp, name)}")
        command = rclone("copyto", str(out / name))
        run([*command, settings.remote(stamp, name)], env, f"uploading {name}")
    verify(settings, env, out, stamp, UPLOADS)
    say(f"verified {settings.url(stamp)}")


def verify(
    settings: Settings,
    env: Mapping[str, str],
    out: Path,
    stamp: str,
    names: tuple[str, ...],
) -> None:
    """Check the stored files match the local ones, by size and checksum."""
    listing = settings.scratch / "uploads.txt"
    listing.write_text("".join(f"{name}\n" for name in names), encoding="utf-8")
    command = rclone("check", "--one-way", "--files-from", str(listing))
    run([*command, str(out), settings.remote(stamp)], env, "verifying the upload")


def confirm(settings: Settings, env: Mapping[str, str], out: Path, stamp: str) -> None:
    """Check, just before publishing, that the store still holds this
    run's manifest and every archive it names.
    """
    stored = read(settings, env, stamp, MANIFEST)
    if stored != (out / MANIFEST).read_text(encoding="utf-8"):
        raise BackupError(f"{settings.url(stamp, MANIFEST)} is not the one uploaded")
    try:
        names = tuple(sorted(json.loads(stored)["archives"]))
    except (ValueError, TypeError, KeyError):
        names = ()
    if not names or not set(names) <= set(UPLOADS):
        raise BackupError(f"{settings.url(stamp, MANIFEST)} names no known archives")
    verify(settings, env, out, stamp, names)
    say(f"confirmed {settings.url(stamp)} before publishing it")


def write(settings: Settings, env: Mapping[str, str], text: str, *names: str) -> None:
    """Write a small object under the backup URL."""
    source = settings.scratch / names[-1]
    source.write_text(text, encoding="utf-8")
    command = rclone("copyto", str(source), settings.remote(*names))
    run(command, env, f"writing {settings.url(*names)}")


def mark(settings: Settings, env: Mapping[str, str], stamp: str) -> None:
    """Write a backup's published marker, recording the time now."""
    text = json.dumps({"publishedAt": clock().strftime(RFC3339)}) + "\n"
    write(settings, env, text, stamp, PUBLISHED)


def publish(settings: Settings, env: Mapping[str, str], stamp: str) -> None:
    """Point latest at a verified backup, then mark it published."""
    write(settings, env, f"{stamp}\n", LATEST)
    say(f"{settings.url(LATEST)} names {stamp}")
    mark(settings, env, stamp)


def survey(settings: Settings, env: Mapping[str, str]) -> Listing:
    """List the backup prefixes, their markers, and what latest names."""
    command = rclone("lsf", "--recursive", "--max-depth", "2")
    lines = run([*command, settings.remote()], env, "listing the backups")
    found: set[str] = set()
    marked: list[str] = []
    latest = None
    for line in lines.splitlines():
        top, _, rest = line.partition("/")
        if is_stamp(top):
            found.add(top)
            if rest == PUBLISHED:
                marked.append(top)
        elif line == LATEST:
            latest = read(settings, env, LATEST).strip()
            latest = latest if is_stamp(latest) else None
    published: dict[str, datetime.datetime] = {}
    for stamp in marked:
        moment = published_at(read(settings, env, stamp, PUBLISHED))
        if moment is not None:
            published[stamp] = moment
    return Listing(found, published, latest)


def read(settings: Settings, env: Mapping[str, str], *names: str) -> str:
    """Read a small object under the backup URL."""
    command = rclone("cat", settings.remote(*names))
    return run(command, env, f"reading {settings.url(*names)}")


def reconcile(settings: Settings, env: Mapping[str, str]) -> None:
    """Mark published the backup latest names, if its run stopped short.

    Only that one: as the current backup, its marker can only lengthen
    its predecessor's window; marking another would invent a successor.
    """
    listing = survey(settings, env)
    stamp = listing.latest
    if stamp in listing.found and stamp not in listing.published:
        say(f"marking {settings.url(stamp)} published, as latest names it")
        mark(settings, env, stamp)


def prune(settings: Settings, env: Mapping[str, str]) -> None:
    """Delete the backup prefixes retain does not keep."""
    listing = survey(settings, env)
    keep = retain(
        listing,
        clock(),
        settings.keep_daily,
        settings.keep_weekly,
        settings.min_age_hours,
        settings.max_run_hours,
    )
    for old in sorted(listing.found - keep):
        say(f"deleting {settings.url(old)}")
        command = rclone("purge", settings.remote(old))
        run(command, env, f"deleting {settings.url(old)}")
    say(f"keeping {len(listing.found & keep)} backups under {settings.url()}")


def discard(settings: Settings, env: Mapping[str, str], stamp: str) -> None:
    """Delete a failed run's own prefix, best effort; prune retries it."""
    say(f"deleting the incomplete {settings.url(stamp)}")
    command = rclone("purge", settings.remote(stamp))
    subprocess.run(command, env=dict(env), stdout=subprocess.DEVNULL, check=False)


def clock() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def run_id() -> str:
    return secrets.token_hex(4)


def site_state(site: Path) -> bytes | None:
    """The site state file's content, or None without one."""
    try:
        return (site / STATE_FILE).read_bytes()
    except FileNotFoundError:
        return None


def complete(state: bytes | None) -> str | None:
    """None when state records a complete site; otherwise why not."""
    if state is None:
        return "has no state file"
    try:
        recorded = json.loads(state).get("state")
    except (ValueError, AttributeError):
        return "holds no valid state"
    return None if recorded == "complete" else f"records state {recorded!r}"


def main(env: Mapping[str, str], argv: Sequence[str] = ()) -> int:
    lock = None
    try:
        settings = Settings.from_env(env)
        try:
            lock = hold_lock(settings.lock)
        except LockMissing as missing:
            say(
                f"skipping this run: {missing}, as the site is not initialised "
                "under this chart version yet; the next run retries"
            )
            return 0
        if lock is None:
            say(f"skipping this run: another backup holds {settings.lock}")
            return 0
        state_path = settings.site / STATE_FILE
        before = site_state(settings.site)
        why = complete(before)
        if why:
            say(f"skipping this run: {state_path} {why}; the next run retries")
            return 0
        stamp = f"{clock().strftime(STAMP_FORMAT)}-{run_id()}"
        say(f"backing up {settings.site} to {settings.url(stamp)}")
        out = export(settings, env, argv)
        if site_state(settings.site) != before:
            say(
                f"discarding the export: {state_path} changed while it ran, "
                "as an import started; the next run retries"
            )
            shutil.rmtree(out, ignore_errors=True)
            return 0
        remote_env = rclone_env(settings, env)
        try:
            upload(settings, remote_env, out, stamp)
            reconcile(settings, remote_env)
            confirm(settings, remote_env, out, stamp)
        except BackupError:
            discard(settings, remote_env, stamp)
            raise
        publish(settings, remote_env, stamp)
        prune(settings, remote_env)
    except (BackupError, VolumeError, OSError) as exc:
        print(f"{PROG}: error: {exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        if lock is not None:
            os.close(lock)
    say("done")
    return 0


if __name__ == "__main__":
    sys.exit(main(os.environ, sys.argv[1:]))
