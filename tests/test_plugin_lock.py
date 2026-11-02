# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for the format check in scripts/check-plugin-lock.sh."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "scripts" / "check-plugin-lock.sh"
LOCK = ROOT / "docker" / "plugins.lock"

CI = "https://gerrit-ci.gerritforge.com/job"
DIGEST = "a" * 64
HEALTHCHECK = (
    f"plugins/healthcheck {CI}/plugin-healthcheck-bazel-stable-3.13/1"
    f"/artifact/bazel-bin/plugins/healthcheck/healthcheck.jar {DIGEST}"
)
SAML = (
    f"lib/saml {CI}/plugin-saml-bazel-stable-3.13/1"
    f"/artifact/bazel-bin/plugins/saml/saml.jar {'b' * 64}"
)

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="the checker is a bash script"
)


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def check(tmp_path: Path, *lines: str) -> subprocess.CompletedProcess[str]:
    lock = tmp_path / "plugins.lock"
    lock.write_text("# A comment\n\n" + "\n".join(lines) + "\n")
    return run("--lock", str(lock))


@pytest.mark.xfail(strict=True, reason="check-plugin-lock.sh not added yet")
def test_repository_lock_is_valid() -> None:
    result = run("--lock", str(LOCK))
    assert result.returncode == 0, result.stderr


@pytest.mark.xfail(strict=True, reason="check-plugin-lock.sh not added yet")
def test_repository_lock_names_every_catalogue_jar() -> None:
    entries = {
        line.split()[0]
        for line in LOCK.read_text().splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert entries == {
        "lib/saml",
        "plugins/its-jira",
        "plugins/events-log",
        "plugins/analytics",
        "plugins/reviewers",
        "plugins/lfs",
        "plugins/healthcheck",
        "plugins/metrics-reporter-prometheus",
    }


@pytest.mark.xfail(strict=True, reason="check-plugin-lock.sh not added yet")
def test_valid_entries_pass(tmp_path: Path) -> None:
    result = check(tmp_path, HEALTHCHECK, SAML)
    assert result.returncode == 0, result.stderr


@pytest.mark.xfail(strict=True, reason="check-plugin-lock.sh not added yet")
@pytest.mark.parametrize(
    ("line", "message"),
    [
        (
            HEALTHCHECK.replace("/1/", "/lastSuccessfulBuild/"),
            "pinned build number",
        ),
        (HEALTHCHECK.replace(DIGEST, "abc"), "sha256"),
        (HEALTHCHECK.replace("plugins/healthcheck ", "opt/healthcheck "), "plugins/"),
        (HEALTHCHECK.replace("/healthcheck.jar", "/other.jar"), "healthcheck.jar"),
        (
            HEALTHCHECK.replace("https://gerrit-ci.gerritforge.com", "http://x"),
            "gerrit-ci.gerritforge.com",
        ),
        (HEALTHCHECK + " extra", "three fields"),
    ],
    ids=["moving-build", "bad-digest", "bad-dir", "jar-name", "host", "fields"],
)
def test_malformed_entry_fails(tmp_path: Path, line: str, message: str) -> None:
    result = check(tmp_path, line)
    assert result.returncode == 1
    assert message in result.stderr


@pytest.mark.xfail(strict=True, reason="check-plugin-lock.sh not added yet")
def test_duplicate_name_fails(tmp_path: Path) -> None:
    result = check(tmp_path, HEALTHCHECK, HEALTHCHECK.replace("plugins/", "lib/", 1))
    assert result.returncode == 1
    assert "more than once" in result.stderr


@pytest.mark.xfail(strict=True, reason="check-plugin-lock.sh not added yet")
def test_empty_lock_fails(tmp_path: Path) -> None:
    result = check(tmp_path)
    assert result.returncode == 1
    assert "no entries" in result.stderr


@pytest.mark.xfail(strict=True, reason="check-plugin-lock.sh not added yet")
def test_missing_lock_is_an_error(tmp_path: Path) -> None:
    result = run("--lock", str(tmp_path / "absent.lock"))
    assert result.returncode == 2
