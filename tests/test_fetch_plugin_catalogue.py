# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for scripts/fetch-plugin-catalogue.sh, the chart image's fetch.

oras, cosign and curl are stubs that log every call, so the tests
assert which source the script used without reaching the network.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "scripts" / "fetch-plugin-catalogue.sh"

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
DIGEST = "sha256:" + "d" * 64
REPOSITORY = "example-org/example-repo"
IDENTITY = (
    f"https://github.com/{REPOSITORY}"
    "/.github/workflows/plugin-catalogue.yaml@refs/heads/main"
)
ISSUER = "https://token.actions.githubusercontent.com"

# ORAS_RESOLVE picks the registry's answer: found, missing, error, or a
# failure that says "not found" about something other than the tag:
# helper (a missing credential helper) or other-ref.
# A pull copies MIRROR into the -o directory.
ORAS_STUB = """#!/usr/bin/env bash
echo "oras $*" >> "${CALLS}"
case "$1" in
    resolve)
        case "${ORAS_RESOLVE}" in
            found) echo "${ORAS_DIGEST}" ;;
            missing) echo "Error: failed to resolve digest: ${2}: not found" >&2; exit 1 ;;
            helper)
                echo 'Error: failed to resolve digest: error getting credentials - err: exec: "docker-credential-desktop": executable file not found in $PATH, out: ``' >&2
                exit 1
                ;;
            other-ref) echo "Error: failed to resolve digest: ${2}-other: not found" >&2; exit 1 ;;
            *) echo "Error: unauthorized" >&2; exit 1 ;;
        esac
        ;;
    pull)
        out=""
        while [[ $# -gt 0 ]]; do
            [[ "$1" == "-o" ]] && out="$2"
            shift
        done
        cp -R "${MIRROR}/." "$out/"
        ;;
    *) exit 3 ;;
esac
"""
COSIGN_STUB = """#!/usr/bin/env bash
echo "cosign $*" >> "${CALLS}"
exit "${COSIGN_RC:-0}"
"""
# Stands in for curl in fetch-plugins.sh: copies the fixture named by
# the URL's last component and reports HTTP 200
CURL_STUB = """#!/usr/bin/env bash
echo "curl $*" >> "${CALLS}"
out=""
while [[ $# -gt 1 ]]; do
    [[ "$1" == "-o" ]] && out="$2"
    shift
done
cp "${FIXTURES}/${1##*/}" "$out"
printf 200
"""

pytestmark = pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in ("bash", "sh", "sha256sum")),
    reason="the script needs bash, sh and sha256sum",
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_lock(path: Path) -> Path:
    path.write_text(
        "# A comment\n\n"
        f"plugins/healthcheck {HEALTHCHECK_URL} {sha256(HEALTHCHECK_JAR)}\n"
        f"lib/saml {SAML_URL} {sha256(SAML_JAR)}\n"
    )
    return path


def write_stub(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


@pytest.fixture
def lock(tmp_path: Path) -> Path:
    return write_lock(tmp_path / "plugins.lock")


@pytest.fixture
def mirror(tmp_path: Path, lock: Path) -> Path:
    """The artifact as oras pull lays it out."""
    mirror = tmp_path / "mirror"
    (mirror / "plugins").mkdir(parents=True)
    (mirror / "lib").mkdir()
    (mirror / "plugins" / "healthcheck.jar").write_bytes(HEALTHCHECK_JAR)
    (mirror / "lib" / "saml.jar").write_bytes(SAML_JAR)
    shutil.copy(lock, mirror / "plugins.lock")
    return mirror


@pytest.fixture
def env(tmp_path: Path, mirror: Path) -> dict[str, str]:
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "healthcheck.jar").write_bytes(HEALTHCHECK_JAR)
    (fixtures / "saml.jar").write_bytes(SAML_JAR)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    write_stub(bindir / "oras", ORAS_STUB)
    write_stub(bindir / "cosign", COSIGN_STUB)
    write_stub(bindir / "curl", CURL_STUB)
    calls = tmp_path / "calls.log"
    calls.touch()
    return {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "CALLS": str(calls),
        "FIXTURES": str(fixtures),
        "MIRROR": str(mirror),
        "ORAS_RESOLVE": "found",
        "ORAS_DIGEST": DIGEST,
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_REPOSITORY": REPOSITORY,
    }


