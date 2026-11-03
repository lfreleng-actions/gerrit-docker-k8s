# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for scripts/fetch-plugin-catalogue.sh, the image build's fetch.

oras, cosign and curl are stubs: each records its arguments, so a test
can tell which source the script used without reaching the network.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "scripts" / "fetch-plugin-catalogue.sh"

HEALTHCHECK_JAR = b"healthcheck jar"
SAML_JAR = b"saml jar"
UPSTREAM = "https://upstream.invalid/job"
HEALTHCHECK_URL = f"{UPSTREAM}/healthcheck/1/healthcheck.jar"
SAML_URL = f"{UPSTREAM}/saml/1/saml.jar"
DIGEST = "sha256:" + "d" * 64
MIRROR = "ghcr.io/lfreleng-actions/gerrit-docker-k8s/plugin-catalogue"
IDENTITY = (
    "https://github.com/lfreleng-actions/gerrit-docker-k8s"
    "/.github/workflows/plugin-catalogue.yaml@refs/heads/main"
)
ISSUER = "https://token.actions.githubusercontent.com"

# resolve: ORAS_MODE picks a present tag, an absent one, or an error.
# pull: copies MIRROR_DIR into the -o directory.
ORAS_STUB = """#!/usr/bin/env bash
echo "oras $*" >>"${CALLS}"
case "$1" in
    resolve)
        case "${ORAS_MODE}" in
            present) echo "${DIGEST}" ;;
            absent) echo "Error: ${2}: not found" >&2; exit 1 ;;
            *) echo "Error: connection refused" >&2; exit 1 ;;
        esac
        ;;
    pull)
        out=""
        while [[ $# -gt 0 ]]; do
            [[ "$1" == "-o" ]] && out="$2"
            shift
        done
        cp -R "${MIRROR_DIR}/." "$out"
        ;;
    *) exit 3 ;;
esac
"""
COSIGN_STUB = """#!/usr/bin/env bash
echo "cosign $*" >>"${CALLS}"
exit "${COSIGN_RC:-0}"
"""
# Copies the fixture named by the URL's last component
CURL_STUB = """#!/usr/bin/env bash
echo "curl $*" >>"${CALLS}"
out=""
while [[ $# -gt 1 ]]; do
    [[ "$1" == "-o" ]] && out="$2"
    shift
done
[[ -f "${FIXTURES}/${1##*/}" ]] || exit 22
cp "${FIXTURES}/${1##*/}" "$out"
"""

