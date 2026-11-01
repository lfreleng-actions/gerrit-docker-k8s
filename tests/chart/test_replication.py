# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for the Gerrit chart's outbound replication remotes.

Checks that ``replication.enabled`` renders ``etc/replication.config``,
the ``known_hosts`` ConfigMap, the SSH mount, the plugin enablement and
the NetworkPolicy egress for the remotes, and that the default render
carries none of them. The golden render of ``replication.config`` lives
with the example that enables it, under ``tests/chart/golden/``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from chart.test_render import CHART

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="needs helm")
XFAIL = pytest.mark.xfail(strict=True, reason="replication is not rendered yet")

GITHUB_KEY = (
    "github.com ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
)
EXTRA_KEY = "git.example.org ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExtraHostKey"
REPLICATION: dict[str, Any] = {
    "replication": {
        "enabled": True,
        "sshKeySecret": "gerrit-replication-ssh",
        "knownHosts": EXTRA_KEY + "\n",
        "remotes": [
            {
                "name": "github",
                "url": "git@github.com:example-org/${name}.git",
                "authGroup": "GitHub Replication",
                "push": ["+refs/*:refs/*"],
                "remoteNameStyle": "dash",
                "projects": [],
                "mirror": False,
            },
            {
                "name": "mirror",
                "url": "ssh://git@git.example.org:2222/${name}.git",
                "mirror": True,
                "push": ["+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*"],
            },
        ],
    }
}