def run(
    env: dict[str, str], lock: Path, dest: Path, *args: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), "--lock", str(lock), *args, str(dest)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=60,
    )


def calls(env: dict[str, str]) -> list[str]:
    return Path(env["CALLS"]).read_text().splitlines()


def assert_catalogue(dest: Path) -> None:
    assert (dest / "plugins" / "healthcheck.jar").read_bytes() == HEALTHCHECK_JAR
    assert (dest / "lib" / "saml.jar").read_bytes() == SAML_JAR
    assert sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*")) == [
        "lib",
        "lib/saml.jar",
        "plugins",
        "plugins/healthcheck.jar",
    ]
    for path in [dest, *dest.rglob("*")]:
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == (0o755 if path.is_dir() else 0o644), path


@pytest.mark.xfail(strict=True, reason="fetch-plugin-catalogue.sh not added yet")
def test_uses_the_verified_mirror(
    tmp_path: Path, lock: Path, env: dict[str, str]
) -> None:
    dest = tmp_path / "out" / "catalogue"
    result = run(env, lock, dest)
    assert result.returncode == 0, result.stderr
    assert_catalogue(dest)
    ref = f"ghcr.io/{REPOSITORY}/plugin-catalogue"
    log = calls(env)
    assert log[0] == f"oras resolve {ref}:{sha256(lock.read_bytes())}"
    assert log[1] == (
        f"cosign verify {ref}@{DIGEST} --certificate-identity {IDENTITY}"
        f" --certificate-oidc-issuer {ISSUER}"
    )
    assert log[2].startswith(f"oras pull {ref}@{DIGEST} ")
    assert not [c for c in log if c.startswith("curl")]
    output = result.stdout + result.stderr
    assert f"Plugin catalogue source: mirror {ref}@{DIGEST}" in output
    assert "gerritforge" not in output.lower()


@pytest.mark.xfail(strict=True, reason="fetch-plugin-catalogue.sh not added yet")
def test_lowercases_the_registry_path(
    tmp_path: Path, lock: Path, env: dict[str, str]
) -> None:
    env["GITHUB_REPOSITORY"] = "Example-Org/Example-Repo"
    result = run(env, lock, tmp_path / "catalogue")
    assert result.returncode == 0, result.stderr
    log = calls(env)
    assert log[0].startswith("oras resolve ghcr.io/example-org/example-repo/")
    assert (
        "--certificate-identity https://github.com/Example-Org/Example-Repo/" in log[1]
    )


@pytest.mark.xfail(strict=True, reason="fetch-plugin-catalogue.sh not added yet")
def test_falls_back_when_the_tag_is_missing(
    tmp_path: Path, lock: Path, env: dict[str, str]
) -> None:
    env["ORAS_RESOLVE"] = "missing"
    dest = tmp_path / "catalogue"
    result = run(env, lock, dest)
    assert result.returncode == 0, result.stderr
    assert_catalogue(dest)
    log = calls(env)
    assert not [c for c in log if c.startswith(("cosign", "oras pull"))]
    assert [c for c in log if c.startswith("curl") and HEALTHCHECK_URL in c]
    output = result.stdout + result.stderr
    assert "has no mirror" in output
    assert "Plugin catalogue source: upstream" in output


@pytest.mark.xfail(strict=True, reason="fetch-plugin-catalogue.sh not added yet")
def test_upstream_option_skips_the_mirror(
    tmp_path: Path, lock: Path, env: dict[str, str]
) -> None:
    dest = tmp_path / "catalogue"
    result = run(env, lock, dest, "--upstream")
    assert result.returncode == 0, result.stderr
    assert_catalogue(dest)
    assert not [c for c in calls(env) if c.startswith(("oras", "cosign"))]


@pytest.mark.xfail(strict=True, reason="fetch-plugin-catalogue.sh not added yet")
def test_registry_error_fails_without_fallback(
    tmp_path: Path, lock: Path, env: dict[str, str]
) -> None:
    env["ORAS_RESOLVE"] = "error"
    dest = tmp_path / "catalogue"
    result = run(env, lock, dest)
    assert result.returncode == 2
    assert "unauthorized" in result.stderr
    assert not dest.exists()
    assert not [c for c in calls(env) if c.startswith("curl")]


