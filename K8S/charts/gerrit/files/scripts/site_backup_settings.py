# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Settings of the chart's backup CronJob, ``site-backup.py``.

rclone, as for site-import's s3 source, is configured only through
environment variables, with ``env_auth`` reading the AWS credentials
or the web identity token from the container's environment; no
credential reaches a command line or the log. Configuration, besides
``site-backup.py``'s ``--exclude=<path>`` arguments, all from the
environment:

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

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

REMOTE = "sitebackup"
LOCK_FILE = Path(".chart") / "backup-lock" / "lock"
URL = re.compile(r"s3://([^/]+)(/.*)?")
EXPORTER = "/opt/gerrit-site-export/gerrit-site-export.py"


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


def rclone_env(settings: Settings, base: Mapping[str, str]) -> dict[str, str]:
    """The environment rclone runs with: base, less its own RCLONE_*."""
    env = {k: v for k, v in base.items() if not k.startswith("RCLONE_")}
    prefix = f"RCLONE_CONFIG_{REMOTE.upper()}_"
    env["RCLONE_CONFIG"] = "/notfound"
    env[f"{prefix}TYPE"] = "s3"
    env[f"{prefix}PROVIDER"] = settings.provider
    env[f"{prefix}ENV_AUTH"] = "true"
    # The operator provisions the bucket; never check for or create it
    env[f"{prefix}NO_CHECK_BUCKET"] = "true"
    if settings.endpoint:
        env[f"{prefix}ENDPOINT"] = settings.endpoint
    if settings.region:
        env[f"{prefix}REGION"] = settings.region
    return env