pytestmark = [
    pytest.mark.skipif(
        any(shutil.which(tool) is None for tool in ("bash", "sh", "sha256sum")),
        reason="the fetch needs bash, sh and sha256sum",
    ),
    pytest.mark.xfail(strict=True, reason="fetch-plugin-catalogue.sh not added"),
]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Env:
    """Stubbed tools, fixtures and a lock in a temporary directory."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.lock = tmp_path / "plugins.lock"
        self.lock.write_text(
            "# A comment\n\n"
            f"plugins/healthcheck {HEALTHCHECK_URL} {sha256(HEALTHCHECK_JAR)}\n"
            f"lib/saml {SAML_URL} {sha256(SAML_JAR)}\n"
        )
        self.calls = tmp_path / "calls.txt"
        self.calls.write_text("")
        self.fixtures = tmp_path / "fixtures"
        self.fixtures.mkdir()
        (self.fixtures / "healthcheck.jar").write_bytes(HEALTHCHECK_JAR)
        (self.fixtures / "saml.jar").write_bytes(SAML_JAR)
        self.mirror = tmp_path / "mirror"
        (self.mirror / "plugins").mkdir(parents=True)
        (self.mirror / "lib").mkdir()
        (self.mirror / "plugins" / "healthcheck.jar").write_bytes(HEALTHCHECK_JAR)
        (self.mirror / "lib" / "saml.jar").write_bytes(SAML_JAR)
        shutil.copy(self.lock, self.mirror / "plugins.lock")
        bindir = tmp_path / "bin"
        bindir.mkdir()
        for name, body in (
            ("oras", ORAS_STUB),
            ("cosign", COSIGN_STUB),
            ("curl", CURL_STUB),
        ):
            stub = bindir / name
            stub.write_text(body)
            stub.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
            "CALLS": str(self.calls),
            "DIGEST": DIGEST,
            "FIXTURES": str(self.fixtures),
            "MIRROR_DIR": str(self.mirror),
            "ORAS_MODE": "present",
        }
        self.dest = tmp_path / "catalogue"

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(SCRIPT), "--lock", str(self.lock), *args, str(self.dest)],
            capture_output=True,
            text=True,
            check=False,
            env=self.env,
            timeout=60,
        )

    def called(self, tool: str) -> list[str]:
        return [
            line
            for line in self.calls.read_text().splitlines()
            if line.startswith(f"{tool} ")
        ]

    def jars(self) -> list[str]:
        if not self.dest.exists():
            return []
        return sorted(
            str(p.relative_to(self.dest)) for p in self.dest.rglob("*") if p.is_file()
        )


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def test_mirror_present_takes_the_mirror(env: Env) -> None:
    result = env.run()
    assert result.returncode == 0, result.stderr
    assert env.jars() == ["lib/saml.jar", "plugins/healthcheck.jar"]
    assert (env.dest / "plugins" / "healthcheck.jar").read_bytes() == HEALTHCHECK_JAR
    assert (env.dest / "lib" / "saml.jar").read_bytes() == SAML_JAR
    assert "Plugin catalogue source: mirror" in result.stdout + result.stderr
    assert env.called("curl") == []
    tag = sha256(env.lock.read_bytes())
    assert env.called("oras")[0] == f"oras resolve {MIRROR}:{tag}"
    assert any(
        call.startswith(f"oras pull {MIRROR}@{DIGEST} ") for call in env.called("oras")
    )


def test_mirror_is_verified_with_the_exact_identity(env: Env) -> None:
    result = env.run()
    assert result.returncode == 0, result.stderr
    assert env.called("cosign") == [
        f"cosign verify {MIRROR}@{DIGEST} --certificate-identity {IDENTITY} "
        f"--certificate-oidc-issuer {ISSUER}"
    ]


def test_mirror_is_verified_before_it_is_pulled(env: Env) -> None:
    env.run()
    calls = env.calls.read_text().splitlines()
    verify = next(i for i, c in enumerate(calls) if c.startswith("cosign verify"))
    pull = next(i for i, c in enumerate(calls) if c.startswith("oras pull"))
    assert verify < pull


def test_repository_option_names_mirror_and_identity(env: Env) -> None:
    result = env.run("--repository", "example/fork")
    assert result.returncode == 0, result.stderr
    assert env.called("oras")[0].startswith(
        "oras resolve ghcr.io/example/fork/plugin-catalogue:"
    )
    assert (
        "--certificate-identity https://github.com/example/fork"
        "/.github/workflows/plugin-catalogue.yaml@refs/heads/main"
    ) in env.called("cosign")[0]


def test_failed_verification_fails(env: Env) -> None:
    env.env["COSIGN_RC"] = "1"
    result = env.run()
    assert result.returncode != 0
    assert "verif" in result.stderr.lower()
    assert env.jars() == []
    assert env.called("curl") == []


def test_mirror_digest_mismatch_fails(env: Env) -> None:
    (env.mirror / "lib" / "saml.jar").write_bytes(b"tampered")
    result = env.run()
    assert result.returncode != 0
    assert "sha256 mismatch for lib/saml" in result.stderr
    assert env.called("curl") == []


def test_mirror_missing_an_entry_fails(env: Env) -> None:
    (env.mirror / "plugins" / "healthcheck.jar").unlink()
    result = env.run()
    assert result.returncode != 0
    assert "plugins/healthcheck" in result.stderr
    assert env.called("curl") == []


def test_mirror_holding_another_lock_fails(env: Env) -> None:
    (env.mirror / "plugins.lock").write_text("# another lock\n")
    result = env.run()
    assert result.returncode != 0
    assert "plugins.lock" in result.stderr


def test_absent_tag_falls_back_to_upstream(env: Env) -> None:
    env.env["ORAS_MODE"] = "absent"
    result = env.run()
    assert result.returncode == 0, result.stderr
    output = result.stdout + result.stderr
    assert "Plugin catalogue source: upstream" in output
    assert "not mirrored" in output
    assert env.jars() == ["lib/saml.jar", "plugins/healthcheck.jar"]
    assert env.called("cosign") == []
    urls = [call.split()[-1] for call in env.called("curl")]
    assert urls == [HEALTHCHECK_URL, SAML_URL]
    # The upstream fetch is docker/fetch-plugins.sh, with its retries
    assert all("--retry-connrefused" in call for call in env.called("curl"))


def test_upstream_digest_mismatch_fails(env: Env) -> None:
    env.env["ORAS_MODE"] = "absent"
    (env.fixtures / "saml.jar").write_bytes(b"tampered")
    result = env.run()
    assert result.returncode != 0
    assert "sha256 mismatch for lib/saml" in result.stderr


def test_registry_error_fails_without_falling_back(env: Env) -> None:
    env.env["ORAS_MODE"] = "error"
    result = env.run()
    assert result.returncode != 0
    assert "connection refused" in result.stderr
    assert env.called("curl") == []
    assert env.jars() == []


def test_refuses_a_non_empty_destination(env: Env) -> None:
    env.dest.mkdir()
    (env.dest / "stale").write_text("")
    result = env.run()
    assert result.returncode == 2
    assert "not empty" in result.stderr
    assert env.called("oras") == []