@pytest.mark.xfail(strict=True, reason="fetch-plugin-catalogue.sh not added yet")
def test_failed_signature_check_fails(
    tmp_path: Path, lock: Path, env: dict[str, str]
) -> None:
    env["COSIGN_RC"] = "1"
    dest = tmp_path / "catalogue"
    result = run(env, lock, dest)
    assert result.returncode == 1
    assert "signature" in result.stderr
    assert not dest.exists()
    log = calls(env)
    assert not [c for c in log if c.startswith(("oras pull", "curl"))]


@pytest.mark.xfail(strict=True, reason="fetch-plugin-catalogue.sh not added yet")
def test_mirrored_jar_must_match_the_lock(
    tmp_path: Path, lock: Path, env: dict[str, str], mirror: Path
) -> None:
    (mirror / "lib" / "saml.jar").write_bytes(b"tampered")
    dest = tmp_path / "catalogue"
    result = run(env, lock, dest)
    assert result.returncode == 1
    assert "sha256 mismatch for lib/saml" in result.stderr
    assert not dest.exists()


@pytest.mark.xfail(strict=True, reason="fetch-plugin-catalogue.sh not added yet")
def test_mirrored_lock_must_match(
    tmp_path: Path, lock: Path, env: dict[str, str], mirror: Path
) -> None:
    (mirror / "plugins.lock").write_text("# another lock\n")
    dest = tmp_path / "catalogue"
    result = run(env, lock, dest)
    assert result.returncode == 1
    assert "plugins.lock" in result.stderr
    assert not dest.exists()


@pytest.mark.xfail(strict=True, reason="fetch-plugin-catalogue.sh not added yet")
def test_mirror_with_an_extra_file_fails(
    tmp_path: Path, lock: Path, env: dict[str, str], mirror: Path
) -> None:
    (mirror / "plugins" / "extra.jar").write_bytes(b"extra")
    dest = tmp_path / "catalogue"
    result = run(env, lock, dest)
    assert result.returncode == 1
    assert not dest.exists()


@pytest.mark.xfail(strict=True, reason="fetch-plugin-catalogue.sh not added yet")
def test_refuses_a_non_empty_destination(
    tmp_path: Path, lock: Path, env: dict[str, str]
) -> None:
    dest = tmp_path / "catalogue"
    dest.mkdir()
    (dest / "stale").write_text("x")
    result = run(env, lock, dest)
    assert result.returncode == 2
    assert calls(env) == []


@pytest.mark.xfail(strict=True, reason="fetch-plugin-catalogue.sh not added yet")
def test_accepts_an_empty_destination(
    tmp_path: Path, lock: Path, env: dict[str, str]
) -> None:
    dest = tmp_path / "catalogue"
    dest.mkdir()
    result = run(env, lock, dest)
    assert result.returncode == 0, result.stderr
    assert_catalogue(dest)


@pytest.mark.xfail(strict=True, reason="fetch-plugin-catalogue.sh not added yet")
def test_rejects_a_malformed_lock(
    tmp_path: Path, lock: Path, env: dict[str, str]
) -> None:
    lock.write_text(f"plugins/healthcheck {HEALTHCHECK_URL} abc\n")
    result = run(env, lock, tmp_path / "catalogue")
    assert result.returncode == 1
    assert calls(env) == []


@pytest.mark.xfail(strict=True, reason="fetch-plugin-catalogue.sh not added yet")
@pytest.mark.parametrize(
    ("answer", "message"),
    [
        ("helper", "executable file not found"),
        ("other-ref", "-other: not found"),
    ],
)
def test_unrelated_not_found_fails_without_fallback(
    tmp_path: Path, lock: Path, env: dict[str, str], answer: str, message: str
) -> None:
    env["ORAS_RESOLVE"] = answer
    dest = tmp_path / "catalogue"
    result = run(env, lock, dest)
    assert result.returncode == 2
    assert message in result.stderr
    assert "has no mirror" not in result.stderr
    assert not dest.exists()
    assert not [c for c in calls(env) if c.startswith("curl")]
