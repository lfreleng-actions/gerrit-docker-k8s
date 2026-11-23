# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for ``runtimeOverrides``, the operator-owned ConfigMap.

Checks that the chart mounts the ConfigMap into ``site-init`` only with
``runtimeOverrides.enabled`` and never renders it, then runs
``site-init.sh`` against a scratch site to check the override step:
an allowed key is set, an empty value removes it, any other key is
ignored and logged, a missing ConfigMap changes nothing, and on a
fresh site the overrides hold during "gerrit init" and after it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from chart.test_hooks import run_site_init
from chart.test_render import CHART
from chart.test_replication import pod_spec, render, site_init_env

SITE_INIT = CHART / "files" / "scripts" / "site-init.sh"
README = CHART / "README.md"
RENDERED = (
    "[gerrit]\n\tbasePath = git\n"
    "[auth]\n\ttype = HTTP\n\tloginUrl = https://values.example.org/login\n"
)
LOGIN = "https://sso.example.org/login"

needs_helm = pytest.mark.skipif(shutil.which("helm") is None, reason="needs helm")


def containers(docs: list[Any]) -> list[dict[str, Any]]:
    """Return every init and main container of the Gerrit pod."""
    spec = pod_spec(docs)
    return [*spec["initContainers"], *spec["containers"]]


def override_mounts(docs: list[Any]) -> dict[str, dict[str, Any]]:
    """Return each container's runtime-overrides mount, by container name."""
    return {
        c["name"]: m
        for c in containers(docs)
        for m in c.get("volumeMounts", [])
        if m["name"] == "runtime-overrides"
    }


def override_volume(docs: list[Any]) -> dict[str, Any] | None:
    """Return the runtime-overrides volume, if the pod has one."""
    for volume in pod_spec(docs)["volumes"]:
        if volume["name"] == "runtime-overrides":
            found: dict[str, Any] = volume
            return found
    return None


@needs_helm
def test_disabled_mounts_nothing(tmp_path: Path) -> None:
    docs = render(tmp_path)
    assert override_volume(docs) is None
    assert override_mounts(docs) == {}
    assert "GERRIT_RUNTIME_OVERRIDES" not in site_init_env(docs)


@pytest.mark.xfail(strict=True, reason="runtime overrides not yet added")
@needs_helm
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("", "gerrit-overrides"),
        ("ops-switches", "ops-switches"),
        # Valid ConfigMap names that YAML would read as other scalars
        ("null", "null"),
        ("true", "true"),
        ("1234", "1234"),
    ],
)
def test_enabled_mounts_the_configmap(tmp_path: Path, name: str, expected: str) -> None:
    values = {"runtimeOverrides": {"enabled": True, "configMapName": name}}
    docs = render(tmp_path, values)
    assert override_volume(docs) == {
        "name": "runtime-overrides",
        "configMap": {"name": expected, "optional": True},
    }
    assert override_mounts(docs) == {
        "site-init": {
            "name": "runtime-overrides",
            "mountPath": "/runtime-overrides",
            "readOnly": True,
        }
    }
    assert site_init_env(docs)["GERRIT_RUNTIME_OVERRIDES"] == "/runtime-overrides"


@needs_helm
@pytest.mark.parametrize("enabled", [False, True])
def test_chart_never_renders_the_configmap(tmp_path: Path, enabled: bool) -> None:
    docs = render(tmp_path, {"runtimeOverrides": {"enabled": enabled}})
    names = {d["metadata"]["name"] for d in docs if d["kind"] == "ConfigMap"}
    assert "gerrit-overrides" not in names


@pytest.fixture
def site(tmp_path: Path) -> dict[str, str]:
    """An initialised scratch site and site-init's environment."""
    bin_dir = tmp_path / "image-bin"
    bin_dir.mkdir()
    for name in ("gerrit.war", "gerrit.sh"):
        (bin_dir / name).write_text("", encoding="utf-8")
    rendered = tmp_path / "rendered"
    rendered.mkdir()
    (rendered / "gerrit.config").write_text(RENDERED, encoding="utf-8")
    for name in ("jgit.config", "notedb.config", "log4j.properties"):
        (rendered / name).write_text("", encoding="utf-8")
    root = tmp_path / "site"
    (root / "etc").mkdir(parents=True)
    (root / ".chart").mkdir()
    (root / ".chart" / "initialised").write_text("{}\n", encoding="utf-8")
    return {
        "GERRIT_SITE": str(root),
        "GERRIT_IMAGE_BIN": str(bin_dir),
        "GERRIT_RENDERED_CONFIG": str(rendered),
        "GERRIT_CATALOGUE": str(tmp_path / "catalogue"),
        "GERRIT_RUNTIME_OVERRIDES": str(tmp_path / "overrides"),
    }


def mount_configmap(env: dict[str, str], data: dict[str, str]) -> None:
    """Lay *data* out as the kubelet mounts a ConfigMap: links to ..data."""
    mount = Path(env["GERRIT_RUNTIME_OVERRIDES"])
    payload = mount / "..2026_11_23_00_00_00.000000000"
    payload.mkdir(parents=True)
    (mount / "..data").symlink_to(payload.name)
    for key, value in data.items():
        (payload / key).write_text(value, encoding="utf-8")
        (mount / key).symlink_to(f"..data/{key}")


