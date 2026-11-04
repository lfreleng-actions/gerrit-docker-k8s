# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Render tests for the chart's ``site-import`` init container.

The container runs ``files/scripts/site-import.py`` ahead of
``site-init`` on every start, with the site volume, the rendered
configuration and a scratch volume; ``siteImport`` picks the source.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from chart.test_replication import egress_ports, find, pod_spec, render, run_helm

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="needs helm")


PVC_SOURCE: dict[str, Any] = {
    "type": "pvc",
    "url": "/mnt/import/export",
    "volume": {"persistentVolumeClaim": {"claimName": "gerrit-export"}},
}


S3_SOURCE: dict[str, Any] = {"type": "s3", "url": "s3://gerrit-exports/final"}


def import_values(**source: Any) -> dict[str, Any]:
    return {"siteImport": {"source": source}} if source else {}


def init_names(docs: list[Any]) -> list[str]:
    return [c["name"] for c in pod_spec(docs)["initContainers"]]


def importer(docs: list[Any]) -> dict[str, Any]:
    found = [c for c in pod_spec(docs)["initContainers"] if c["name"] == "site-import"]
    assert len(found) == 1
    container: dict[str, Any] = found[0]
    return container


def env_of(container: dict[str, Any]) -> dict[str, str]:
    return {e["name"]: e.get("value", "") for e in container["env"]}


