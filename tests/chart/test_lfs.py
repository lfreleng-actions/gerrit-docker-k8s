# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for the Gerrit chart's Git LFS support through the lfs plugin.

Checks what the chart renders with ``plugins.lfs`` enabled on the s3
and fs backends: ``lfs.plugin`` in ``gerrit.config``, ``etc/lfs.config``,
the plugin ``site-init`` copies, the S3 key Secret and the environment
``render-config`` reads it from, and the NetworkPolicy egress to the S3
endpoint, plus the render failures for bad values. Then runs
``render-config.sh`` against a scratch site to check the
``etc/lfs.secure.config`` it writes and removes. The default render
must carry none of it.
"""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
from chart.test_hooks import helm_fails, plugins
from chart.test_its_jira import config_data
from chart.test_replication import egress_ports, find, render
from chart.test_saml import container, gerrit_config
from chart.test_secure_config import SECRET_ENV, run_script

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="needs helm")

KEY_ENV = ("GERRIT_LFS_S3_ACCESS_KEY", "GERRIT_LFS_S3_SECRET_KEY")


def lfs_values(**extra: Any) -> dict[str, Any]:
    """Return values enabling lfs on S3, with *extra* plugin values."""
    lfs = {
        "enabled": True,
        "storage": {"backend": "s3"},
        "s3": {"bucket": "gerrit-lfs", "region": "eu-west-1"},
        **extra,
    }
    return {"plugins": {"lfs": lfs}}


def key_refs(docs: list[Any]) -> dict[str, Any]:
    """Return the Secret references of render-config's LFS key variables."""
    return {
        e["name"]: e["valueFrom"]["secretKeyRef"]
        for e in container(docs, "render-config")["env"]
        if e["name"] in KEY_ENV
    }


def test_disabled_renders_none_of_it(tmp_path: Path) -> None:
    docs = render(tmp_path)
    assert "[lfs]" not in gerrit_config(docs)
    assert '[plugins "lfs"]' not in gerrit_config(docs)
    assert "lfs.config" not in config_data(docs)
    assert find(docs, "Secret", "gerrit-lfs") is None
    assert key_refs(docs) == {}
    assert "lfs" not in plugins(docs)


