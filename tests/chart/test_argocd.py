# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for the example Argo CD application in ``K8S/argocd/``.

Renders the chart with the values the example ``Application`` sets
and checks that its ``AppProject`` admits every kind the chart then
renders, and that the chart renders no Secret, which the project
leaves out so a forgotten ``existingSecret`` fails the sync.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from chart.test_render import CHART, ROOT, helm

EXAMPLE = ROOT / "K8S" / "argocd" / "example-gerrit.yaml"
# Every optional workload, the Traefik ingress and the ServiceMonitor,
# with the Secret each optional feature reads named
EVERYTHING = (
    "--set",
    "ingress.enabled=true",
    "--set",
    "ingress.className=traefik",
    "--set",
    "ingress.traefik.enabled=true",
    "--set",
    "gc.cronJob.enabled=true",
    "--set",
    "backup.enabled=true",
    "--set",
    "backup.s3.bucket=gerrit-backups",
    "--set",
    "logs.retentionDays=7",
    "--set",
    "toolbox.enabled=true",
    "--set",
    "plugins.metrics-reporter-prometheus.enabled=true",
    "--set",
    "metrics.existingSecret=gerrit-metrics-token",
    "--set",
    "metrics.serviceMonitor.enabled=true",
    "--api-versions",
    "monitoring.coreos.com/v1",
)

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="needs helm")


def documents() -> dict[str, dict[str, Any]]:
    """Return the objects of the example's ``List`` by kind."""
    example = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    assert example["kind"] == "List"
    return {d["kind"]: d for d in example["items"]}


def render(tmp_path: Path, *args: str) -> list[dict[str, Any]]:
    """Render the chart with the example's ``valuesObject`` and *args*."""
    source = documents()["Application"]["spec"]["sources"][0]
    path = tmp_path / "values.yaml"
    path.write_text(yaml.safe_dump(source["helm"]["valuesObject"]), encoding="utf-8")
    out = helm("template", "gerrit", str(CHART), "-f", str(path), *args, auth=False)
    return [d for d in yaml.safe_load_all(out) if d]


def admitted() -> set[tuple[str, str]]:
    """Return the ``(group, kind)`` pairs the AppProject admits."""
    spec = documents()["AppProject"]["spec"]
    return {(r["group"], r["kind"]) for r in spec["namespaceResourceWhitelist"]}


def group_kind(doc: dict[str, Any]) -> tuple[str, str]:
    """Return a rendered object's API group and kind."""
    group = doc["apiVersion"].rpartition("/")[0]
    return group, doc["kind"]


@pytest.mark.parametrize("args", [(), EVERYTHING], ids=["defaults", "everything"])
def test_project_admits_what_the_chart_renders(
    tmp_path: Path, args: tuple[str, ...]
) -> None:
    rendered = {group_kind(d) for d in render(tmp_path, *args)}
    assert ("", "Secret") not in rendered
    assert rendered <= admitted()


def test_project_leaves_secrets_out() -> None:
    assert ("", "Secret") not in admitted()


def test_example_authenticates_with_saml(tmp_path: Path) -> None:
    source = documents()["Application"]["spec"]["sources"][0]
    auth = source["helm"]["valuesObject"]["auth"]
    assert auth["saml"]["enabled"] is True
    assert auth["saml"]["existingSecret"]
    assert "allowDevelopmentAccounts" not in auth
    (config,) = [
        d["data"]["gerrit.config"]
        for d in render(tmp_path)
        if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "gerrit-config"
    ]
    assert "\ttype = HTTP\n" in config
    assert "DEVELOPMENT_BECOME_ANY_ACCOUNT" not in config
    assert f"\tmemberOfAttr = {auth['saml']['memberOfAttr']}\n" in config


def test_application_matches_section_six() -> None:
    docs = documents()
    spec = docs["Application"]["spec"]
    assert "automated" not in spec["syncPolicy"]
    labels = spec["syncPolicy"]["managedNamespaceMetadata"]["labels"]
    assert labels["pod-security.kubernetes.io/enforce"] == "restricted"
    assert "RespectIgnoreDifferences=true" in spec["syncPolicy"]["syncOptions"]
    assert [d["kind"] for d in spec["ignoreDifferences"]] == ["StatefulSet"]
    chart, values = spec["sources"]
    assert chart["repoURL"] in docs["AppProject"]["spec"]["sourceRepos"]
    assert values["repoURL"] in docs["AppProject"]["spec"]["sourceRepos"]
    assert chart["helm"]["valueFiles"] == [f"${values['ref']}/gerrit/values.yaml"]
    assert docs["Secret"]["stringData"]["url"] == chart["repoURL"]
