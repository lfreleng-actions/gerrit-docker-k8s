# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Render tests for the chart's backup CronJob.

``backup.enabled`` adds a CronJob that runs
``files/scripts/site-backup.py`` on the site volume, mounted read-only,
and its own NetworkPolicy. The rendered CronJob is compared with
``tests/chart/golden/backup-cronjob.golden``, and its site mount with
the one site-import's report maps to Gerrit's path; after an intended
change, regenerate it with ``UPDATE_GOLDEN=1 uv run pytest tests/chart``
and review the diff.
"""

from __future__ import annotations

import importlib
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from chart.test_render import CHART, GOLDEN, GOLDEN_HEADER, helm, read_golden
from chart.test_replication import find, render, run_helm

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="needs helm")

ENABLED: dict[str, Any] = {"enabled": True, "s3": {"bucket": "gerrit-backups"}}


def backup_values(**extra: Any) -> dict[str, Any]:
    return {"backup": {**ENABLED, **extra}}


def cronjob(docs: list[Any]) -> dict[str, Any]:
    found = find(docs, "CronJob", "gerrit-backup")
    assert found is not None
    cron: dict[str, Any] = found
    return cron


def pod_of(docs: list[Any]) -> dict[str, Any]:
    pod: dict[str, Any] = cronjob(docs)["spec"]["jobTemplate"]["spec"]["template"]
    return pod


def env_of(docs: list[Any]) -> dict[str, str]:
    (container,) = pod_of(docs)["spec"]["containers"]
    return {e["name"]: e.get("value", "") for e in container["env"]}


def refused(tmp_path: Path, values: dict[str, Any], message: str) -> None:
    path = tmp_path / "values.yaml"
    path.write_text(yaml.safe_dump(values), encoding="utf-8")
    proc = run_helm("-f", str(path))
    assert proc.returncode != 0
    assert message in proc.stderr


def test_no_backup_by_default(tmp_path: Path) -> None:
    docs = render(tmp_path)
    assert find(docs, "CronJob", "gerrit-backup") is None
    assert find(docs, "NetworkPolicy", "gerrit-backup") is None


def render_cronjob_template(tmp_path: Path, values: dict[str, Any]) -> str:
    path = tmp_path / "values.yaml"
    path.write_text(yaml.safe_dump(values), encoding="utf-8")
    out: str = helm(
        "template",
        "gerrit",
        str(CHART),
        "--namespace",
        "gerrit-test",
        "-f",
        str(path),
        "--show-only",
        "templates/backup-cronjob.yaml",
    )
    return out


def check_golden(name: str, out: str) -> None:
    golden = GOLDEN / name
    if os.environ.get("UPDATE_GOLDEN"):
        golden.write_text(GOLDEN_HEADER + out, encoding="utf-8")
    assert out == read_golden(golden)


def test_backup_matches_golden(tmp_path: Path) -> None:
    values = backup_values(
        timeZone="Etc/UTC",
        existingSecret="gerrit-backup-s3",
        scratchSizeLimit="50Gi",
        s3={
            "bucket": "gerrit-backups",
            "prefix": "/sites/gerrit/",
            "endpoint": "http://store.example:9000",
            "region": "eu-west-1",
            "provider": "Minio",
        },
    )
    check_golden("backup-cronjob.golden", render_cronjob_template(tmp_path, values))


def test_suspended_backup_matches_golden(tmp_path: Path) -> None:
    """``backup.suspend`` pauses the schedule but keeps the CronJob."""
    out = render_cronjob_template(tmp_path, backup_values(suspend=True))
    check_golden("backup-cronjob-suspended.golden", out)


def test_backup_defaults(tmp_path: Path) -> None:
    docs = render(tmp_path, backup_values())
    env = env_of(docs)
    assert env["SITE_BACKUP_URL"] == "s3://gerrit-backups/gerrit-test/gerrit"
    assert env["SITE_BACKUP_S3_PROVIDER"] == "AWS"
    assert env["SITE_BACKUP_KEEP_DAILY"] == "14"
    assert env["SITE_BACKUP_KEEP_WEEKLY"] == "8"
    assert env["SITE_BACKUP_MIN_AGE_HOURS"] == "24"
    assert env["SITE_BACKUP_MAX_RUN_HOURS"] == "6"
    assert "AWS_SECRET_ACCESS_KEY" not in env
    cron = cronjob(docs)
    assert cron["spec"]["jobTemplate"]["spec"]["activeDeadlineSeconds"] == 21600
    assert cron["spec"]["schedule"] == "0 2 * * *"
    assert cron["spec"]["concurrencyPolicy"] == "Forbid"
    assert cron["spec"]["suspend"] is False
    # The CronJob's scripts come from the chart's scripts ConfigMap
    scripts = find(docs, "ConfigMap", "gerrit-scripts")
    assert scripts is not None
    assert "site-backup.py" in scripts["data"]
    assert "site_backup_retention.py" in scripts["data"]
    assert "site_backup_volume.py" in scripts["data"]


def test_site_import_knows_the_backup_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """site-import's report maps the CronJob's mount to Gerrit's path."""
    monkeypatch.syspath_prepend(str(CHART / "files" / "scripts"))
    report = importlib.import_module("site_import_report")
    env = env_of(render(tmp_path, backup_values()))
    assert {env["SITE_BACKUP_SITE"]: "/var/gerrit"} == report.CHART_BACKUP_MOUNTS