def mounts_of(container: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {m["mountPath"]: m for m in container["volumeMounts"]}


def volumes(docs: list[Any]) -> dict[str, dict[str, Any]]:
    return {v["name"]: v for v in pod_spec(docs)["volumes"]}


def refused(tmp_path: Path, values: dict[str, Any], message: str) -> None:
    path = tmp_path / "values.yaml"
    path.write_text(yaml.safe_dump(values), encoding="utf-8")
    proc = run_helm("-f", str(path))
    assert proc.returncode != 0
    assert message in proc.stderr


def test_runs_before_site_init(tmp_path: Path) -> None:
    names = init_names(render(tmp_path))
    assert names.index("site-import") == names.index("site-init") - 1


def test_default_is_a_fresh_site_in_auto_mode(tmp_path: Path) -> None:
    container = importer(render(tmp_path))
    assert container["command"] == ["python3", "/scripts/site-import.py"]
    env = env_of(container)
    assert env["SITE_IMPORT_MODE"] == "auto"
    assert env["SITE_IMPORT_SOURCE_TYPE"] == "none"
    assert env["SITE_IMPORT_FORCE_TOKEN"] == ""
    assert env["SITE_IMPORT_REINDEX"] == "true"
    assert env["SITE_IMPORT_VERIFY"] == "true"
    assert env["SITE_IMPORT_SCRATCH"] == "/scratch"
    assert env["SITE_INIT_SCRIPT"] == "/scripts/site-init.sh"
    assert "SITE_IMPORT_URLS" not in env
    assert "SITE_IMPORT_SAML_KEYSTORE" not in env


def test_shares_the_prepare_environment_with_site_init(tmp_path: Path) -> None:
    docs = render(tmp_path)
    init = next(c for c in pod_spec(docs)["initContainers"] if c["name"] == "site-init")
    shared = {k: v for k, v in env_of(init).items() if not k.startswith("GERRIT_HOOK")}
    shared = {k: v for k, v in shared.items() if not k.startswith("GERRIT_G2P")}
    env = env_of(importer(docs))
    assert {k: env[k] for k in shared} == shared
    assert env["GERRIT_SITE"] == "/mnt/site"


def test_mounts_site_config_scripts_and_scratch(tmp_path: Path) -> None:
    docs = render(tmp_path)
    mounts = mounts_of(importer(docs))
    assert mounts["/mnt/site"]["name"] == "site"
    assert mounts["/config"] == {
        "name": "config",
        "mountPath": "/config",
        "readOnly": True,
    }
    assert mounts["/scripts"]["readOnly"] is True
    assert mounts["/scratch"]["name"] == "import-scratch"
    assert "/mnt/import" not in mounts
    assert volumes(docs)["import-scratch"] == {"name": "import-scratch", "emptyDir": {}}
    assert "import-source" not in volumes(docs)


def test_scripts_configmap_carries_the_importer(tmp_path: Path) -> None:
    scripts = find(render(tmp_path), "ConfigMap", "gerrit-scripts")
    assert scripts is not None
    for name in (
        "site-import.py",
        "site_import_archive.py",
        "site_import_common.py",
        "site_import_s3.py",
        "site_import_unpack.py",
    ):
        assert name in scripts["data"]
    assert "site-init.sh" in scripts["data"]
    assert "verify-site.py" not in scripts["data"]


def test_pvc_source_mounts_the_volume_read_only(tmp_path: Path) -> None:
    docs = render(tmp_path, import_values(**PVC_SOURCE))
    container = importer(docs)
    assert env_of(container)["SITE_IMPORT_SOURCE_TYPE"] == "pvc"
    assert env_of(container)["SITE_IMPORT_SOURCE_URL"] == "/mnt/import/export"
    assert mounts_of(container)["/mnt/import"] == {
        "name": "import-source",
        "mountPath": "/mnt/import",
        "readOnly": True,
    }
    assert volumes(docs)["import-source"] == {
        "name": "import-source",
        "persistentVolumeClaim": {"claimName": "gerrit-export"},
    }
    gerrit = next(c for c in pod_spec(docs)["containers"] if c["name"] == "gerrit")
    assert "import-source" not in {m["name"] for m in gerrit["volumeMounts"]}


def test_https_secret_mounts_the_urls(tmp_path: Path) -> None:
    docs = render(tmp_path, import_values(type="https", existingSecret="export-urls"))
    container = importer(docs)
    assert env_of(container)["SITE_IMPORT_URLS"] == "/import-urls"
    assert mounts_of(container)["/import-urls"]["readOnly"] is True
    assert volumes(docs)["import-urls"]["secret"] == {
        "secretName": "export-urls",
        "defaultMode": 0o400,
    }


@pytest.mark.parametrize(
    ("source", "ports"),
    [
        ({"type": "https", "url": "https://exports.example/final"}, set()),
        ({"type": "https", "url": "https://exports.example:8443/final"}, {8443}),
        (
            {
                "type": "https",
                "existingSecret": "export-urls",
                "egressPorts": [9000, 443],
            },
            {9000},
        ),
        (
            {
                "type": "https",
                "url": "https://exports.example:8443/final",
                "egressPorts": ["8443", 9000],
            },
            {8443, 9000},
        ),
        # Only an https source fetches over the network
        ({**PVC_SOURCE, "egressPorts": [9000]}, set()),
    ],
)
def test_egress_reaches_the_https_source(
    tmp_path: Path, source: dict[str, Any], ports: set[int]
) -> None:
    docs = render(tmp_path, import_values(**source))
    assert importer(docs)
    assert egress_ports(docs) == {53, 443} | ports


def test_scratch_size_limit(tmp_path: Path) -> None:
    docs = render(tmp_path, {"siteImport": {"scratchSizeLimit": "50Gi"}})
    assert volumes(docs)["import-scratch"]["emptyDir"] == {"sizeLimit": "50Gi"}


def test_force_and_options_reach_the_container(tmp_path: Path) -> None:
    values = {
        "siteImport": {
            "mode": "force",
            "forceToken": 2,
            "reindex": False,
            "verify": False,
            "source": {**PVC_SOURCE, "expectedManifestSha256": "ab" * 32},
        }
    }
    env = env_of(importer(render(tmp_path, values)))
    assert env["SITE_IMPORT_MODE"] == "force"
    assert env["SITE_IMPORT_FORCE_TOKEN"] == "2"
    assert env["SITE_IMPORT_REINDEX"] == "false"
    assert env["SITE_IMPORT_VERIFY"] == "false"
    assert env["SITE_IMPORT_EXPECTED_MANIFEST_SHA256"] == "ab" * 32


def test_resources_default_to_gerrits(tmp_path: Path) -> None:
    docs = render(tmp_path)
    gerrit = next(c for c in pod_spec(docs)["containers"] if c["name"] == "gerrit")
    assert importer(docs)["resources"] == gerrit["resources"]
    limits = {"limits": {"memory": "8Gi"}}
    docs = render(tmp_path, {"siteImport": {"resources": limits}})
    assert importer(docs)["resources"] == limits


def test_heap_and_cpu_follow_the_resources(tmp_path: Path) -> None:
    # The default resources set a memory limit and no CPU limit
    env = env_of(importer(render(tmp_path)))
    assert env["SITE_IMPORT_JAVA_HEAP"] == "-XX:MaxRAMPercentage=70.0"
    assert env["SITE_IMPORT_CPU_LIMIT"] == ""
    limits = {"limits": {"cpu": "1500m", "memory": "16Gi"}}
    env = env_of(importer(render(tmp_path, {"siteImport": {"resources": limits}})))
    assert env["SITE_IMPORT_JAVA_HEAP"] == "-XX:MaxRAMPercentage=70.0"
    assert env["SITE_IMPORT_CPU_LIMIT"] == "1500m"
    values = {"siteImport": {"resources": {"limits": {"cpu": 4}}}}
    env = env_of(importer(render(tmp_path, values)))
    assert env["SITE_IMPORT_JAVA_HEAP"] == ""
    assert env["SITE_IMPORT_CPU_LIMIT"] == "4"


def test_heap_limit_wins_over_the_memory_limit(tmp_path: Path) -> None:
    env = env_of(importer(render(tmp_path, {"container": {"heapLimit": "3g"}})))
    assert env["SITE_IMPORT_JAVA_HEAP"] == "-Xmx3g"


def test_secure_keys_follow_the_features(tmp_path: Path) -> None:
    keys = env_of(importer(render(tmp_path)))["SITE_IMPORT_SECURE_KEYS"].split()
    assert keys == ["auth.registerEmailPrivateKey", "sendemail.smtpPass"]


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"mode": "always"}, "siteImport.mode must be auto, force or skip"),
        (
            {"source": {"type": "s3"}},
            "siteImport.source.type s3 needs siteImport.source.url s3://",
        ),
        (
            {"source": {"type": "s3", "url": "https://bucket/x"}},
            "siteImport.source.type s3 needs siteImport.source.url s3://",
        ),
        (
            {"source": {"type": "s3", "url": "s3://"}},
            "siteImport.source.type s3 needs siteImport.source.url s3://",
        ),
        (
            {"source": {**S3_SOURCE, "s3": {"endpoint": "store.example:9000"}}},
            "siteImport.source.s3.endpoint must be an http:// or https:// URL",
        ),
        (
            {"source": {**S3_SOURCE, "s3": {"endpoint": "http://"}}},
            "siteImport.source.s3.endpoint must be an http:// or https:// URL",
        ),
        (
            {"source": {**S3_SOURCE, "s3": {"endpoint": "https://store.example/s3"}}},
            "siteImport.source.s3.endpoint must be an http:// or https:// URL",
        ),
        (
            {"source": {**S3_SOURCE, "s3": {"endpoint": "https://store.example?x=1"}}},
            "siteImport.source.s3.endpoint must be an http:// or https:// URL",
        ),
        (
            {"source": {**S3_SOURCE, "s3": {"endpoint": "https://key@store.example"}}},
            "siteImport.source.s3.endpoint must be an http:// or https:// URL",
        ),
        (
            {"source": {**S3_SOURCE, "s3": {"endpoint": "https://s3.example:99999"}}},
            "siteImport.source.s3.endpoint must be an http:// or https:// URL",
        ),
        (
            {"source": {**S3_SOURCE, "s3": {"provider": ""}}},
            "siteImport.source.s3.provider must name an rclone S3 provider",
        ),
        (
            {"source": {"type": "ftp"}},
            "siteImport.source.type must be none, pvc, https or s3",
        ),
        (
            {"source": {"type": "pvc", "url": "/mnt/import"}},
            "needs siteImport.source.volume",
        ),
        (
            {"source": {**PVC_SOURCE, "url": "/data/export"}},
            "must be a directory under /mnt/import",
        ),
        ({"source": {"type": "https"}}, "needs siteImport.source.url or"),
        (
            {"source": {"type": "https", "url": "https://x", "egressPorts": [70000]}},
            "egressPorts must hold TCP port numbers",
        ),
        (
            {"source": {"type": "https", "url": "https://x", "egressPorts": ["ssh"]}},
            "egressPorts must hold TCP port numbers",
        ),
    ],
)
def test_bad_values_are_refused(
    tmp_path: Path, values: dict[str, Any], message: str
) -> None:
    # Every case is a render the chart accepted before site import
    assert importer(render(tmp_path))
    refused(tmp_path, {"siteImport": values}, message)


