# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for site-import's s3 source.

``site_import_s3.py`` fetches each export file with ``rclone copyto``
from ``siteImport.source.url``, ``s3://<bucket>/<prefix>``. rclone is
configured only through environment variables, and credentials reach
it only through the environment the chart sets, never its command
line. The end-to-end tests run the importer against a stub ``rclone``
serving the export written by the real export tool.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import test_site_import
from test_site_import import load, needs_tools, run_main, state

URL = "s3://gerrit-exports/sites/final"
SECRET = "placeholder-secret"


# The fixtures test_site_import defines, which pytest finds by name here
source = test_site_import.source
export = test_site_import.export
pod = test_site_import.pod


def load_s3() -> ModuleType:
    load()
    return sys.modules["site_import_s3"]


def s3_env(pod: dict[str, Any], **extra: str) -> dict[str, str]:
    return {
        **pod["env"],
        "SITE_IMPORT_SOURCE_TYPE": "s3",
        "SITE_IMPORT_SOURCE_URL": URL,
        **extra,
    }


def test_settings_read_the_s3_options(pod: dict[str, Any]) -> None:
    module = load()
    settings = module.Settings.from_env(
        s3_env(
            pod,
            SITE_IMPORT_S3_ENDPOINT="https://store.example:9000",
            SITE_IMPORT_S3_REGION="eu-west-1",
            SITE_IMPORT_S3_PROVIDER="Minio",
        )
    )
    assert settings.source == "s3"
    assert settings.s3_endpoint == "https://store.example:9000"
    assert settings.s3_region == "eu-west-1"
    assert settings.s3_provider == "Minio"
    assert settings.source_label == f"s3:{URL}"
    defaults = module.Settings.from_env(s3_env(pod))
    assert (defaults.s3_endpoint, defaults.s3_region) == ("", "")
    assert defaults.s3_provider == "AWS"


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"SITE_IMPORT_SOURCE_URL": "https://bucket/x"}, "s3://<bucket>"),
        ({"SITE_IMPORT_SOURCE_URL": "s3://"}, "s3://<bucket>"),
        ({"SITE_IMPORT_SOURCE_URL": "s3:///prefix"}, "s3://<bucket>"),
        ({"SITE_IMPORT_SOURCE_URL": "s3://bucket/../x"}, "s3://<bucket>"),
        ({"SITE_IMPORT_S3_ENDPOINT": "store.example:9000"}, "endpoint"),
        ({"SITE_IMPORT_S3_ENDPOINT": "ftp://store.example"}, "endpoint"),
        ({"SITE_IMPORT_S3_ENDPOINT": "http://"}, "endpoint"),
        ({"SITE_IMPORT_S3_ENDPOINT": "https://:9000"}, "endpoint"),
        ({"SITE_IMPORT_S3_ENDPOINT": "https://store.example/"}, "endpoint"),
        ({"SITE_IMPORT_S3_ENDPOINT": "https://store.example/s3"}, "endpoint"),
        ({"SITE_IMPORT_S3_ENDPOINT": "https://store.example?x=1"}, "endpoint"),
        ({"SITE_IMPORT_S3_ENDPOINT": "https://store.example#x"}, "endpoint"),
        ({"SITE_IMPORT_S3_ENDPOINT": "https://key@store.example"}, "endpoint"),
        ({"SITE_IMPORT_S3_ENDPOINT": "https://store.example:99999"}, "endpoint"),
        ({"SITE_IMPORT_S3_ENDPOINT": "https://store.example:0"}, "endpoint"),
        ({"SITE_IMPORT_S3_ENDPOINT": "https://store.example:"}, "endpoint"),
        ({"SITE_IMPORT_S3_ENDPOINT": "https://store example"}, "endpoint"),
        ({"SITE_IMPORT_S3_PROVIDER": ""}, "provider"),
    ],
)
def test_settings_refuse_bad_s3_values(
    pod: dict[str, Any], extra: dict[str, str], message: str
) -> None:
    module = load()
    with pytest.raises(module.SiteImportError, match=message):
        module.Settings.from_env(s3_env(pod, **extra))


@pytest.mark.parametrize(
    ("url", "path"),
    [
        ("s3://bucket", "bucket"),
        ("s3://bucket/", "bucket"),
        ("s3://bucket/a/b/", "bucket/a/b"),
        (URL, "gerrit-exports/sites/final"),
    ],
)
def test_object_paths(pod: dict[str, Any], url: str, path: str) -> None:
    module = load_s3()
    settings = load().Settings.from_env(s3_env(pod, SITE_IMPORT_SOURCE_URL=url))
    assert module.remote_path(settings, "manifest.json") == (
        f"siteimport:{path}/manifest.json"
    )
    assert module.object_url(settings, "manifest.json") == (
        f"s3://{path}/manifest.json"
    )