def test_default_prefix_differs_by_namespace(tmp_path: Path) -> None:
    """Two releases of one name in different namespaces, sharing a
    bucket, never share a prefix: their locks are on different volumes,
    so each could otherwise move latest to the other's backup and
    prune the other's history.
    """
    path = tmp_path / "values.yaml"
    path.write_text(yaml.safe_dump(backup_values()), encoding="utf-8")
    urls = []
    for namespace in ("team-a", "team-b"):
        proc = run_helm("-f", str(path), "--namespace", namespace)
        assert proc.returncode == 0, proc.stderr
        docs = [d for d in yaml.safe_load_all(proc.stdout) if d]
        urls.append(env_of(docs)["SITE_BACKUP_URL"])
    assert urls == [
        "s3://gerrit-backups/team-a/gerrit",
        "s3://gerrit-backups/team-b/gerrit",
    ]


def test_backup_pod_reads_the_site_and_is_isolated(tmp_path: Path) -> None:
    values = {
        **backup_values(s3={"bucket": "b", "endpoint": "https://s3.example:8443"}),
        "serviceAccount": {"create": True},
    }
    docs = render(tmp_path, values)
    pod = pod_of(docs)
    spec = pod["spec"]
    assert spec["automountServiceAccountToken"] is False
    assert spec["serviceAccountName"] == "gerrit"
    assert spec["securityContext"]["runAsUser"] == 1001
    (container,) = spec["containers"]
    # The site read-only, once: no subPath for kubelet to create as root;
    # the lock is a file site-init creates on the site
    site = [m for m in container["volumeMounts"] if m["name"] == "site"]
    assert site == [{"name": "site", "mountPath": "/mnt/site", "readOnly": True}]
    assert not any("subPath" in m for m in container["volumeMounts"])
    assert "SITE_BACKUP_LOCK" not in env_of(docs)
    claims = [v for v in spec["volumes"] if "persistentVolumeClaim" in v]
    assert [c["persistentVolumeClaim"] for c in claims] == [
        {"claimName": "site-gerrit-0"}
    ]
    labels = pod["metadata"]["labels"]
    for svc in (d for d in docs if d["kind"] == "Service"):
        assert not svc["spec"]["selector"].items() <= labels.items()
    policies = [
        d
        for d in docs
        if d["kind"] == "NetworkPolicy"
        and d["spec"]["podSelector"]["matchLabels"].items() <= labels.items()
    ]
    assert [p["metadata"]["name"] for p in policies] == ["gerrit-backup"]
    (policy,) = policies
    assert "ingress" not in policy["spec"]
    ports = {
        port["port"]
        for rule in policy["spec"]["egress"]
        for port in rule["ports"]
        if port["protocol"] == "TCP"
    }
    assert ports == {53, 443, 8443}


def test_backup_names_fit(tmp_path: Path) -> None:
    long = "n" * 70
    docs = render(tmp_path, {**backup_values(), "fullnameOverride": long})
    cron = [d for d in docs if d["kind"] == "CronJob"]
    assert [len(c["metadata"]["name"]) for c in cron] == [52]
    labels = cron[0]["spec"]["jobTemplate"]["spec"]["template"]["metadata"]["labels"]
    assert all(len(v) <= 63 for v in labels.values())


@pytest.mark.parametrize(
    ("s3", "retention", "message"),
    [
        ({}, {}, "backup.s3.bucket must name an S3 bucket"),
        ({"bucket": "b/c"}, {}, "backup.s3.bucket must name an S3 bucket"),
        ({"bucket": "b", "prefix": "a/../c"}, {}, "backup.s3.prefix must be"),
        ({"bucket": "b", "prefix": "a//c"}, {}, "backup.s3.prefix must be"),
        ({"bucket": "b", "endpoint": "ftp://x"}, {}, "backup.s3.endpoint must be"),
        ({"bucket": "b", "provider": ""}, {}, "backup.s3.provider must name"),
        ({"bucket": "b"}, {"keepDaily": -1}, "backup.retention.keepDaily must be"),
        ({"bucket": "b"}, {"keepWeekly": "x"}, "backup.retention.keepWeekly must be"),
        ({"bucket": "b"}, {"minAgeHours": 1.5}, "backup.retention.minAgeHours must be"),
        (
            {"bucket": "b"},
            {"minAgeHours": 0},
            "backup.retention.minAgeHours must be at least 1",
        ),
    ],
)
def test_backup_refuses_bad_values(
    tmp_path: Path, s3: dict[str, Any], retention: dict[str, Any], message: str
) -> None:
    values: dict[str, Any] = {"backup": {"enabled": True, "s3": s3}}
    if retention:
        values["backup"]["retention"] = retention
    refused(tmp_path, values, message)


@pytest.mark.parametrize("hours", [0, -1, 1.5, "x"])
def test_backup_refuses_bad_max_run_hours(tmp_path: Path, hours: Any) -> None:
    values = backup_values(s3={"bucket": "b"}, maxRunHours=hours)
    refused(tmp_path, values, "backup.maxRunHours must be a whole number of at least 1")


def test_backup_deadline_follows_max_run_hours(tmp_path: Path) -> None:
    docs = render(tmp_path, backup_values(maxRunHours=2))
    assert cronjob(docs)["spec"]["jobTemplate"]["spec"]["activeDeadlineSeconds"] == 7200
    assert env_of(docs)["SITE_BACKUP_MAX_RUN_HOURS"] == "2"


def test_backup_exclude_renders_into_the_command(tmp_path: Path) -> None:
    (default,) = pod_of(render(tmp_path, backup_values()))["spec"]["containers"]
    assert default["command"] == ["python3", "/scripts/site-backup.py"]
    paths = ["data/events-log-db*", 'data/odd "name"']
    docs = render(tmp_path, backup_values(exclude=paths))
    (container,) = pod_of(docs)["spec"]["containers"]
    assert container["command"] == [
        "python3",
        "/scripts/site-backup.py",
        "--exclude=data/events-log-db*",
        '--exclude=data/odd "name"',
    ]
