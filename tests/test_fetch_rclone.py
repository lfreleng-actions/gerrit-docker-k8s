# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for docker/fetch-rclone.sh, Dockerfile.chart's rclone install.

The script downloads a release archive and the release's SHA256SUMS,
checks the archive against the pinned sha256 and against SHA256SUMS,
and installs the binary. A local HTTP server stands in for the
release host.
"""

from __future__ import annotations

import hashlib
import http.server
import io
import shutil
import subprocess
import threading
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "docker" / "fetch-rclone.sh"
VERSION = "v9.9.9"
NAME = f"rclone-{VERSION}-linux-amd64"
BINARY = b"#!/bin/sh\necho 'rclone v9.9.9'\n"

pytestmark = [
    pytest.mark.skipif(
        any(
            shutil.which(tool) is None
            for tool in ("sh", "curl", "sha256sum", "python3")
        ),
        reason="the fetch needs sh, curl, sha256sum and python3",
    ),
    pytest.mark.xfail(strict=True, reason="docker/fetch-rclone.sh does not exist"),
]


def archive() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zipped:
        zipped.writestr(f"{NAME}/README.txt", "rclone\n")
        zipped.writestr(f"{NAME}/rclone", BINARY)
    return buffer.getvalue()


ARCHIVE = archive()
DIGEST = hashlib.sha256(ARCHIVE).hexdigest()


def serve(files: dict[str, bytes]) -> type[http.server.BaseHTTPRequestHandler]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = files.get(self.path)
            if body is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            del format, args

    return Handler


class Release:
    """A release host the test fills: path to body, and its base URL."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.base = ""


@pytest.fixture
def release() -> Iterator[Release]:
    host = Release()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), serve(host.files))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host.base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield host
    finally:
        server.shutdown()
        server.server_close()


def publish(release: Release, sums: str | None = None) -> None:
    release.files[f"/{VERSION}/{NAME}.zip"] = ARCHIVE
    if sums is None:
        sums = f"{'1' * 64}  rclone-{VERSION}-linux-arm64.zip\n{DIGEST}  {NAME}.zip\n"
    release.files[f"/{VERSION}/SHA256SUMS"] = (
        "-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA1\n\n" + sums
    ).encode()


def fetch(
    tmp_path: Path, release: Release, digest: str = DIGEST
) -> subprocess.CompletedProcess[str]:
    dest = tmp_path / "bin"
    dest.mkdir(exist_ok=True)
    return subprocess.run(
        ["sh", str(SCRIPT), VERSION, digest, str(dest), release.base],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_installs_the_verified_binary(tmp_path: Path, release: Release) -> None:
    publish(release)
    result = fetch(tmp_path, release)
    assert result.returncode == 0, result.stderr
    binary = tmp_path / "bin" / "rclone"
    assert binary.read_bytes() == BINARY
    assert binary.stat().st_mode & 0o777 == 0o755
    assert sorted(p.name for p in (tmp_path / "bin").iterdir()) == ["rclone"]


def test_pinned_digest_mismatch_fails(tmp_path: Path, release: Release) -> None:
    publish(release)
    result = fetch(tmp_path, release, digest="0" * 64)
    assert result.returncode == 1
    assert f"{NAME}.zip has sha256 {DIGEST}, not the pinned {'0' * 64}" in (
        result.stderr
    )
    assert not (tmp_path / "bin" / "rclone").exists()


@pytest.mark.parametrize(
    "sums",
    [
        f"{'2' * 64}  {NAME}.zip\n",
        f"{DIGEST}  rclone-{VERSION}-linux-arm64.zip\n",
        f"{DIGEST}  {NAME}.zip.extra\n",
        f"0{DIGEST}  {NAME}.zip\n",
        f"{DIGEST}{NAME}.zip\n",
    ],
    ids=["other-digest", "other-archive", "longer-name", "longer-digest", "joined"],
)
def test_archive_must_match_sha256sums(
    tmp_path: Path, release: Release, sums: str
) -> None:
    publish(release, sums)
    result = fetch(tmp_path, release)
    assert result.returncode == 1
    assert f"SHA256SUMS does not list {NAME}.zip with sha256 {DIGEST}" in (
        result.stderr
    )
    assert not (tmp_path / "bin" / "rclone").exists()


@pytest.mark.parametrize(
    "line",
    [
        f"{DIGEST}  {NAME}.zip\r\n",
        f"{DIGEST} {NAME}.zip\n",
        f"{DIGEST}\t{NAME}.zip  \n",
        f"{DIGEST} *{NAME}.zip\n",
    ],
    ids=["crlf", "one-space", "tab-and-trailing", "binary-marker"],
)
def test_sha256sums_layout_is_tolerated(
    tmp_path: Path, release: Release, line: str
) -> None:
    publish(release, f"{'1' * 64}  rclone-{VERSION}-linux-arm64.zip\r\n{line}")
    result = fetch(tmp_path, release)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "bin" / "rclone").read_bytes() == BINARY


def test_missing_release_fails(tmp_path: Path, release: Release) -> None:
    result = fetch(tmp_path, release)
    assert result.returncode == 1
    assert f"{VERSION}/{NAME}.zip" in result.stderr
    assert not (tmp_path / "bin" / "rclone").exists()


def test_bad_digest_is_refused(tmp_path: Path, release: Release) -> None:
    publish(release)
    result = fetch(tmp_path, release, digest="not-a-digest")
    assert result.returncode == 2
    assert "64 lower-case hex" in result.stderr