def test_s3_backend(tmp_path: Path) -> None:
    docs = render(tmp_path, lfs_values())
    config = gerrit_config(docs)
    assert "[lfs]\n\tplugin = lfs\n" in config
    assert '[plugins "lfs"]' not in config
    assert config_data(docs)["lfs.config"] == (
        "[storage]\n\tbackend = s3\n[s3]\n\tbucket = gerrit-lfs\n\tregion = eu-west-1\n"
    )
    assert plugins(docs)[-1] == "lfs"
    assert key_refs(docs) == {
        "GERRIT_LFS_S3_ACCESS_KEY": {"name": "gerrit-lfs", "key": "accessKey"},
        "GERRIT_LFS_S3_SECRET_KEY": {"name": "gerrit-lfs", "key": "secretKey"},
    }
    secret = find(docs, "Secret", "gerrit-lfs")
    assert secret is not None
    assert secret["data"] == {"accessKey": "", "secretKey": ""}
    assert secret["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"


def test_every_section_renders(tmp_path: Path) -> None:
    values = lfs_values(
        s3={
            "hostname": "minio.example.org",
            "region": "us-east-1",
            "bucket": "lfs",
            "storageClass": "REDUCED_REDUNDANCY",
            "expirationSeconds": 120,
            "disableSslVerify": False,
        },
        fs={"directory": None},
        locks={"directory": "/var/gerrit/data/lfs-locks"},
        auth={"sshExpirationSeconds": 30},
    )
    assert config_data(render(tmp_path, values))["lfs.config"] == (
        "[storage]\n\tbackend = s3\n"
        "[s3]\n\tbucket = lfs\n\tdisableSslVerify = false\n"
        "\texpirationSeconds = 120\n\thostname = minio.example.org\n"
        "\tregion = us-east-1\n\tstorageClass = REDUCED_REDUNDANCY\n"
        "[locks]\n\tdirectory = /var/gerrit/data/lfs-locks\n"
        "[auth]\n\tsshExpirationSeconds = 30\n"
    )


def test_existing_secret_is_used(tmp_path: Path) -> None:
    docs = render(tmp_path, lfs_values(existingSecret="lfs-keys"))
    assert find(docs, "Secret", "gerrit-lfs") is None
    assert {r["name"] for r in key_refs(docs).values()} == {"lfs-keys"}


def test_fs_backend_needs_no_secret(tmp_path: Path) -> None:
    values = lfs_values(storage={"backend": "fs"}, s3={}, fs={"directory": "lfs"})
    docs = render(tmp_path, values)
    assert "[lfs]\n\tplugin = lfs\n" in gerrit_config(docs)
    assert config_data(docs)["lfs.config"] == (
        "[storage]\n\tbackend = fs\n[fs]\n\tdirectory = lfs\n"
    )
    assert find(docs, "Secret", "gerrit-lfs") is None
    assert key_refs(docs) == {}
    assert "lfs" in plugins(docs)


@pytest.mark.parametrize(
    ("hostname", "ports"),
    [
        (None, set()),
        ("s3.amazonaws.com", set()),
        ("minio.example.org:443", set()),
        ("minio.example.org:9000", {9000}),
    ],
)
def test_egress_reaches_the_s3_port(
    tmp_path: Path, hostname: str | None, ports: set[int]
) -> None:
    docs = render(tmp_path, lfs_values(s3={"bucket": "b", "hostname": hostname}))
    assert egress_ports(docs) == {53, 443} | ports


def test_fs_backend_opens_no_egress(tmp_path: Path) -> None:
    values = lfs_values(storage={"backend": "fs"}, s3={"hostname": "minio:9000"})
    assert egress_ports(render(tmp_path, values)) == {53, 443}


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize(
    ("s3", "message"),
    [
        ({"accessKey": "AKIA"}, "plugins.lfs.s3.accessKey is not supported"),
        ({"secretKey": "x"}, "plugins.lfs.s3.secretKey is not supported"),
        ({"SECRETKEY": "x"}, "plugins.lfs.s3.SECRETKEY is not supported"),
        ({"bad key": "x"}, 'plugins.lfs.s3 key "bad key" is not supported'),
        ({"bucket": {"x": 1}}, "plugins.lfs.s3.bucket is not supported"),
        ({"bucket": ["a", "b"]}, "plugins.lfs.s3.bucket is not supported"),
    ],
)
def test_invalid_s3_values_fail(
    tmp_path: Path, enabled: bool, s3: dict[str, Any], message: str
) -> None:
    values = lfs_values(enabled=enabled, s3=s3)
    assert message in helm_fails(tmp_path, values)


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"storage": {"backend": "gcs"}}, "storage.backend must be s3 or fs"),
        ({"locks": "data/locks"}, "plugins.lfs.locks is not supported"),
    ],
)
def test_invalid_values_fail(
    tmp_path: Path, extra: dict[str, Any], message: str
) -> None:
    assert message in helm_fails(tmp_path, lfs_values(**extra))


@pytest.fixture
def site(tmp_path: Path) -> Path:
    """A scratch site directory with an etc/ directory."""
    (tmp_path / "etc").mkdir()
    return tmp_path


def lfs_keys(secure: Path) -> list[str]:
    """Return the keys and values git reads from lfs.secure.config."""
    read = subprocess.run(
        ["git", "config", "-f", str(secure), "--list"],
        capture_output=True,
        text=True,
        check=True,
    )
    return read.stdout.splitlines()


