# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for the Gerrit chart's admin toolbox and its helpers.

Compares the rendered toolbox with ``tests/chart/golden/admin-toolbox.golden``,
checks its isolation, and runs the helpers under ``files/toolbox/``
against a fixture site. ``gerrit-require-offline`` runs with
``socket.getaddrinfo`` stubbed, so no test depends on the host's DNS.

After an intended change to the rendered toolbox, regenerate the
golden with ``UPDATE_GOLDEN=1 uv run pytest tests/chart`` and review
the diff.
"""

from __future__ import annotations

import os
import runpy
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from chart.test_maintenance import loose_objects, make_repo, packs
from chart.test_render import CHART, GOLDEN, GOLDEN_HEADER, helm, read_golden

TOOLBOX = ("--set", "toolbox.enabled=true")
HELPERS = CHART / "files" / "toolbox"
CHECK = HELPERS / "gerrit-require-offline.py"
GC_SCRIPT = CHART / "files" / "scripts" / "git-gc.sh"
POD_DNS = "gerrit-0.gerrit-headless.gerrit-test.svc.cluster.local."
needs_helm = pytest.mark.skipif(shutil.which("helm") is None, reason="needs helm")

# Runs gerrit-require-offline with getaddrinfo answering as STUB_DNS says
STUB_RUNNER = """\
import os, runpy, socket, sys

def fake(name, *args, **kwargs):
    mode = os.environ["STUB_DNS"]
    if mode == "answer":
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", 0))]
    if mode == "nxdomain":
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
    raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")

socket.getaddrinfo = fake
check = os.environ["STUB_CHECK"]
sys.argv[0] = check
runpy.run_path(check, run_name="__main__")
"""


def render(*args: str) -> list[dict[str, Any]]:
    """Return every object the chart renders with the given helm options."""
    out = helm("template", "gerrit", str(CHART), "--namespace", "gerrit-test", *args)
    return [d for d in yaml.safe_load_all(out) if d]


def toolbox_objects(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the rendered objects that belong to the admin toolbox."""
    return [
        d
        for d in docs
        if d["metadata"].get("labels", {}).get("app.kubernetes.io/component")
        == "admin-toolbox"
    ]