def installed(env: dict[str, str], key: str) -> list[str]:
    """Return every value of *key* in the site's etc/gerrit.config."""
    config = Path(env["GERRIT_SITE"]) / "etc" / "gerrit.config"
    proc = subprocess.run(
        ["git", "config", "-f", str(config), "--get-all", key],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.splitlines()


@pytest.mark.xfail(strict=True, reason="runtime overrides not yet added")
def test_allowed_key_is_set(site: dict[str, str]) -> None:
    mount_configmap(site, {"auth.loginUrl": LOGIN + "\n", "auth.loginText": "SSO"})
    proc = run_site_init(site)
    assert proc.returncode == 0, proc.stderr
    assert installed(site, "auth.loginUrl") == [LOGIN]
    assert installed(site, "auth.loginText") == ["SSO"]
    assert installed(site, "auth.type") == ["HTTP"]
    assert f"Runtime override: set auth.loginUrl = {LOGIN}" in proc.stdout


@pytest.mark.xfail(strict=True, reason="runtime overrides not yet added")
@pytest.mark.parametrize("value", ["", "\n"])
def test_empty_value_removes_the_key(site: dict[str, str], value: str) -> None:
    mount_configmap(site, {"auth.loginUrl": value, "auth.logoutUrl": ""})
    proc = run_site_init(site)
    assert proc.returncode == 0, proc.stderr
    assert installed(site, "auth.loginUrl") == []
    assert installed(site, "auth.logoutUrl") == []
    assert "Runtime override: removed auth.loginUrl" in proc.stdout


@pytest.mark.xfail(strict=True, reason="runtime overrides not yet added")
@pytest.mark.parametrize(
    "key",
    [
        "auth.type",
        "auth.httpHeader",
        "saml.metadataPath",
        "httpd.filterClass",
        "gerrit.serverId",
        "auth.loginurl",
    ],
)
def test_other_key_is_ignored_and_logged(site: dict[str, str], key: str) -> None:
    secret = "not-for-the-log"
    mount_configmap(site, {key: secret})
    proc = run_site_init(site)
    assert proc.returncode == 0, proc.stderr
    config = Path(site["GERRIT_SITE"]) / "etc" / "gerrit.config"
    assert config.read_text(encoding="utf-8") == RENDERED
    assert f"Runtime override {key} is not an allowed key; ignored" in proc.stderr
    assert secret not in proc.stdout + proc.stderr


@pytest.mark.parametrize("state", ["missing", "empty", "unset"])
def test_missing_configmap_changes_nothing(site: dict[str, str], state: str) -> None:
    if state == "empty":
        mount_configmap(site, {})
    elif state == "unset":
        del site["GERRIT_RUNTIME_OVERRIDES"]
    proc = run_site_init(site)
    assert proc.returncode == 0, proc.stderr
    config = Path(site["GERRIT_SITE"]) / "etc" / "gerrit.config"
    assert config.read_text(encoding="utf-8") == RENDERED
    assert "Runtime override" not in proc.stdout + proc.stderr


@pytest.mark.xfail(strict=True, reason="runtime overrides not yet added")
def test_fresh_site_keeps_overrides_through_init(
    site: dict[str, str], tmp_path: Path
) -> None:
    """On a site without the marker, "gerrit init" runs on the config
    the overrides already changed, and install_config then restores the
    render, so the overrides must be applied again after it: the set
    key and the removal both survive into the config Gerrit starts on.
    """
    (Path(site["GERRIT_SITE"]) / ".chart" / "initialised").unlink()
    mount_configmap(site, {"auth.loginUrl": "", "auth.loginText": "SSO"})
    seen = tmp_path / "at-init.log"
    path_dir = tmp_path / "path"
    path_dir.mkdir()
    java = path_dir / "java"
    # A stub launcher: init records the overridden keys it sees, then
    # writes the config as a real init does
    java.write_text(
        "#!/bin/sh\n"
        'case " $* " in *" version "*) echo "gerrit version 3.13.4"; exit 0;; esac\n'
        'config="$GERRIT_SITE/etc/gerrit.config"\n'
        "for key in auth.loginUrl auth.loginText; do\n"
        f'  echo "$key=$(git config -f "$config" --get "$key")" >> {seen}\n'
        "done\n"
        'git config -f "$config" gerrit.serverId init-id\n',
        encoding="utf-8",
    )
    java.chmod(0o755)
    proc = run_site_init({**site, "PATH": f"{path_dir}:{os.environ['PATH']}"})
    assert proc.returncode == 0, proc.stderr
    assert seen.read_text(encoding="utf-8").split() == [
        "auth.loginUrl=",
        "auth.loginText=SSO",
    ]
    assert installed(site, "auth.loginUrl") == []
    assert installed(site, "auth.loginText") == ["SSO"]
    assert installed(site, "gerrit.serverId") == ["init-id"]
    assert (Path(site["GERRIT_SITE"]) / ".chart" / "initialised").is_file()


@pytest.mark.xfail(strict=True, reason="runtime overrides not yet added")
def test_readme_lists_the_allowed_keys() -> None:
    script = SITE_INIT.read_text(encoding="utf-8")
    match = re.search(r'^override_keys="([^"]*)"$', script, re.MULTILINE)
    assert match is not None
    readme = README.read_text(encoding="utf-8")
    section = readme.split("\n## Runtime overrides\n", 1)[1].split("\n## ", 1)[0]
    listed = re.findall(r"^- `([^`]+)`", section, re.MULTILINE)
    assert listed == match.group(1).split()
