# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for docker/fetch-plugins.sh, Dockerfile.chart's catalogue fetch."""

from __future__ import annotations

import hashlib
import http.server
import shutil
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "docker" / "fetch-plugins.sh"
JAR = b"not really a jar\n"

pytestmark = pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in ("sh", "curl", "sha256sum")),
    reason="the fetch needs sh, curl and sha256sum",
)


class _Handler(http.server.BaseHTTPRequestHandler):
    """Serve JAR for /ok.jar and 404 for everything else."""

    def do_GET(self) -> None:
        if self.path == "/ok.jar":
            self.send_response(200)
            self.send_header("Content-Length", str(len(JAR)))
            self.end_headers()
            self.wfile.write(JAR)
        else:
            self.send_error(404)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        del format, args


@pytest.fixture
def base_url() -> Iterator[str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def fetch(tmp_path: Path, *lines: str) -> subprocess.CompletedProcess[str]:
    lock = tmp_path / "plugins.lock"
    lock.write_text("# A comment\n\n" + "\n".join(lines) + "\n")
    catalogue = tmp_path / "catalogue"
    (catalogue / "plugins").mkdir(parents=True)
    (catalogue / "lib").mkdir()
    return subprocess.run(
        ["sh", str(SCRIPT), str(lock), str(catalogue)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_fetches_and_verifies_every_entry(tmp_path: Path, base_url: str) -> None:
    digest = hashlib.sha256(JAR).hexdigest()
    result = fetch(
        tmp_path,
        f"plugins/good {base_url}/ok.jar {digest}",
        f"lib/other {base_url}/ok.jar {digest}",
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "catalogue" / "plugins" / "good.jar").read_bytes() == JAR
    assert (tmp_path / "catalogue" / "lib" / "other.jar").read_bytes() == JAR


def test_missing_artefact_names_entry_and_refresh(
    tmp_path: Path, base_url: str
) -> None:
    digest = hashlib.sha256(JAR).hexdigest()
    url = f"{base_url}/job/plugin-gone/7/artifact/gone.jar"
    result = fetch(
        tmp_path,
        f"plugins/good {base_url}/ok.jar {digest}",
        f"plugins/gone {url} {digest}",
    )
    assert result.returncode != 0
    assert "plugins/gone" in result.stderr
    assert url in result.stderr
    assert "HTTP status: 404" in result.stderr
    assert "latest build" in result.stderr
    assert "bash scripts/check-plugin-lock.sh --refresh gone" in result.stderr


def test_digest_mismatch_fails(tmp_path: Path, base_url: str) -> None:
    result = fetch(tmp_path, f"plugins/good {base_url}/ok.jar {'0' * 64}")
    assert result.returncode != 0
    assert "sha256 mismatch for plugins/good" in result.stderr
    assert "--refresh" not in result.stderr


def check(tmp_path: Path, *lines: str) -> subprocess.CompletedProcess[str]:
    lock = tmp_path / "plugins.lock"
    lock.write_text("# A comment\n\n" + "\n".join(lines) + "\n")
    return subprocess.run(
        ["sh", str(SCRIPT), "--check", str(lock), str(tmp_path / "catalogue")],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


@pytest.fixture
def prepared(tmp_path: Path) -> Path:
    """A catalogue already laid out, as the build context holds it."""
    catalogue = tmp_path / "catalogue"
    (catalogue / "plugins").mkdir(parents=True)
    (catalogue / "lib").mkdir()
    (catalogue / "plugins" / "good.jar").write_bytes(JAR)
    (catalogue / "lib" / "other.jar").write_bytes(JAR)
    return catalogue


# Nothing listens here, so a check that tried to download would fail
UNREACHABLE = "http://127.0.0.1:9/ok.jar"


@pytest.mark.xfail(strict=True, reason="fetch-plugins.sh --check not added")
def test_check_verifies_without_downloading(tmp_path: Path, prepared: Path) -> None:
    digest = hashlib.sha256(JAR).hexdigest()
    result = check(
        tmp_path,
        f"plugins/good {UNREACHABLE} {digest}",
        f"lib/other {UNREACHABLE} {digest}",
    )
    assert result.returncode == 0, result.stderr
    assert "Fetching" not in result.stdout


@pytest.mark.xfail(strict=True, reason="fetch-plugins.sh --check not added")
def test_check_rejects_a_digest_mismatch(tmp_path: Path, prepared: Path) -> None:
    digest = hashlib.sha256(JAR).hexdigest()
    result = check(
        tmp_path,
        f"plugins/good {UNREACHABLE} {digest}",
        f"lib/other {UNREACHABLE} {'0' * 64}",
    )
    assert result.returncode == 1
    assert "sha256 mismatch for lib/other" in result.stderr


@pytest.mark.xfail(strict=True, reason="fetch-plugins.sh --check not added")
def test_check_rejects_a_missing_jar(tmp_path: Path, prepared: Path) -> None:
    (prepared / "lib" / "other.jar").unlink()
    digest = hashlib.sha256(JAR).hexdigest()
    result = check(
        tmp_path,
        f"plugins/good {UNREACHABLE} {digest}",
        f"lib/other {UNREACHABLE} {digest}",
    )
    assert result.returncode == 1
    assert "lib/other" in result.stderr


@pytest.mark.xfail(strict=True, reason="fetch-plugins.sh --check not added")
def test_check_rejects_an_extra_jar(tmp_path: Path, prepared: Path) -> None:
    (prepared / "plugins" / "stray.jar").write_bytes(JAR)
    digest = hashlib.sha256(JAR).hexdigest()
    result = check(
        tmp_path,
        f"plugins/good {UNREACHABLE} {digest}",
        f"lib/other {UNREACHABLE} {digest}",
    )
    assert result.returncode == 1
    assert "Expected 2 jars" in result.stderr