def test_rclone_command_holds_no_secrets(pod: dict[str, Any]) -> None:
    module = load_s3()
    settings = load().Settings.from_env(s3_env(pod))
    dest = Path("/scratch/download/git.tar.zst")
    command = module.rclone_command(settings, "git.tar.zst", dest)
    assert command == [
        "rclone",
        "copyto",
        "--error-on-no-transfer",
        "--stats",
        "0",
        "--retries",
        "5",
        "--retries-sleep",
        "3s",
        "--low-level-retries",
        "10",
        "siteimport:gerrit-exports/sites/final/git.tar.zst",
        str(dest),
    ]


def test_rclone_environment(pod: dict[str, Any]) -> None:
    module = load_s3()
    settings = load().Settings.from_env(
        s3_env(
            pod,
            SITE_IMPORT_S3_ENDPOINT="http://store.example:9000",
            SITE_IMPORT_S3_REGION="eu-west-1",
            SITE_IMPORT_S3_PROVIDER="Minio",
        )
    )
    base = {
        "PATH": "/usr/bin",
        "AWS_ACCESS_KEY_ID": "AKIDEXAMPLE",
        "AWS_SECRET_ACCESS_KEY": SECRET,
        "AWS_WEB_IDENTITY_TOKEN_FILE": "/var/run/secrets/eks/token",
        # A stray rclone setting must not reach the fetch
        "RCLONE_CONFIG_SITEIMPORT_ACL": "public-read",
        "RCLONE_HEADER": "X: y",
    }
    env = module.rclone_env(settings, base)
    assert {k: v for k, v in env.items() if k.startswith("RCLONE_")} == {
        "RCLONE_CONFIG": "/notfound",
        "RCLONE_CONFIG_SITEIMPORT_TYPE": "s3",
        "RCLONE_CONFIG_SITEIMPORT_PROVIDER": "Minio",
        "RCLONE_CONFIG_SITEIMPORT_ENV_AUTH": "true",
        "RCLONE_CONFIG_SITEIMPORT_ENDPOINT": "http://store.example:9000",
        "RCLONE_CONFIG_SITEIMPORT_REGION": "eu-west-1",
    }
    # Credentials stay where the chart put them, for env_auth to read
    assert env["AWS_SECRET_ACCESS_KEY"] == SECRET
    assert env["AWS_WEB_IDENTITY_TOKEN_FILE"] == "/var/run/secrets/eks/token"
    assert env["PATH"] == "/usr/bin"
    aws = module.rclone_env(load().Settings.from_env(s3_env(pod)), {})
    assert "RCLONE_CONFIG_SITEIMPORT_ENDPOINT" not in aws
    assert "RCLONE_CONFIG_SITEIMPORT_REGION" not in aws
    assert aws["RCLONE_CONFIG_SITEIMPORT_PROVIDER"] == "AWS"


def stub_rclone(pod: dict[str, Any], export: Path, fail: str = "") -> Path:
    """Put a fake rclone on PATH serving the export; return its log.

    It records its arguments and the rclone settings it sees, exits
    with $STUB_STATUS for the object $STUB_FAIL names,
    and otherwise copies the export file the object path names.
    """
    tmp: Path = pod["tmp"]
    bin_dir = tmp / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp / "rclone.log"
    rclone = bin_dir / "rclone"
    rclone.write_text(
        "#!/bin/sh\n"
        f'echo "argv: $*" >> {log}\n'
        f'env | grep "^RCLONE_" | sort >> {log}\n'
        "for arg; do src=$dest; dest=$arg; done\n"
        "name=${src##*/}\n"
        'if [ "$name" = "$STUB_FAIL" ]; then exit "$STUB_STATUS"; fi\n'
        '[ "$name" = "$STUB_EMPTY" ] && exit 0\n'
        f'cp "{export}/$name" "$dest"\n'
    )
    rclone.chmod(0o755)
    path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
    pod["env"]["PATH"] = path
    return log


