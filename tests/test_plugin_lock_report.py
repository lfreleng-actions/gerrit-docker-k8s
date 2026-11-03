# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for the --report mode of scripts/check-plugin-lock.sh.

A stand-in ``curl`` placed first on PATH serves recorded GerritForge
responses, so these tests never touch the network: any URL without a
recorded response fails as an unreachable host would. Like the real
curl, it rejects a URL holding brackets unless globbing is off, and
prints ``--write-out``'s ``%{http_code}`` even when a request fails.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "scripts" / "check-plugin-lock.sh"
FIXTURES = Path(__file__).parent / "fixtures"

CI = "https://gerrit-ci.gerritforge.com/job"
JOB = "plugin-healthcheck-bazel-stable-3.13"
ARTEFACT = "artifact/bazel-bin/plugins/healthcheck/healthcheck.jar"
PINNED_URL = f"{CI}/{JOB}/1/{ARTEFACT}"
JOB_API = f"{CI}/{JOB}/api/json?tree=lastSuccessfulBuild[number]"
JAR = b"healthcheck jar bytes"
DIGEST = hashlib.sha256(JAR).hexdigest()

SAML_JOB = "plugin-saml-bazel-stable-3.13"
SAML_URL = f"{CI}/{SAML_JOB}/1/artifact/bazel-bin/plugins/saml/saml.jar"
SAML_API = f"{CI}/{SAML_JOB}/api/json?tree=lastSuccessfulBuild[number]"
SAML_JAR = b"saml jar bytes"

FAKE_CURL = """
import json
import os
import sys

routes = json.loads(open(os.environ["FAKE_CURL_ROUTES"]).read())
args = sys.argv[1:]
out = None
url = None
write_out = ""
i = 0
while i < len(args):
    arg = args[i]
    if arg in ("-o", "--output"):
        out = args[i + 1]
        i += 2
        continue
    if arg in ("-w", "--write-out"):
        write_out = args[i + 1]
        i += 2
        continue
    if arg in ("--retry", "--retry-delay", "--max-time"):
        i += 2
        continue
    if not arg.startswith("-"):
        url = arg
    i += 1
with open(os.environ["FAKE_CURL_LOG"], "a") as log:
    log.write(f"{url}\\n")
if "[" in url and not any(
    a == "--globoff" or (a.startswith("-") and not a.startswith("--") and "g" in a)
    for a in args
):
    sys.stderr.write(f"curl: (3) bad range in URL: {url}\\n")
    sys.exit(3)


def finish(code, status):
    sys.stdout.write(write_out.replace("%{http_code}", status))
    sys.exit(code)


route = routes.get(url)
if route is None:
    sys.stderr.write(f"curl: (7) Failed to connect: {url}\\n")
    finish(7, "000")
if isinstance(route, int):
    sys.stderr.write(f"curl: (22) The requested URL returned error: {route}\\n")
    finish(22, str(route))
data = open(route, "rb").read()
if out:
    open(out, "wb").write(data)
else:
    sys.stdout.buffer.write(data)
    sys.stdout.flush()
finish(0, "200")
"""

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="the checker is a bash script"
)