def test_keys_land_in_private_lfs_secure_config(site: Path) -> None:
    env = {
        **SECRET_ENV,
        "GERRIT_LFS_S3_ACCESS_KEY": "fixed-access-key",
        "GERRIT_LFS_S3_SECRET_KEY": "fixed secret; with # marks",
    }
    proc = run_script(site, env)
    assert proc.returncode == 0, proc.stderr
    secure = site / "etc" / "lfs.secure.config"
    assert stat.S_IMODE(secure.stat().st_mode) == 0o600
    assert lfs_keys(secure) == [
        "s3.accesskey=fixed-access-key",
        "s3.secretkey=fixed secret; with # marks",
    ]
    assert "fixed-access-key" not in proc.stdout + proc.stderr
    main = (site / "etc" / "secure.config").read_text(encoding="utf-8")
    assert "fixed-access-key" not in main


def test_empty_keys_are_left_out(site: Path) -> None:
    proc = run_script(site, {**SECRET_ENV, **dict.fromkeys(KEY_ENV, "")})
    assert proc.returncode == 0, proc.stderr
    secure = site / "etc" / "lfs.secure.config"
    assert stat.S_IMODE(secure.stat().st_mode) == 0o600
    assert secure.read_text(encoding="utf-8") == ""


@pytest.mark.parametrize("imported", ["file", "symlink"])
def test_no_keys_remove_lfs_secure_config(site: Path, imported: str) -> None:
    """Disabled, or on the fs backend, the chart passes no keys."""
    secure = site / "etc" / "lfs.secure.config"
    if imported == "file":
        secure.write_text("[s3]\n\tsecretKey = old\n", encoding="utf-8")
    else:
        (site / "etc" / "keys.config").write_text("kept\n", encoding="utf-8")
        secure.symlink_to("keys.config")
    proc = run_script(site, SECRET_ENV)
    assert proc.returncode == 0, proc.stderr
    assert not secure.exists() and not secure.is_symlink()
    if imported == "symlink":
        assert (site / "etc" / "keys.config").read_text(encoding="utf-8") == "kept\n"


def test_keys_replace_a_symlink_rather_than_write_through_it(site: Path) -> None:
    target = site / "etc" / "gerrit.config"
    target.write_text("[gerrit]\n", encoding="utf-8")
    secure = site / "etc" / "lfs.secure.config"
    secure.symlink_to("gerrit.config")
    env = {**SECRET_ENV, **dict.fromkeys(KEY_ENV, "k")}
    proc = run_script(site, env)
    assert proc.returncode == 0, proc.stderr
    assert not secure.is_symlink()
    assert target.read_text(encoding="utf-8") == "[gerrit]\n"


def test_keys_replace_a_symlink_to_a_directory(site: Path) -> None:
    elsewhere = site / "elsewhere"
    elsewhere.mkdir()
    secure = site / "etc" / "lfs.secure.config"
    secure.symlink_to(elsewhere)
    env = {**SECRET_ENV, **dict.fromkeys(KEY_ENV, "k")}
    proc = run_script(site, env)
    assert proc.returncode == 0, proc.stderr
    assert not secure.is_symlink()
    assert lfs_keys(secure) == ["s3.accesskey=k", "s3.secretkey=k"]
    assert list(elsewhere.iterdir()) == []


@pytest.mark.parametrize("env", [{}, dict.fromkeys(KEY_ENV, "k")])
def test_keys_are_staged_outside_what_an_export_carries(
    site: Path, env: dict[str, str]
) -> None:
    """A staging file or git lock a stopped run left is removed.

    git config takes <file>.lock while it writes, and fails while one
    exists, so a lock a killed run left would stop every later start.
    """
    (site / ".chart").mkdir()
    stale = [
        site / folder / f"{name}{suffix}"
        for folder, name in [
            ("etc", "secure.config.new"),
            ("etc", "lfs.secure.config.new"),
            (".chart", "lfs.secure.config.new"),
        ]
        for suffix in ["", ".lock"]
    ]
    for path in stale:
        path.write_text("[s3]\n\tsecretKey = old\n", encoding="utf-8")
    proc = run_script(site, {**SECRET_ENV, **env})
    assert proc.returncode == 0, proc.stderr
    assert not any(path.exists() for path in stale)
    assert sorted(p.name for p in (site / "etc").iterdir()) == sorted(
        ["secure.config", *(["lfs.secure.config"] if env else [])]
    )
