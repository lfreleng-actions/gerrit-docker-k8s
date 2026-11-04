# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Fetch export files from S3 for site-import's s3 source.

``siteImport.source.url`` is ``s3://<bucket>/<prefix>``; each export
file is the object ``<prefix>/<name>``. rclone fetches it, configured
only through environment variables as the remote ``siteimport``:
``env_auth`` reads the credentials the chart provides, either
``AWS_ACCESS_KEY_ID`` and ``AWS_SECRET_ACCESS_KEY`` from a Secret or
the web identity token IRSA injects. No credential reaches rclone's
command line or the log. ``site-import.py`` is the entry point; see
its docstring.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from site_import_common import Settings, SiteImportError, run, say

REMOTE = "siteimport"
# rclone's exit status when --error-on-no-transfer finds nothing to copy
NO_TRANSFER = 9
RCLONE_FLAGS = [
    "--error-on-no-transfer",
    "--stats",
    "0",
    "--retries",
    "5",
    "--retries-sleep",
    "3s",
    "--low-level-retries",
    "10",
]


def bucket_path(settings: Settings) -> str:
    """The bucket and prefix of the source URL, without a trailing slash."""
    return settings.url.removeprefix("s3://").rstrip("/")


def remote_path(settings: Settings, name: str) -> str:
    return f"{REMOTE}:{bucket_path(settings)}/{name}"


def object_url(settings: Settings, name: str) -> str:
    return f"s3://{bucket_path(settings)}/{name}"


def rclone_command(settings: Settings, name: str, dest: Path) -> list[str]:
    return ["rclone", "copyto", *RCLONE_FLAGS, remote_path(settings, name), str(dest)]


def rclone_env(settings: Settings, base: Mapping[str, str]) -> dict[str, str]:
    """The fetch's environment: base without rclone settings, plus the remote.

    RCLONE_CONFIG=/notfound keeps rclone from reading or wanting a
    config file; inherited RCLONE_* settings are dropped so only the
    chart's options shape the fetch.
    """
    env = {k: v for k, v in base.items() if not k.startswith("RCLONE_")}
    prefix = f"RCLONE_CONFIG_{REMOTE.upper()}_"
    env["RCLONE_CONFIG"] = "/notfound"
    env[f"{prefix}TYPE"] = "s3"
    env[f"{prefix}PROVIDER"] = settings.s3_provider
    env[f"{prefix}ENV_AUTH"] = "true"
    if settings.s3_endpoint:
        env[f"{prefix}ENDPOINT"] = settings.s3_endpoint
    if settings.s3_region:
        env[f"{prefix}REGION"] = settings.s3_region
    return env


def fetch_s3(settings: Settings, name: str, env: Mapping[str, str]) -> Path:
    """Download one export file to scratch and return its path."""
    url = object_url(settings, name)
    dest = settings.scratch / "download" / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.unlink(missing_ok=True)
    say(f"downloading {name} from {url}")
    result = run(rclone_command(settings, name, dest), env=rclone_env(settings, env))
    if result.returncode == NO_TRANSFER:
        raise SiteImportError(f"{url} does not exist")
    if result.returncode:
        raise SiteImportError(
            f"downloading {url} failed with rclone exit status {result.returncode}"
        )
    if not dest.is_file():
        raise SiteImportError(f"rclone wrote nothing for {url}")
    return dest