class Harness:
    """Run the checker against a lock with recorded CI responses."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        curl = self.bin / "curl"
        curl.write_text(f"#!{sys.executable}\n{FAKE_CURL}")
        curl.chmod(0o755)
        self.routes: dict[str, str | int] = {}
        self.log = tmp_path / "curl.log"
        self.report = tmp_path / "report.json"

    def serve(self, url: str, body: bytes | Path | int) -> None:
        """Answer ``url`` with ``body``, or fail with an HTTP status."""
        if isinstance(body, int):
            self.routes[url] = body
            return
        if isinstance(body, bytes):
            path = self.tmp / f"body-{len(self.routes)}"
            path.write_bytes(body)
            body = path
        self.routes[url] = str(body)

    def run(self, *lines: str) -> subprocess.CompletedProcess[str]:
        lock = self.tmp / "plugins.lock"
        lock.write_text("\n".join(lines) + "\n")
        routes = self.tmp / "routes.json"
        routes.write_text(json.dumps(self.routes))
        env = {
            **os.environ,
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "FAKE_CURL_ROUTES": str(routes),
            "FAKE_CURL_LOG": str(self.log),
        }
        return subprocess.run(
            [
                "bash",
                str(SCRIPT),
                "--lock",
                str(lock),
                "--report",
                str(self.report),
            ],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

    def entries(self) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = json.loads(self.report.read_text())["entries"]
        return entries

    def refresh(self) -> list[str]:
        names: list[str] = json.loads(self.report.read_text())["refresh"]
        return names


def healthcheck(digest: str = DIGEST) -> str:
    return f"plugins/healthcheck {PINNED_URL} {digest}"


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def test_pinned_build_current(harness: Harness) -> None:
    harness.serve(JOB_API, FIXTURES / "plugin_lock_job_build_1.json")
    harness.serve(PINNED_URL, JAR)

    result = harness.run(healthcheck())

    assert result.returncode == 0, result.stderr
    assert harness.entries() == [
        {
            "entry": "plugins/healthcheck",
            "job": JOB,
            "url": PINNED_URL,
            "pinned_build": 1,
            "latest_build": 1,
            "artefact": "ok",
            "status": "current",
        }
    ]
    assert harness.refresh() == []


def test_newer_build_is_a_warning(harness: Harness) -> None:
    harness.serve(JOB_API, FIXTURES / "plugin_lock_job_build_2.json")
    harness.serve(PINNED_URL, JAR)

    result = harness.run(healthcheck())

    assert result.returncode == 0, result.stderr
    [entry] = harness.entries()
    assert entry["latest_build"] == 2
    assert entry["artefact"] == "ok"
    assert entry["status"] == "newer"
    assert harness.refresh() == ["plugins/healthcheck"]
    assert "build 2" in result.stderr


def test_missing_artefact_fails(harness: Harness) -> None:
    harness.serve(JOB_API, FIXTURES / "plugin_lock_job_build_2.json")
    harness.serve(PINNED_URL, 404)

    result = harness.run(healthcheck())

    assert result.returncode == 1
    [entry] = harness.entries()
    assert entry["artefact"] == "missing"
    assert entry["status"] == "missing"
    assert harness.refresh() == ["plugins/healthcheck"]


def test_gone_artefact_fails(harness: Harness) -> None:
    harness.serve(JOB_API, FIXTURES / "plugin_lock_job_build_2.json")
    harness.serve(PINNED_URL, 410)

    result = harness.run(healthcheck())

    assert result.returncode == 1
    [entry] = harness.entries()
    assert entry["status"] == "missing"
    assert harness.refresh() == ["plugins/healthcheck"]


@pytest.mark.parametrize("status", [429, 500, 503])
def test_server_error_is_not_missing(harness: Harness, status: int) -> None:
    harness.serve(JOB_API, FIXTURES / "plugin_lock_job_build_1.json")
    harness.serve(PINNED_URL, status)

    result = harness.run(healthcheck())

    assert result.returncode == 2
    [entry] = harness.entries()
    assert entry["latest_build"] == 1
    assert entry["artefact"] == "error"
    assert entry["status"] == "error"
    assert harness.refresh() == []
    assert f"HTTP {status}" in result.stderr


def test_server_error_with_newer_build_is_refreshed(harness: Harness) -> None:
    harness.serve(JOB_API, FIXTURES / "plugin_lock_job_build_2.json")
    harness.serve(PINNED_URL, 503)

    result = harness.run(healthcheck())

    assert result.returncode == 2
    [entry] = harness.entries()
    assert entry["status"] == "error"
    assert harness.refresh() == ["plugins/healthcheck"]


def test_digest_mismatch_fails(harness: Harness) -> None:
    harness.serve(JOB_API, FIXTURES / "plugin_lock_job_build_1.json")
    harness.serve(PINNED_URL, JAR)

    result = harness.run(healthcheck("0" * 64))

    assert result.returncode == 1
    [entry] = harness.entries()
    assert entry["artefact"] == "mismatch"
    assert entry["status"] == "mismatch"
    assert harness.refresh() == ["plugins/healthcheck"]
    assert "mismatch" in result.stderr


def test_unreachable_job_api_is_an_error(harness: Harness) -> None:
    harness.serve(PINNED_URL, JAR)

    result = harness.run(healthcheck())

    assert result.returncode == 2
    [entry] = harness.entries()
    assert entry["latest_build"] is None
    assert entry["artefact"] == "ok"
    assert entry["status"] == "error"
    assert harness.refresh() == []


def test_failed_download_with_newer_build_is_refreshed(harness: Harness) -> None:
    harness.serve(JOB_API, FIXTURES / "plugin_lock_job_build_2.json")

    result = harness.run(healthcheck())

    assert result.returncode == 2
    [entry] = harness.entries()
    assert entry["latest_build"] == 2
    assert entry["artefact"] == "error"
    assert entry["status"] == "error"
    assert harness.refresh() == ["plugins/healthcheck"]


def test_report_covers_every_entry(harness: Harness) -> None:
    harness.serve(JOB_API, FIXTURES / "plugin_lock_job_build_1.json")
    harness.serve(PINNED_URL, JAR)
    harness.serve(SAML_API, FIXTURES / "plugin_lock_job_build_2.json")
    harness.serve(SAML_URL, SAML_JAR)
    saml = f"lib/saml {SAML_URL} {hashlib.sha256(SAML_JAR).hexdigest()}"

    result = harness.run(healthcheck(), saml)

    assert result.returncode == 0, result.stderr
    statuses = {e["entry"]: e["status"] for e in harness.entries()}
    assert statuses == {"plugins/healthcheck": "current", "lib/saml": "newer"}
    assert harness.refresh() == ["lib/saml"]
    requested = set(harness.log.read_text().split())
    assert requested == {JOB_API, PINNED_URL, SAML_API, SAML_URL}
