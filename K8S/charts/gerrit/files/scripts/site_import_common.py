# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Settings and helpers shared by the site-import modules.

``site-import.py`` is the entry point; see its docstring.
"""

from __future__ import annotations

import hashlib
import io
import math
import shutil
import subprocess
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROG = "site-import"
MODES = ("auto", "force", "skip")
SOURCES = ("none", "pvc", "https")
VERSION_JAR = "WEB-INF/lib/com_google_gerrit_common_libversion.jar"
VERSION_RESOURCE = "com/google/gerrit/common/Version"


class SiteImportError(Exception):
    """A failure that stops the import; the message is safe to log."""


def say(message: str) -> None:
    print(f"{PROG}: {message}", flush=True)


def warn(message: str) -> None:
    print(f"{PROG}: warning: {message}", flush=True)


def flag(value: str) -> bool:
    if value.strip().lower() in ("true", "1", "yes"):
        return True
    if value.strip().lower() in ("", "false", "0", "no"):
        return False
    raise SiteImportError(f"not a boolean: {value!r}")


def cpu_threads(value: str) -> int | None:
    """Threads for a Kubernetes CPU quantity, rounded up; None when unset."""
    value = value.strip()
    if not value:
        return None
    try:
        cores = int(value[:-1]) / 1000 if value.endswith("m") else float(value)
    except ValueError:
        cores = 0
    if not math.isfinite(cores) or cores <= 0:
        raise SiteImportError(f"not a CPU limit: {value!r}")
    return max(1, math.ceil(cores))


@dataclass
class Settings:
    """What the chart asked for, from the environment."""

    site: Path
    rendered: Path
    image_bin: Path
    scratch: Path
    site_init: Path
    tools_bin: Path = Path("/opt/gerrit-tools/bin")
    mode: str = "auto"
    token: str = ""
    source: str = "none"
    url: str = ""
    urls: Path | None = None
    pin: str = ""
    reindex: bool = True
    verify: bool = True
    saml_keystore: Path | None = None
    secure_keys: list[str] = field(default_factory=list)
    java: str = "java"
    java_heap: list[str] = field(default_factory=list)
    reindex_threads: int | None = None

    @classmethod
    def from_env(cls, env: dict[str, str]) -> Settings:
        def path(name: str, default: str = "") -> Path:
            value = env.get(name, default)
            if not value:
                raise SiteImportError(f"{name} is not set")
            return Path(value)

        def optional(name: str) -> Path | None:
            value = env.get(name, "")
            return Path(value) if value else None

        settings = cls(
            site=path("GERRIT_SITE"),
            rendered=path("GERRIT_RENDERED_CONFIG"),
            image_bin=path("GERRIT_IMAGE_BIN", "/var/gerrit/bin"),
            scratch=path("SITE_IMPORT_SCRATCH"),
            site_init=path("SITE_INIT_SCRIPT", "/scripts/site-init.sh"),
            tools_bin=path("GERRIT_TOOLS_BIN", "/opt/gerrit-tools/bin"),
            mode=env.get("SITE_IMPORT_MODE", "auto"),
            token=env.get("SITE_IMPORT_FORCE_TOKEN", ""),
            source=env.get("SITE_IMPORT_SOURCE_TYPE", "none"),
            url=env.get("SITE_IMPORT_SOURCE_URL", ""),
            urls=optional("SITE_IMPORT_URLS"),
            pin=env.get("SITE_IMPORT_EXPECTED_MANIFEST_SHA256", "").strip().lower(),
            reindex=flag(env.get("SITE_IMPORT_REINDEX", "true")),
            verify=flag(env.get("SITE_IMPORT_VERIFY", "true")),
            saml_keystore=optional("SITE_IMPORT_SAML_KEYSTORE"),
            secure_keys=env.get("SITE_IMPORT_SECURE_KEYS", "").split(),
            java=env.get("SITE_IMPORT_JAVA", "java"),
            java_heap=env.get("SITE_IMPORT_JAVA_HEAP", "").split(),
            reindex_threads=cpu_threads(env.get("SITE_IMPORT_CPU_LIMIT", "")),
        )
        if settings.mode not in MODES:
            raise SiteImportError(
                f"siteImport.mode {settings.mode!r} is not one of {', '.join(MODES)}"
            )
        if settings.source not in SOURCES:
            raise SiteImportError(
                f"siteImport.source.type {settings.source!r} is not one of {', '.join(SOURCES)}"
            )
        if settings.source != "none" and not (settings.url or settings.urls):
            raise SiteImportError(
                f"siteImport.source.type {settings.source} needs a url"
            )
        return settings

    @property
    def source_label(self) -> str:
        return "none" if self.source == "none" else f"{self.source}:{self.url}"


def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, text=True, check=False, **kwargs)
    except FileNotFoundError as exc:
        raise SiteImportError(f"required tool not found: {command[0]}") from exc


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return run(["git", "-c", "safe.directory=*", *args], capture_output=True)


def config_get(path: Path, key: str) -> str:
    """Return a key from a git config file, or "" when it is absent."""
    if not path.is_file():
        raise SiteImportError(f"cannot read {path}")
    result = git("config", "-f", str(path), "--get", key)
    if result.returncode > 1:
        raise SiteImportError(f"cannot read {key} from {path}")
    return result.stdout.strip()


def config_names(path: Path) -> list[str]:
    """Return the names in a git config file, never its values."""
    if not path.is_file():
        return []
    result = git("config", "-f", str(path), "--list", "--name-only")
    if result.returncode:
        raise SiteImportError(f"cannot parse {path}")
    return sorted(set(result.stdout.split()))


def normalise_name(name: str) -> str:
    """Lower-case a config name's section and key, as git reports them."""
    section, _, rest = name.partition(".")
    middle, _, key = rest.rpartition(".")
    if not middle:
        return f"{section.lower()}.{key.lower()}"
    return f"{section.lower()}.{middle}.{key.lower()}"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError as exc:
        raise SiteImportError(f"cannot read {path.name}: {exc.strerror}") from exc
    return digest.hexdigest()


def empty_dir(path: Path, keep: tuple[str, ...] = ()) -> None:
    """Remove everything in a directory but the names in keep."""
    path.mkdir(parents=True, exist_ok=True)
    for entry in path.iterdir():
        if entry.name in keep:
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()


def gerrit_version(war: Path) -> str | None:
    """Return the Gerrit release a gerrit.war carries, if it names one.

    A release war records it in the Version resource of a nested jar;
    the war's own manifest may carry Implementation-Version instead.
    """
    try:
        with zipfile.ZipFile(war) as archive:
            text = archive.read("META-INF/MANIFEST.MF").decode("utf-8", "replace")
            for line in text.splitlines():
                key, _, value = line.partition(":")
                if key.strip() == "Implementation-Version" and value.strip():
                    return value.strip()
            with (
                archive.open(VERSION_JAR) as nested,
                zipfile.ZipFile(io.BytesIO(nested.read())) as jar,
            ):
                version = jar.read(VERSION_RESOURCE).decode("utf-8", "replace")
    except (OSError, KeyError, zipfile.BadZipFile):
        return None
    return version.strip().removeprefix("v") or None
