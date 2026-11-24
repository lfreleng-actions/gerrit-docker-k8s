# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for the Gerrit chart's shared SELinux level.

``seLinux.level`` gives every pod that mounts the site volume the same
``securityContext.seLinuxOptions.level``, so that on nodes assigning
each pod its own MCS categories a second pod cannot relabel the volume
away from Gerrit. Checks the five pod templates that mount it, every
rendered pod that mounts the site claim, the empty default, and the
refusal of a malformed level.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from chart.test_replication import run_helm

# Turns on every feature whose pod mounts the site volume
FEATURES: dict[str, Any] = {
    "backup": {"enabled": True, "s3": {"bucket": "gerrit-backups"}},
    "gc": {"cronJob": {"enabled": True}},
    "logs": {"retentionDays": 30},
    "toolbox": {"enabled": True},
}
SITE_PODS = {
    ("StatefulSet", "gerrit"),
    ("CronJob", "gerrit-backup"),
    ("CronJob", "gerrit-git-gc"),
    ("CronJob", "gerrit-log-cleanup"),
    ("Deployment", "gerrit-toolbox"),
}
SITE_CLAIM = "site-gerrit-0"
LEVEL = "s0:c123,c456"

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="needs helm")


def template(tmp_path: Path, level: object = None) -> Any:
    """Run ``helm template`` with every site feature on and *level* set."""
    values: dict[str, Any] = dict(FEATURES)
    if level is not None:
        values["seLinux"] = {"level": level}
    path = tmp_path / "values.yaml"
    path.write_text(yaml.safe_dump(values), encoding="utf-8")
    return run_helm("-f", str(path))


def render(tmp_path: Path, level: object = None) -> list[dict[str, Any]]:
    """Return every rendered object, with every site feature on."""
    proc = template(tmp_path, level)
    assert proc.returncode == 0, proc.stderr
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


def pod_spec(doc: dict[str, Any]) -> dict[str, Any] | None:
    """Return the pod spec an object renders, or None if it has none."""
    if doc["kind"] == "Pod":
        spec: dict[str, Any] = doc["spec"]
        return spec
    spec = doc.get("spec") or {}
    if doc["kind"] == "CronJob":
        spec = spec["jobTemplate"]["spec"]
    template_spec: dict[str, Any] | None = (spec.get("template") or {}).get("spec")
    return template_spec


def mounts_site(doc: dict[str, Any], spec: dict[str, Any]) -> bool:
    """Whether the pod an object renders mounts the site claim."""
    claims = {
        (v.get("persistentVolumeClaim") or {}).get("claimName")
        for v in spec.get("volumes") or []
    }
    templates = {
        t["metadata"]["name"] for t in doc["spec"].get("volumeClaimTemplates") or []
    }
    return SITE_CLAIM in claims or "site" in templates


def site_pods(docs: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    """Map each object whose pod mounts the site claim to its pod spec."""
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for doc in docs:
        spec = pod_spec(doc)
        if spec is not None and mounts_site(doc, spec):
            found[(doc["kind"], doc["metadata"]["name"])] = spec
    return found


def has_key(node: object, key: str) -> bool:
    """Whether *key* appears anywhere in a parsed YAML tree."""
    if isinstance(node, dict):
        return any(k == key or has_key(v, key) for k, v in node.items())
    if isinstance(node, list):
        return any(has_key(v, key) for v in node)
    return False


def test_the_site_pods_are_the_five_templates(tmp_path: Path) -> None:
    assert set(site_pods(render(tmp_path))) == SITE_PODS


@pytest.mark.xfail(strict=True, reason="seLinux.level not implemented yet")
@pytest.mark.parametrize("level", [LEVEL, "s0", "s0:c0", "s0:c1023", "s0:c5,c9,c700"])
def test_every_site_pod_carries_the_level(tmp_path: Path, level: str) -> None:
    pods = site_pods(render(tmp_path, level))
    assert set(pods) >= SITE_PODS
    for name, spec in pods.items():
        assert spec["securityContext"].get("seLinuxOptions") == {"level": level}, name


def test_empty_level_renders_no_selinux_options(tmp_path: Path) -> None:
    for level in (None, ""):
        for doc in render(tmp_path, level):
            spec = pod_spec(doc)
            if spec is not None:
                assert not has_key(spec, "seLinuxOptions"), doc["metadata"]["name"]


@pytest.mark.xfail(strict=True, reason="seLinux.level not implemented yet")
@pytest.mark.parametrize(
    "level",
    ["s0:c1024", "c1,c2", "s0:c5,", "s0:c1, c2", "s1:c1", " s0", "user_u"],
)
def test_malformed_level_is_refused(tmp_path: Path, level: str) -> None:
    proc = template(tmp_path, level)
    assert proc.returncode != 0
    assert f'seLinux.level "{level}"' in proc.stderr