def run_helm(*args: str) -> subprocess.CompletedProcess[str]:
    """Run ``helm template`` on the chart with the given options."""
    return subprocess.run(
        ["helm", "template", "gerrit", str(CHART), "--namespace", "gerrit-test", *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def render(tmp_path: Path, values: dict[str, Any] | None = None) -> list[Any]:
    """Return every object the chart renders, with *values* when given."""
    args: list[str] = []
    if values is not None:
        path = tmp_path / "values.yaml"
        path.write_text(yaml.safe_dump(values), encoding="utf-8")
        args = ["-f", str(path)]
    proc = run_helm(*args)
    assert proc.returncode == 0, proc.stderr
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


def find(docs: list[Any], kind: str, name: str) -> dict[str, Any] | None:
    """Return the rendered object of *kind* and *name*, if any."""
    for doc in docs:
        if doc["kind"] == kind and doc["metadata"]["name"] == name:
            found: dict[str, Any] = doc
            return found
    return None


def pod_spec(docs: list[Any]) -> dict[str, Any]:
    """Return the Gerrit StatefulSet's pod spec."""
    sts = find(docs, "StatefulSet", "gerrit")
    assert sts is not None
    spec: dict[str, Any] = sts["spec"]["template"]["spec"]
    return spec


def site_init_env(docs: list[Any]) -> dict[str, str]:
    """Return the site-init container's environment."""
    init = [c for c in pod_spec(docs)["initContainers"] if c["name"] == "site-init"]
    return {e["name"]: e.get("value", "") for e in init[0]["env"]}


def egress_ports(docs: list[Any]) -> set[int]:
    """Return the TCP egress ports the chart's NetworkPolicies allow."""
    return {
        port["port"]
        for doc in docs
        if doc["kind"] == "NetworkPolicy"
        for rule in doc["spec"].get("egress", [])
        for port in rule.get("ports", [])
        if port["protocol"] == "TCP"
    }


def git_config(text: str, tmp_path: Path, *args: str) -> str:
    """Return ``git config`` output for a rendered config file."""
    path = tmp_path / "replication.config"
    path.write_text(text, encoding="utf-8")
    proc = subprocess.run(
        ["git", "config", "-f", str(path), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def test_disabled_renders_none_of_it(tmp_path: Path) -> None:
    docs = render(tmp_path)
    config = find(docs, "ConfigMap", "gerrit-config")
    assert config is not None
    assert "replication.config" not in config["data"]
    assert find(docs, "ConfigMap", "gerrit-known-hosts") is None
    spec = pod_spec(docs)
    assert all(v["name"] != "replication-ssh" for v in spec["volumes"])
    env = site_init_env(docs)
    assert "replication" not in env["GERRIT_PLUGINS"].split()
    assert ".ssh" not in env["GERRIT_SITE_DIRS"].split()
    assert egress_ports(docs) == {53, 443}


@XFAIL
def test_enabled_renders_the_config(tmp_path: Path) -> None:
    docs = render(tmp_path, REPLICATION)
    config = find(docs, "ConfigMap", "gerrit-config")
    assert config is not None
    text = config["data"]["replication.config"]
    assert git_config(text, tmp_path, "gerrit.autoReload") == "true"
    assert git_config(text, tmp_path, "replication.maxRetries") == "5"
    assert git_config(text, tmp_path, "replication.lockErrorMaxRetries") == "5"
    assert (
        git_config(text, tmp_path, "remote.github.url")
        == "git@github.com:example-org/${name}.git"
    )
    assert git_config(text, tmp_path, "remote.github.authGroup") == "GitHub Replication"
    assert git_config(text, tmp_path, "--get-all", "remote.mirror.push").split(
        "\n"
    ) == ["+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*"]
    assert git_config(text, tmp_path, "remote.mirror.mirror") == "true"


@XFAIL
def test_remote_renders_only_the_keys_it_sets(tmp_path: Path) -> None:
    docs = render(tmp_path, REPLICATION)
    config = find(docs, "ConfigMap", "gerrit-config")
    assert config is not None
    keys = git_config(
        config["data"]["replication.config"],
        tmp_path,
        "--name-only",
        "--get-regexp",
        r"^remote\.mirror\.",
    ).split("\n")
    assert sorted(keys) == [
        "remote.mirror.mirror",
        "remote.mirror.push",
        "remote.mirror.push",
        "remote.mirror.url",
    ]


@XFAIL
def test_enabled_adds_the_plugin_once(tmp_path: Path) -> None:
    values = {**REPLICATION, "pluginCatalogue": {"plugins": ["replication", "x"]}}
    listed = site_init_env(render(tmp_path, values))["GERRIT_PLUGINS"].split()
    assert sorted(listed) == ["replication", "x"]
    alone = site_init_env(render(tmp_path, REPLICATION))["GERRIT_PLUGINS"].split()
    assert alone == ["replication"]


@XFAIL
def test_known_hosts_carry_github_and_extra_keys(tmp_path: Path) -> None:
    docs = render(tmp_path, REPLICATION)
    cm = find(docs, "ConfigMap", "gerrit-known-hosts")
    assert cm is not None
    lines = cm["data"]["known_hosts"].splitlines()
    assert GITHUB_KEY in lines
    assert sum(line.startswith("github.com ") for line in lines) == 3
    assert EXTRA_KEY in lines


@XFAIL
def test_ssh_material_is_mounted_read_only(tmp_path: Path) -> None:
    docs = render(tmp_path, REPLICATION)
    spec = pod_spec(docs)
    volume = [v for v in spec["volumes"] if v["name"] == "replication-ssh"]
    assert len(volume) == 1
    projected = volume[0]["projected"]
    assert projected["defaultMode"] == 0o400
    sources = projected["sources"]
    assert {"secret": {"name": "gerrit-replication-ssh"}} in sources
    cm = [s["configMap"] for s in sources if "configMap" in s]
    assert cm == [
        {
            "name": "gerrit-known-hosts",
            "items": [{"key": "known_hosts", "path": "known_hosts2"}],
        }
    ]
    gerrit = [c for c in spec["containers"] if c["name"] == "gerrit"][0]
    mounts = [m for m in gerrit["volumeMounts"] if m["name"] == "replication-ssh"]
    assert mounts == [
        {"name": "replication-ssh", "mountPath": "/var/gerrit/.ssh", "readOnly": True}
    ]
    assert ".ssh" in site_init_env(docs)["GERRIT_SITE_DIRS"].split()


@XFAIL
def test_no_key_secret_mounts_known_hosts_only(tmp_path: Path) -> None:
    values = {"replication": {**REPLICATION["replication"], "sshKeySecret": ""}}
    spec = pod_spec(render(tmp_path, values))
    volume = [v for v in spec["volumes"] if v["name"] == "replication-ssh"]
    sources = volume[0]["projected"]["sources"]
    assert [list(s) for s in sources] == [["configMap"]]


@XFAIL
@pytest.mark.parametrize(
    ("url", "port"),
    [
        ("git@github.com:org/${name}.git", 22),
        ("ssh://git@git.example.org/${name}.git", 22),
        ("ssh://git@git.example.org:2222/${name}.git", 2222),
        ("http://git.example.org/${name}.git", 80),
        ("git://git.example.org/${name}.git", 9418),
    ],
)
def test_egress_reaches_each_remote(tmp_path: Path, url: str, port: int) -> None:
    values = {"replication": {"enabled": True, "remotes": [{"name": "r", "url": url}]}}
    assert egress_ports(render(tmp_path, values)) == {53, 443, port}


def test_https_remote_needs_no_extra_egress(tmp_path: Path) -> None:
    values = {
        "replication": {
            "enabled": True,
            "remotes": [{"name": "r", "url": "https://git.example.org/${name}.git"}],
        }
    }
    assert egress_ports(render(tmp_path, values)) == {53, 443}


@XFAIL
@pytest.mark.parametrize(
    ("admin_url", "ports"),
    [
        ("gerrit+http://replica.example.org/", {80}),
        ("gerrit+ssh://replica.example.org/", {22}),
        ("gerrit+ssh://replica.example.org:29418/", {29418}),
    ],
)
def test_egress_reaches_the_admin_url(
    tmp_path: Path, admin_url: str, ports: set[int]
) -> None:
    remote = {
        "name": "r",
        "url": "https://replica.example.org/${name}.git",
        "adminUrl": admin_url,
    }
    values = {"replication": {"enabled": True, "remotes": [remote]}}
    assert egress_ports(render(tmp_path, values)) == {53, 443} | ports


@XFAIL
@pytest.mark.parametrize("key", ["url", "adminUrl"])
def test_egress_reaches_each_url_in_a_list(tmp_path: Path, key: str) -> None:
    remote: dict[str, Any] = {"name": "r", "url": "https://git.example.org/${name}.git"}
    remote[key] = [
        "ssh://git@git.example.org:2222/${name}.git",
        "https://git.example.org:9443/${name}.git",
    ]
    values = {"replication": {"enabled": True, "remotes": [remote]}}
    assert egress_ports(render(tmp_path, values)) == {53, 443, 2222, 9443}


@XFAIL
@pytest.mark.parametrize(
    ("remotes", "message"),
    [
        ([{"url": "git@github.com:o/${name}.git"}], "needs a name"),
        ([{"name": "github"}], "needs a url"),
        (
            [
                {"name": "a", "url": "git@github.com:o/${name}.git"},
                {"name": "a", "url": "git@github.com:p/${name}.git"},
            ],
            "listed twice",
        ),
    ],
)
def test_invalid_remotes_fail(
    tmp_path: Path, remotes: list[dict[str, str]], message: str
) -> None:
    path = tmp_path / "values.yaml"
    path.write_text(
        yaml.safe_dump({"replication": {"enabled": True, "remotes": remotes}}),
        encoding="utf-8",
    )
    proc = run_helm("-f", str(path))
    assert proc.returncode != 0
    assert message in proc.stderr