@needs_tools
def test_s3_import_adopts_the_export(
    pod: dict[str, Any],
    export: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    log = stub_rclone(pod, export)
    env = s3_env(
        pod,
        SITE_IMPORT_S3_ENDPOINT="http://store.example:9000",
        SITE_IMPORT_S3_PROVIDER="Minio",
        AWS_ACCESS_KEY_ID="AKIDEXAMPLE",
        AWS_SECRET_ACCESS_KEY=SECRET,
    )
    _, code = run_main(env, monkeypatch)
    out = capsys.readouterr()
    assert code == 0, out.err
    record = state(pod["site"])
    assert record["state"] == "complete"
    assert record["source"] == f"s3:{URL}"
    text = log.read_text()
    for name in ("manifest.json", "git.tar.zst", "site.tar.zst"):
        assert f"siteimport:gerrit-exports/sites/final/{name}" in text
        assert f"downloading {name} from {URL}/{name}" in out.out
    assert "RCLONE_CONFIG_SITEIMPORT_ENDPOINT=http://store.example:9000" in text
    assert "RCLONE_CONFIG_SITEIMPORT_ENV_AUTH=true" in text
    assert SECRET not in text
    assert SECRET not in out.out + out.err
    assert (pod["site"] / "etc" / "gerrit.config").is_file()


@needs_tools
@pytest.mark.parametrize(
    ("status", "message"),
    [
        ("9", f"{URL}/git.tar.zst does not exist"),
        ("1", f"downloading {URL}/git.tar.zst failed with rclone exit status 1"),
    ],
)
def test_s3_failure_names_the_object(
    pod: dict[str, Any],
    export: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    message: str,
) -> None:
    stub_rclone(pod, export)
    env = s3_env(pod, STUB_FAIL="git.tar.zst", STUB_STATUS=status)
    _, code = run_main(env, monkeypatch)
    assert code == 1
    record = state(pod["site"])
    assert record["state"] == "failed"
    assert record["reason"] == message
    assert not (pod["site"] / "etc").exists()


@needs_tools
def test_s3_fetch_that_writes_nothing_fails(
    pod: dict[str, Any], export: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_rclone(pod, export)
    env = s3_env(pod, STUB_EMPTY="manifest.json")
    _, code = run_main(env, monkeypatch)
    assert code == 1
    assert state(pod["site"])["reason"] == (
        f"rclone wrote nothing for {URL}/manifest.json"
    )


@needs_tools
def test_s3_import_resolves_latest(
    pod: dict[str, Any],
    export: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    log = stub_rclone(pod, export)
    (export / "latest").write_text("20260102T030405Z\n")
    latest = "s3://gerrit-backups/gerrit/latest"
    env = s3_env(pod, SITE_IMPORT_SOURCE_URL=latest)
    _, code = run_main(env, monkeypatch)
    out = capsys.readouterr()
    assert code == 0, out.err
    dated = "gerrit-backups/gerrit/20260102T030405Z"
    calls = [line for line in log.read_text().splitlines() if "copyto" in line]
    assert [call.split()[-2] for call in calls] == [
        "siteimport:gerrit-backups/gerrit/latest",
        f"siteimport:{dated}/manifest.json",
        f"siteimport:{dated}/git.tar.zst",
        f"siteimport:{dated}/site.tar.zst",
    ]
    assert f"{latest} names the backup s3://{dated}" in out.out
    assert state(pod["site"])["source"] == f"s3:{latest}"


@needs_tools
@pytest.mark.parametrize("content", ["", "../20260102T030405Z", "2026-01-02"])
def test_s3_import_refuses_a_bad_latest(
    pod: dict[str, Any],
    export: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: str,
) -> None:
    log = stub_rclone(pod, export)
    (export / "latest").write_text(content)
    latest = "s3://gerrit-backups/gerrit/latest"
    _, code = run_main(s3_env(pod, SITE_IMPORT_SOURCE_URL=latest), monkeypatch)
    assert code == 1
    assert state(pod["site"])["reason"] == f"{latest} names no backup: {content!r}"
    assert "manifest.json" not in log.read_text()


@needs_tools
def test_s3_import_refuses_a_latest_that_is_not_utf8(
    pod: dict[str, Any],
    export: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = stub_rclone(pod, export)
    (export / "latest").write_bytes(b"20260102T030405Z\xff\xfe\n")
    latest = "s3://gerrit-backups/gerrit/latest"
    _, code = run_main(s3_env(pod, SITE_IMPORT_SOURCE_URL=latest), monkeypatch)
    assert code == 1
    recorded = state(pod["site"])
    assert recorded["state"] == "failed"
    assert recorded["reason"] == (
        f"{latest} is not a valid pointer: it is not UTF-8 text"
    )
    assert "manifest.json" not in log.read_text()


@needs_tools
def test_s3_import_resolves_only_the_final_latest(
    pod: dict[str, Any],
    export: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # backup.s3.prefix: latest puts the pointer at <bucket>/latest/latest.
    log = stub_rclone(pod, export)
    (export / "latest").write_text("20260102T030405Z\n")
    env = s3_env(pod, SITE_IMPORT_SOURCE_URL="s3://backups/latest/latest")
    _, code = run_main(env, monkeypatch)
    out = capsys.readouterr()
    assert code == 0, out.err
    calls = [line for line in log.read_text().splitlines() if "copyto" in line]
    assert [call.split()[-2] for call in calls] == [
        "siteimport:backups/latest/latest",
        "siteimport:backups/latest/20260102T030405Z/manifest.json",
        "siteimport:backups/latest/20260102T030405Z/git.tar.zst",
        "siteimport:backups/latest/20260102T030405Z/site.tar.zst",
    ]