def deployment(docs: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the single rendered toolbox Deployment."""
    (found,) = [d for d in docs if d["kind"] == "Deployment"]
    return found


@needs_helm
def test_no_toolbox_by_default() -> None:
    docs = render()
    assert toolbox_objects(docs) == []
    assert not [d for d in docs if d["kind"] == "Deployment"]


@needs_helm
def test_toolbox_matches_golden() -> None:
    out = helm(
        "template",
        "gerrit",
        str(CHART),
        "--namespace",
        "gerrit-test",
        *TOOLBOX,
        "--set",
        "toolbox.existingSecret=gerrit-toolbox-ssh",
        "--show-only",
        "templates/admin-toolbox.yaml",
    )
    golden = GOLDEN / "admin-toolbox.golden"
    if os.environ.get("UPDATE_GOLDEN"):
        golden.write_text(GOLDEN_HEADER + out, encoding="utf-8")
    assert out == read_golden(golden)


@needs_helm
def test_toolbox_is_at_zero_replicas_without_api_access() -> None:
    docs = render(*TOOLBOX)
    kinds = {d["kind"] for d in docs}
    assert not kinds & {"ServiceAccount", "Role", "RoleBinding", "ClusterRole"}
    dep = deployment(docs)
    assert dep["spec"]["replicas"] == 0
    assert dep["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"] == "1"
    spec = dep["spec"]["template"]["spec"]
    assert spec["automountServiceAccountToken"] is False
    assert "serviceAccountName" not in spec


@needs_helm
def test_toolbox_meets_the_restricted_standard() -> None:
    spec = deployment(render(*TOOLBOX))["spec"]["template"]["spec"]
    pod = spec["securityContext"]
    assert pod["runAsNonRoot"] is True
    assert pod["seccompProfile"] == {"type": "RuntimeDefault"}
    for container in spec["containers"] + spec.get("initContainers", []):
        ctx = container["securityContext"]
        assert ctx["allowPrivilegeEscalation"] is False
        assert ctx["capabilities"]["drop"] == ["ALL"]
        assert "add" not in ctx["capabilities"]
        assert ctx["runAsNonRoot"] is True
    assert not [v for v in spec["volumes"] if "hostPath" in v]


@needs_helm
def test_toolbox_prefers_the_gerrit_node_and_mounts_the_site() -> None:
    docs = render(*TOOLBOX)
    spec = deployment(docs)["spec"]["template"]["spec"]
    affinity = spec["affinity"]["podAffinity"]
    assert "requiredDuringSchedulingIgnoredDuringExecution" not in affinity
    (pref,) = affinity["preferredDuringSchedulingIgnoredDuringExecution"]
    term = pref["podAffinityTerm"]
    assert term["topologyKey"] == "kubernetes.io/hostname"
    (sts,) = [d for d in docs if d["kind"] == "StatefulSet"]
    assert (
        term["labelSelector"]["matchLabels"] == sts["spec"]["selector"]["matchLabels"]
    )
    claims = [
        v["persistentVolumeClaim"]
        for v in spec["volumes"]
        if "persistentVolumeClaim" in v
    ]
    assert claims == [{"claimName": f"site-{sts['metadata']['name']}-0"}]
    (container,) = spec["containers"]
    env = {e["name"]: e.get("value") for e in container["env"]}
    assert env["GERRIT_POD_DNS"] == POD_DNS
    assert env["GERRIT_SITE"] == "/var/gerrit"


@needs_helm
def test_toolbox_pod_dns_follows_cluster_domain() -> None:
    docs = render(*TOOLBOX, "--set", "clusterDomain=example.internal")
    (container,) = deployment(docs)["spec"]["template"]["spec"]["containers"]
    env = {e["name"]: e.get("value") for e in container["env"]}
    assert env["GERRIT_POD_DNS"] == (
        "gerrit-0.gerrit-headless.gerrit-test.svc.example.internal."
    )


@needs_helm
def test_toolbox_helpers_are_on_path() -> None:
    docs = render(*TOOLBOX)
    spec = deployment(docs)["spec"]["template"]["spec"]
    (helpers,) = [v for v in spec["volumes"] if v["name"] == "helpers"]
    paths = {i["path"] for i in helpers["configMap"]["items"]}
    assert {
        "gerrit-cache-purge",
        "gerrit-index-prune",
        "gerrit-gc-now",
        "gerrit-require-offline",
        "gerrit-site-export",
    } <= paths
    assert helpers["configMap"]["defaultMode"] == 0o555
    (container,) = spec["containers"]
    mounts = {m["name"]: m["mountPath"] for m in container["volumeMounts"]}
    assert mounts["helpers"] == "/usr/local/sbin"
    (config,) = [
        d for d in docs if d["kind"] == "ConfigMap" and d in toolbox_objects(docs)
    ]
    assert config["metadata"]["name"] == helpers["configMap"]["name"]
    files = {p.name: p.read_text(encoding="utf-8") for p in HELPERS.iterdir()}
    assert config["data"] == files


@needs_helm
def test_toolbox_ssh_identity_comes_from_the_secret() -> None:
    spec = deployment(render(*TOOLBOX))["spec"]["template"]["spec"]
    assert not [v for v in spec["volumes"] if "secret" in v]
    docs = render(*TOOLBOX, "--set", "toolbox.existingSecret=admin-ssh")
    spec = deployment(docs)["spec"]["template"]["spec"]
    (ssh,) = [v for v in spec["volumes"] if "secret" in v]
    assert ssh["secret"]["secretName"] == "admin-ssh"
    assert ssh["secret"]["defaultMode"] == 0o400
    assert ssh["secret"]["items"] == [{"key": "ssh-privatekey", "path": "id"}]
    assert not [d for d in docs if d["kind"] == "Secret" and "toolbox" in str(d)]


@needs_helm
def test_toolbox_network_policy() -> None:
    docs = render(*TOOLBOX)
    labels = deployment(docs)["spec"]["template"]["metadata"]["labels"]
    for svc in (d for d in docs if d["kind"] == "Service"):
        assert not svc["spec"]["selector"].items() <= labels.items()
    policies = [
        d
        for d in docs
        if d["kind"] == "NetworkPolicy"
        and d["spec"]["podSelector"]["matchLabels"].items() <= labels.items()
    ]
    (policy,) = policies
    assert policy["spec"]["policyTypes"] == ["Ingress", "Egress"]
    assert "ingress" not in policy["spec"]
    dns, ssh = policy["spec"]["egress"]
    assert "to" not in dns
    assert {(p["protocol"], p["port"]) for p in dns["ports"]} == {
        ("UDP", 53),
        ("TCP", 53),
    }
    (sts,) = [d for d in docs if d["kind"] == "StatefulSet"]
    assert ssh["to"] == [
        {"podSelector": {"matchLabels": sts["spec"]["selector"]["matchLabels"]}}
    ]
    assert ssh["ports"] == [{"protocol": "TCP", "port": 29418}]
    no_policy = render(*TOOLBOX, "--set", "networkPolicy.enabled=false")
    assert not [d for d in toolbox_objects(no_policy) if d["kind"] == "NetworkPolicy"]


@needs_helm
@pytest.mark.parametrize(
    "override",
    [
        ("--set", "nameOverride=" + "n" * 63),
        ("--set", "nameOverride=" + "n" * 55 + "-toolbox"),
        ("--set", "fullnameOverride=" + "f" * 52),
    ],
)
def test_toolbox_names_fit_kubernetes_limits(override: tuple[str, str]) -> None:
    docs = render(*TOOLBOX, *override)
    (sts,) = [d for d in docs if d["kind"] == "StatefulSet"]
    gerrit_name = sts["spec"]["selector"]["matchLabels"]["app.kubernetes.io/name"]
    for obj in toolbox_objects(docs):
        assert len(obj["metadata"]["name"]) <= 63
        for value in obj["metadata"]["labels"].values():
            assert len(value) <= 63, value
    pod = deployment(docs)["spec"]["template"]
    assert pod["metadata"]["labels"]["app.kubernetes.io/name"] != gerrit_name


def stub_dns(
    mode: str, monkeypatch: pytest.MonkeyPatch, calls: list[str] | None = None
) -> None:
    """Make socket.getaddrinfo answer, say NXDOMAIN, or time out."""

    def fake(name: str, *_args: object, **_kwargs: object) -> list[Any]:
        if calls is not None:
            calls.append(name)
        if mode == "answer":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", 0))]
        if mode == "nxdomain":
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure")

    monkeypatch.setattr(socket, "getaddrinfo", fake)


def run_check(
    monkeypatch: pytest.MonkeyPatch, *args: str, dns: str | None = POD_DNS
) -> int:
    """Run gerrit-require-offline in this process; return its exit status."""
    if dns is None:
        monkeypatch.delenv("GERRIT_POD_DNS", raising=False)
    else:
        monkeypatch.setenv("GERRIT_POD_DNS", dns)
    monkeypatch.setattr(sys, "argv", [str(CHECK), *args])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(CHECK), run_name="__main__")
    code = exc.value.code
    assert isinstance(code, int)
    return code


@pytest.mark.parametrize(
    ("mode", "args", "expected"),
    [
        ("answer", (), 1),
        ("nxdomain", (), 0),
        ("error", (), 1),
        ("answer", ("--force",), 0),
        ("error", ("--force",), 0),
        ("nxdomain", ("--force",), 0),
    ],
)
def test_check_with_a_stubbed_resolver(
    mode: str,
    args: tuple[str, ...],
    expected: int,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[str] = []
    stub_dns(mode, monkeypatch, calls)
    assert run_check(monkeypatch, *args) == expected
    assert calls == [POD_DNS]
    err = capsys.readouterr().err
    if mode != "nxdomain":
        assert ("WARNING" if args else "ERROR") in err


def test_check_fails_closed_without_a_name(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_dns("nxdomain", monkeypatch)
    assert run_check(monkeypatch, dns=None) == 1
    assert run_check(monkeypatch, "--force", dns=None) == 0


def test_check_rejects_unknown_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_dns("nxdomain", monkeypatch)
    assert run_check(monkeypatch, "--now") == 2


def helper_env(tmp_path: Path, site: Path, mode: str) -> dict[str, str]:
    """Return an environment whose PATH holds gerrit-require-offline,
    running the real check with getaddrinfo answering as *mode* says."""
    bin_dir = tmp_path / "bin"
    if not bin_dir.exists():
        bin_dir.mkdir()
        runner = tmp_path / "stub_runner.py"
        runner.write_text(STUB_RUNNER, encoding="utf-8")
        shim = bin_dir / "gerrit-require-offline"
        shim.write_text(
            f"#!/bin/sh\nexec '{sys.executable}' '{runner}' \"$@\"\n",
            encoding="utf-8",
        )
        shim.chmod(0o755)
    return {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GERRIT_SITE": str(site),
        "GERRIT_POD_DNS": POD_DNS,
        "GERRIT_GC_SCRIPT": str(GC_SCRIPT),
        "STUB_CHECK": str(CHECK),
        "STUB_DNS": mode,
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
    }


def run_helper(
    helper: str, tmp_path: Path, site: Path, mode: str, *args: str
) -> subprocess.CompletedProcess[str]:
    """Run one helper script against *site*."""
    return subprocess.run(
        ["sh", str(HELPERS / f"{helper}.sh"), *args],
        capture_output=True,
        text=True,
        check=False,
        env=helper_env(tmp_path, site, mode),
    )


def cache_site(tmp_path: Path) -> Path:
    """Return a site whose cache holds H2 files and one other file."""
    site = tmp_path / "site"
    cache = site / "cache"
    cache.mkdir(parents=True)
    for name in ("diff_summary.h2.db", "git_file_diff.mv.db", "keep.txt"):
        (cache / name).write_text("x", encoding="utf-8")
    return site


@pytest.mark.parametrize(
    ("mode", "args", "purged"),
    [
        ("answer", (), False),
        ("error", (), False),
        ("nxdomain", (), True),
        ("answer", ("--force",), True),
    ],
)
def test_cache_purge(
    mode: str, args: tuple[str, ...], purged: bool, tmp_path: Path
) -> None:
    site = cache_site(tmp_path)
    proc = run_helper("gerrit-cache-purge", tmp_path, site, mode, *args)
    assert (proc.returncode == 0) is purged, proc.stderr
    left = sorted(p.name for p in (site / "cache").iterdir())
    if purged:
        assert left == ["keep.txt"]
    else:
        assert "--force" in proc.stderr
        assert left == ["diff_summary.h2.db", "git_file_diff.mv.db", "keep.txt"]


INDEX_CONFIG = """\
[index "accounts_0013"]
\tready = true
[index "changes_0083"]
\tready = false
[index "changes_0084"]
\tready = true
[index "groups_0010"]
\tready = true
"""


def index_site(tmp_path: Path) -> Path:
    """Return a site with current, stale and unconfigured index versions."""
    site = tmp_path / "site"
    index = site / "index"
    for name in (
        "accounts_0012",
        "accounts_0013",
        "changes_0082",
        "changes_0083",
        "changes_0084",
        "groups_0010",
        "projects_0008",
    ):
        (index / name / "open").mkdir(parents=True)
    (index / "gerrit_index.config").write_text(INDEX_CONFIG, encoding="utf-8")
    return site


@pytest.mark.parametrize(
    ("mode", "args", "pruned"),
    [
        ("answer", (), False),
        ("error", (), False),
        ("nxdomain", (), True),
        ("answer", ("--force",), True),
    ],
)
def test_index_prune(
    mode: str, args: tuple[str, ...], pruned: bool, tmp_path: Path
) -> None:
    site = index_site(tmp_path)
    before = sorted(p.name for p in (site / "index").iterdir())
    proc = run_helper("gerrit-index-prune", tmp_path, site, mode, *args)
    assert (proc.returncode == 0) is pruned, proc.stderr
    left = sorted(p.name for p in (site / "index").iterdir())
    if pruned:
        assert left == [
            "accounts_0013",
            "changes_0084",
            "gerrit_index.config",
            "groups_0010",
            "projects_0008",
        ]
    else:
        assert "--force" in proc.stderr
        assert left == before


def test_index_prune_needs_a_ready_version(tmp_path: Path) -> None:
    site = index_site(tmp_path)
    (site / "index" / "gerrit_index.config").write_text(
        '[index "changes_0084"]\n\tready = false\n', encoding="utf-8"
    )
    before = sorted(p.name for p in (site / "index").iterdir())
    proc = run_helper("gerrit-index-prune", tmp_path, site, "nxdomain")
    assert proc.returncode != 0
    assert "no index version ready" in proc.stderr
    assert sorted(p.name for p in (site / "index").iterdir()) == before


def test_index_prune_needs_the_index_config(tmp_path: Path) -> None:
    site = index_site(tmp_path)
    (site / "index" / "gerrit_index.config").unlink()
    proc = run_helper("gerrit-index-prune", tmp_path, site, "nxdomain")
    assert proc.returncode != 0
    assert "gerrit_index.config" in proc.stderr
    assert (site / "index" / "changes_0082").is_dir()


@pytest.mark.parametrize(
    ("mode", "args", "ran"),
    [
        ("answer", (), False),
        ("error", (), False),
        ("nxdomain", (), True),
        ("answer", ("--force",), True),
    ],
)
def test_gc_now(mode: str, args: tuple[str, ...], ran: bool, tmp_path: Path) -> None:
    site = tmp_path / "site"
    (site / "etc").mkdir(parents=True)
    repo = make_repo(site / "git", "project")
    proc = run_helper("gerrit-gc-now", tmp_path, site, mode, *args)
    assert (proc.returncode == 0) is ran, proc.stderr
    if ran:
        assert "1 repositories, 0 failed" in proc.stdout
        assert packs(repo)
        assert loose_objects(repo) == []
    else:
        assert "--force" in proc.stderr
        assert packs(repo) == []


def test_gc_now_passes_git_gc_options(tmp_path: Path) -> None:
    site = tmp_path / "site"
    (site / "etc").mkdir(parents=True)
    repo = make_repo(site / "git", "project")
    proc = run_helper("gerrit-gc-now", tmp_path, site, "nxdomain", "--auto")
    assert proc.returncode == 0, proc.stderr
    assert packs(repo) == []


@pytest.mark.parametrize(
    "helper", ["gerrit-cache-purge", "gerrit-index-prune", "gerrit-gc-now"]
)
def test_helpers_print_usage(helper: str, tmp_path: Path) -> None:
    proc = run_helper(helper, tmp_path, tmp_path / "site", "answer", "--help")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith(f"Usage: {helper} [--force]")
    assert "--force" in proc.stdout


def test_index_prune_stops_on_an_unreadable_config(tmp_path: Path) -> None:
    site = index_site(tmp_path)
    (site / "index" / "gerrit_index.config").write_text(
        INDEX_CONFIG + '[index "projects_0008"]\n\tready = maybe\n',
        encoding="utf-8",
    )
    before = sorted(p.name for p in (site / "index").iterdir())
    proc = run_helper("gerrit-index-prune", tmp_path, site, "nxdomain")
    assert proc.returncode != 0
    assert "cannot read" in proc.stderr
    assert sorted(p.name for p in (site / "index").iterdir()) == before


# Stands in for the export tool: records its arguments
EXPORT_STUB = """\
import pathlib, sys
pathlib.Path(sys.argv[0]).with_suffix(".args").write_text(" ".join(sys.argv[1:]))
"""


@pytest.mark.parametrize(
    ("mode", "args", "ran"),
    [
        ("answer", ("final", "--out", "x"), False),
        ("error", ("final", "--out", "x"), False),
        ("nxdomain", ("final", "--out", "x"), True),
        ("answer", ("final", "--out", "x", "--i-stopped-writes"), True),
        ("answer", ("seed", "--out", "x"), True),
        ("answer", ("seed", "--out", "final"), True),
        ("answer", ("seed", "--out=final"), True),
        ("answer", ("--out", "final", "final"), False),
    ],
)
def test_site_export_checks_gerrit_before_a_final_export(
    mode: str, args: tuple[str, ...], ran: bool, tmp_path: Path
) -> None:
    stub = tmp_path / "export.py"
    stub.write_text(EXPORT_STUB, encoding="utf-8")
    recorded = stub.with_suffix(".args")
    site = tmp_path / "site"
    proc = subprocess.run(
        ["sh", str(HELPERS / "gerrit-site-export.sh"), *args],
        capture_output=True,
        text=True,
        check=False,
        env={**helper_env(tmp_path, site, mode), "GERRIT_SITE_EXPORT": str(stub)},
    )
    assert (proc.returncode == 0) is ran, proc.stderr
    if ran:
        assert recorded.read_text(encoding="utf-8") == " ".join(args)
    else:
        assert "--i-stopped-writes" in proc.stderr
        assert not recorded.exists()