# --- s3 source -----------------------------------------------------------


def s3_env(docs: list[Any]) -> dict[str, Any]:
    return {e["name"]: e for e in importer(docs)["env"]}


def test_s3_source_reaches_the_container(tmp_path: Path) -> None:
    docs = render(tmp_path, import_values(**S3_SOURCE))
    env = env_of(importer(docs))
    assert env["SITE_IMPORT_SOURCE_TYPE"] == "s3"
    assert env["SITE_IMPORT_SOURCE_URL"] == "s3://gerrit-exports/final"
    assert env["SITE_IMPORT_S3_ENDPOINT"] == ""
    assert env["SITE_IMPORT_S3_REGION"] == ""
    assert env["SITE_IMPORT_S3_PROVIDER"] == "AWS"
    # Workload identity: no static keys, and nothing mounted for them
    assert "AWS_ACCESS_KEY_ID" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "SITE_IMPORT_URLS" not in env
    assert "/import-urls" not in mounts_of(importer(docs))
    assert "import-urls" not in volumes(docs)


def test_s3_options_reach_the_container(tmp_path: Path) -> None:
    s3 = {"endpoint": "https://store.example", "region": "eu-west-1"}
    values = import_values(**S3_SOURCE, s3={**s3, "provider": "Minio"})
    env = env_of(importer(render(tmp_path, values)))
    assert env["SITE_IMPORT_S3_ENDPOINT"] == "https://store.example"
    assert env["SITE_IMPORT_S3_REGION"] == "eu-west-1"
    assert env["SITE_IMPORT_S3_PROVIDER"] == "Minio"


