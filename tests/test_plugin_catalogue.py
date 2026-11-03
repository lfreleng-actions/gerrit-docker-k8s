# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for scripts/plugin-catalogue.sh, the plugin catalogue mirror.

The mirror's tag is the sha256 of docker/plugins.lock, so every
consumer that computes it must agree with the workflow that pushes it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "scripts" / "plugin-catalogue.sh"
LOCK = ROOT / "docker" / "plugins.lock"

CI = "https://gerrit-ci.gerritforge.com/job"
HEALTHCHECK_JAR = b"healthcheck jar"
SAML_JAR = b"saml jar"
HEALTHCHECK_URL = (
    f"{CI}/plugin-healthcheck-bazel-stable-3.13/1"
    "/artifact/bazel-bin/plugins/healthcheck/healthcheck.jar"
)
SAML_URL = (
    f"{CI}/plugin-saml-bazel-stable-3.13/1/artifact/bazel-bin/plugins/saml/saml.jar"
)
# An OCI distribution-spec tag
OCI_TAG_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")

# Stands in for curl: copies the fixture named by the URL's last
# component, so no test reaches the network
CURL_STUB = """#!/usr/bin/env bash
set -euo pipefail
out=""
while [[ $# -gt 1 ]]; do
    [[ "$1" == "-o" ]] && out="$2"
    shift
done
cp "${FIXTURES}/${1##*/}" "$out"
"""
# Stands in for jq: fails the call that annotates a jar, and runs the
# real jq for every other call
JQ_STUB = """#!/usr/bin/env bash
prev=""
for arg in "$@"; do
    if [[ "$prev" == "--arg" && "$arg" == "p" ]]; then
        exit 5
    fi
    prev="$arg"
done
exec "${REAL_JQ}" "$@"
"""

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None,
    reason="the script needs bash and jq",
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run(
    *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def write_lock(path: Path, healthcheck_digest: str | None = None) -> Path:
    path.write_text(
        "# A comment\n\n"
        f"plugins/healthcheck {HEALTHCHECK_URL} "
        f"{healthcheck_digest or sha256(HEALTHCHECK_JAR)}\n"
        f"lib/saml {SAML_URL} {sha256(SAML_JAR)}\n"
    )
    return path


@pytest.fixture
def stub_env(tmp_path: Path) -> dict[str, str]:
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "healthcheck.jar").write_bytes(HEALTHCHECK_JAR)
    (fixtures / "saml.jar").write_bytes(SAML_JAR)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    curl = bindir / "curl"
    curl.write_text(CURL_STUB)
    curl.chmod(0o755)
    return {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "FIXTURES": str(fixtures),
    }


def test_tag_is_the_sha256_of_the_lock_bytes(tmp_path: Path) -> None:
    lock = write_lock(tmp_path / "plugins.lock")
    result = run("--lock", str(lock), "tag")
    assert result.returncode == 0, result.stderr
    assert result.stdout == sha256(lock.read_bytes()) + "\n"


def test_tag_of_the_repository_lock() -> None:
    result = run("tag")
    assert result.returncode == 0, result.stderr
    tag = result.stdout.strip()
    assert tag == sha256(LOCK.read_bytes())
    assert OCI_TAG_RE.fullmatch(tag)


def test_tag_changes_with_any_byte_of_the_lock(tmp_path: Path) -> None:
    lock = write_lock(tmp_path / "plugins.lock")
    before = run("--lock", str(lock), "tag").stdout
    lock.write_text(lock.read_text() + "# another comment\n")
    after = run("--lock", str(lock), "tag").stdout
    assert before != after


def test_tag_needs_the_lock(tmp_path: Path) -> None:
    result = run("--lock", str(tmp_path / "absent.lock"), "tag")
    assert result.returncode == 2


def test_stage_lays_out_the_artifact(tmp_path: Path, stub_env: dict[str, str]) -> None:
    lock = write_lock(tmp_path / "plugins.lock")
    out = tmp_path / "out"
    result = run(
        "--lock",
        str(lock),
        "--annotation",
        "org.opencontainers.image.revision=abc",
        "stage",
        str(out),
        env=stub_env,
    )
    assert result.returncode == 0, result.stderr
    assert (out / "plugins" / "healthcheck.jar").read_bytes() == HEALTHCHECK_JAR
    assert (out / "lib" / "saml.jar").read_bytes() == SAML_JAR
    assert (out / "plugins.lock").read_bytes() == lock.read_bytes()
    assert (out / "files.txt").read_text().splitlines() == [
        "plugins/healthcheck.jar:application/java-archive",
        "lib/saml.jar:application/java-archive",
        "plugins.lock:text/plain",
    ]
    annotations = json.loads((out / "annotations.json").read_text())
    assert annotations["$manifest"] == {
        "org.linuxfoundation.releng.plugin-lock.sha256": sha256(lock.read_bytes()),
        "org.opencontainers.image.revision": "abc",
    }
    assert annotations["plugins/healthcheck.jar"] == {
        "org.linuxfoundation.releng.plugin-lock.entry": "plugins/healthcheck",
        "org.linuxfoundation.releng.plugin-lock.url": HEALTHCHECK_URL,
    }
    assert annotations["lib/saml.jar"] == {
        "org.linuxfoundation.releng.plugin-lock.entry": "lib/saml",
        "org.linuxfoundation.releng.plugin-lock.url": SAML_URL,
    }


def test_stage_rejects_a_digest_mismatch(
    tmp_path: Path, stub_env: dict[str, str]
) -> None:
    lock = write_lock(tmp_path / "plugins.lock", healthcheck_digest="c" * 64)
    out = tmp_path / "out"
    result = run("--lock", str(lock), "stage", str(out), env=stub_env)
    assert result.returncode == 1
    assert "sha256 mismatch" in result.stderr
    assert not (out / "annotations.json").exists()


def test_stage_rejects_a_malformed_lock(
    tmp_path: Path, stub_env: dict[str, str]
) -> None:
    lock = tmp_path / "plugins.lock"
    lock.write_text(f"plugins/healthcheck {HEALTHCHECK_URL} abc\n")
    result = run("--lock", str(lock), "stage", str(tmp_path / "out"), env=stub_env)
    assert result.returncode == 1


def test_stage_refuses_a_non_empty_directory(
    tmp_path: Path, stub_env: dict[str, str]
) -> None:
    lock = write_lock(tmp_path / "plugins.lock")
    out = tmp_path / "out"
    out.mkdir()
    (out / "stale").write_text("")
    result = run("--lock", str(lock), "stage", str(out), env=stub_env)
    assert result.returncode == 2
    assert "not empty" in result.stderr


def test_stage_fails_when_a_staging_command_fails(
    tmp_path: Path, stub_env: dict[str, str]
) -> None:
    real_jq = shutil.which("jq")
    assert real_jq is not None
    jq = tmp_path / "bin" / "jq"
    jq.write_text(JQ_STUB)
    jq.chmod(0o755)
    env = {**stub_env, "REAL_JQ": real_jq}
    lock = write_lock(tmp_path / "plugins.lock")
    out = tmp_path / "out"
    result = run("--lock", str(lock), "stage", str(out), env=env)
    # The download ran, so the failure came from the jq call after it
    assert (out / "plugins" / "healthcheck.jar").read_bytes() == HEALTHCHECK_JAR
    assert result.returncode != 0
    assert not (out / "annotations.json").exists()