def test_s3_keys_come_from_the_secret(tmp_path: Path) -> None:
    docs = render(tmp_path, import_values(**S3_SOURCE, existingSecret="s3-keys"))
    env = s3_env(docs)
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        assert env[key] == {
            "name": key,
            "valueFrom": {"secretKeyRef": {"name": "s3-keys", "key": key}},
        }
    # The Secret holds keys, not URLs, so it is not mounted
    assert "SITE_IMPORT_URLS" not in env
    assert "import-urls" not in volumes(docs)
    gerrit = next(c for c in pod_spec(docs)["containers"] if c["name"] == "gerrit")
    assert "AWS_SECRET_ACCESS_KEY" not in {e["name"] for e in gerrit["env"]}


@pytest.mark.parametrize(
    ("endpoint", "ports"),
    [
        ("", set()),
        ("https://store.example", set()),
        ("https://store.example:8443", {8443}),
        ("http://minio.gerrit.svc:9000", {9000}),
        ("http://store.example", {80}),
    ],
)
def test_egress_reaches_the_s3_endpoint(
    tmp_path: Path, endpoint: str, ports: set[int]
) -> None:
    docs = render(tmp_path, import_values(**S3_SOURCE, s3={"endpoint": endpoint}))
    assert egress_ports(docs) == {53, 443} | ports


# --- service account ------------------------------------------------------


def service_accounts(docs: list[Any]) -> list[dict[str, Any]]:
    return [d for d in docs if d["kind"] == "ServiceAccount"]


def test_no_service_account_by_default(tmp_path: Path) -> None:
    docs = render(tmp_path)
    assert service_accounts(docs) == []
    spec = pod_spec(docs)
    assert "serviceAccountName" not in spec
    assert spec["automountServiceAccountToken"] is False


def test_created_service_account_carries_the_annotations(tmp_path: Path) -> None:
    role = "arn:aws:iam::111122223333:role/gerrit-import"
    values = {
        "serviceAccount": {
            "create": True,
            "annotations": {"eks.amazonaws.com/role-arn": role},
        }
    }
    docs = render(tmp_path, values)
    [account] = service_accounts(docs)
    assert account["metadata"]["name"] == "gerrit"
    assert account["metadata"]["namespace"] == "gerrit-test"
    assert account["metadata"]["annotations"] == {"eks.amazonaws.com/role-arn": role}
    assert account["automountServiceAccountToken"] is False
    spec = pod_spec(docs)
    assert spec["serviceAccountName"] == "gerrit"
    # IRSA projects its own token; the API token stays unmounted
    assert spec["automountServiceAccountToken"] is False


@pytest.mark.parametrize(
    ("account", "rendered", "name"),
    [
        ({"create": True, "name": "gerrit-irsa"}, ["gerrit-irsa"], "gerrit-irsa"),
        ({"create": False, "name": "existing"}, [], "existing"),
    ],
)
def test_service_account_name(
    tmp_path: Path, account: dict[str, Any], rendered: list[str], name: str
) -> None:
    docs = render(tmp_path, {"serviceAccount": account})
    assert [a["metadata"]["name"] for a in service_accounts(docs)] == rendered
    assert pod_spec(docs)["serviceAccountName"] == name
