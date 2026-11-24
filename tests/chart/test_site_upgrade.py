# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for ``siteUpgrade``, the in-place Gerrit upgrade in site-init.

Checks ``site-upgrade.py``'s release comparison and each mode's
decision, then runs ``site-init.sh`` against a scratch site with a stub
Java launcher: ``off`` warns, ``refuse`` stops with a message in the
termination log, ``auto`` runs init and reindex and rewrites the
marker, a failure leaves the marker absent and the next start retries,
and ``chart.siteUpgrade.mode`` in the runtime overrides ConfigMap
replaces the values' mode. The render tests check the environment and
resources the chart gives ``site-init``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from chart.test_hooks import helm_fails, run_site_init
from chart.test_render import CHART
from chart.test_replication import pod_spec, render, site_init_env
from chart.test_runtime_overrides import installed, mount_configmap

SCRIPT = CHART / "files" / "scripts" / "site-upgrade.py"
RENDERED = "[gerrit]\n\tbasePath = git\n"
OLD, NEW = "3.12.3", "3.13.4"

needs_helm = pytest.mark.skipif(shutil.which("helm") is None, reason="needs helm")
# Removed by the commit that adds siteUpgrade
XFAIL = pytest.mark.xfail(strict=True, reason="siteUpgrade is not implemented yet")


@pytest.fixture
def upgrade() -> ModuleType:
    """Load the hyphenated script as a module."""
    spec = importlib.util.spec_from_file_location("site_upgrade", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["site_upgrade"] = module
    spec.loader.exec_module(module)
    return module


@XFAIL
@pytest.mark.parametrize(
    ("site", "image", "expected"),
    [
        ("3.12.3", "3.13.4", 1),
        ("3.13.4", "3.13.10", 1),
        ("3.9.11", "3.10.0", 1),
        ("3.13.4", "3.12.11", -1),
        ("4.0.0", "3.99.99", -1),
        ("3.13.4", "3.13.4", 0),
        ("3.13", "3.13.0", 0),
        ("v3.13.4", "3.13.4-12-gabcdef0", 0),
        (None, "3.13.4", None),
        ("", "3.13.4", None),
        ("3.13.4", "unknown", None),
        ("3", "3.13.4", None),
        ("3.13.4.1", "3.13.4", None),
    ],
)
def test_compare_releases(
    upgrade: ModuleType, site: str | None, image: str, expected: int | None
) -> None:
    assert upgrade.compare(site, image) == expected


@XFAIL
@pytest.mark.parametrize(
    ("mode", "site", "image", "pending", "action", "words"),
    [
        ("off", NEW, NEW, False, "nothing", []),
        ("refuse", NEW, NEW, False, "nothing", []),
        ("auto", NEW, NEW, False, "nothing", []),
        ("off", OLD, NEW, False, "warn", [OLD, NEW, "siteUpgrade.mode: auto"]),
        ("refuse", OLD, NEW, False, "refuse", [OLD, NEW, "siteUpgrade.mode: auto"]),
        ("auto", OLD, NEW, False, "upgrade", [OLD, NEW]),
        ("off", NEW, OLD, False, "warn", [OLD, NEW, "downgrades"]),
        ("refuse", NEW, OLD, False, "refuse", [OLD, NEW, "downgrades"]),
        ("auto", NEW, OLD, False, "refuse", [OLD, NEW, "downgrades", "backup"]),
        # A marker an older chart wrote without a version
        ("off", None, NEW, False, "nothing", []),
        ("refuse", None, NEW, False, "refuse", ["gerritVersion"]),
        ("auto", None, NEW, False, "refuse", ["gerritVersion"]),
        ("off", "garbage", NEW, False, "warn", ["garbage"]),
        ("auto", "garbage", NEW, False, "refuse", ["garbage"]),
        # An image whose gerrit.war names no release
        ("off", OLD, None, False, "warn", [OLD]),
        ("auto", OLD, None, False, "refuse", ["siteUpgrade.mode: off"]),
        # An earlier upgrade from OLD that did not finish
        ("off", OLD, NEW, True, "refuse", [OLD, "did not finish"]),
        ("refuse", OLD, NEW, True, "refuse", [OLD, "did not finish"]),
        ("auto", OLD, OLD, True, "refuse", [OLD, "backup"]),
    ],
)
def test_decide(
    upgrade: ModuleType,
    mode: str,
    site: str | None,
    image: str | None,
    pending: bool,
    action: str,
    words: list[str],
) -> None:
    decision = upgrade.decide(mode, site, image, pending)
    assert decision.action == action
    for word in words:
        assert word in decision.message


@XFAIL
def test_decide_rejects_an_unknown_mode(upgrade: ModuleType) -> None:
    with pytest.raises(upgrade.UpgradeError, match="siteUpgrade.mode"):
        upgrade.decide("on", OLD, NEW)


@XFAIL
def test_marker_record(upgrade: ModuleType) -> None:
    assert upgrade.marker_record(NEW, OLD, "2026-11-24T00:00:00Z") == {
        "gerritVersion": NEW,
        "initialisedAt": "2026-11-24T00:00:00Z",
        "upgradedFrom": OLD,
    }


@XFAIL
@pytest.mark.parametrize(
    ("target", "image", "action", "words"),
    [
        (NEW, NEW, "upgrade", [OLD, NEW]),
        (NEW, "3.14.0", "upgrade", [OLD, "3.14.0"]),
        # init may have migrated the site to the target already
        ("3.14.0", NEW, "refuse", [OLD, "3.14.0", NEW, "backup"]),
        (None, NEW, "refuse", ["upgrade-pending", "backup"]),
        ("garbage", NEW, "refuse", ["upgrade-pending", "backup"]),
    ],
)
def test_decide_resumes_only_towards_the_target(
    upgrade: ModuleType,
    target: str | None,
    image: str,
    action: str,
    words: list[str],
) -> None:
    decision = upgrade.decide("auto", OLD, image, pending=True, target=target)
    assert decision.action == action
    for word in words:
        assert word in decision.message


def write_war(path: Path, version: str) -> None:
    """Write a gerrit.war whose manifest names *version*."""
    with zipfile.ZipFile(path, "w") as war:
        war.writestr(
            "META-INF/MANIFEST.MF",
            f"Manifest-Version: 1.0\nImplementation-Version: {version}\n",
        )


@pytest.fixture
def site(tmp_path: Path) -> dict[str, str]:
    """A site OLD initialised, an image carrying NEW, a stub Java
    launcher that logs its arguments, and site-init's environment.
    """
    bin_dir = tmp_path / "image-bin"
    bin_dir.mkdir()
    write_war(bin_dir / "gerrit.war", NEW)
    (bin_dir / "gerrit.sh").write_text("", encoding="utf-8")
    rendered = tmp_path / "rendered"
    rendered.mkdir()
    (rendered / "gerrit.config").write_text(RENDERED, encoding="utf-8")
    for name in ("jgit.config", "notedb.config", "log4j.properties"):
        (rendered / name).write_text("", encoding="utf-8")
    root = tmp_path / "site"
    (root / "etc").mkdir(parents=True)
    (root / ".chart").mkdir()
    write_marker(root, {"gerritVersion": OLD, "initialisedAt": "2026-01-01T00:00:00Z"})
    path_dir = tmp_path / "path"
    path_dir.mkdir()
    java = path_dir / "java"
    # init writes a key the reinstall must remove, and reindex, the
    # last step under the lock, one nothing after it may remove;
    # STUB_FAIL names a command that fails
    java.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> {tmp_path / "java.log"}\n'
        'case " $* " in *" $STUB_FAIL "*) exit 3;; esac\n'
        'case " $* " in *" init "*)\n'
        '  git config -f "$GERRIT_SITE/etc/gerrit.config" init.ran yes;;\n'
        '*" reindex "*)\n'
        '  git config -f "$GERRIT_SITE/etc/gerrit.config" reindex.ran yes\n'
        '  echo "# reindexed" >> "$GERRIT_SITE/bin/gerrit.sh";;\n'
        "esac\n",
        encoding="utf-8",
    )
    java.chmod(0o755)
    termination = tmp_path / "termination-log"
    termination.write_text("", encoding="utf-8")
    return {
        "GERRIT_SITE": str(root),
        "GERRIT_IMAGE_BIN": str(bin_dir),
        "GERRIT_RENDERED_CONFIG": str(rendered),
        "GERRIT_CATALOGUE": str(tmp_path / "catalogue"),
        "GERRIT_RUNTIME_OVERRIDES": str(tmp_path / "overrides"),
        "GERRIT_TERMINATION_LOG": str(termination),
        "PATH": f"{path_dir}:{os.environ['PATH']}",
        "STUB_FAIL": "none",
    }


def write_marker(root: Path, data: dict[str, Any]) -> None:
    (root / ".chart" / "initialised").write_text(json.dumps(data), encoding="utf-8")


def marker(env: dict[str, str]) -> dict[str, Any] | None:
    path = Path(env["GERRIT_SITE"]) / ".chart" / "initialised"
    if not path.exists():
        return None
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def java_calls(env: dict[str, str]) -> list[str]:
    log = Path(env["GERRIT_TERMINATION_LOG"]).with_name("java.log")
    if not log.exists():
        return []
    return log.read_text(encoding="utf-8").splitlines()


def termination(env: dict[str, str]) -> str:
    return Path(env["GERRIT_TERMINATION_LOG"]).read_text(encoding="utf-8")


@XFAIL
def test_off_warns_and_starts(site: dict[str, str]) -> None:
    proc = run_site_init(site)
    assert proc.returncode == 0, proc.stderr
    warnings = [line for line in proc.stderr.splitlines() if "warning" in line]
    assert len(warnings) == 1
    assert OLD in warnings[0]
    assert NEW in warnings[0]
    assert "siteUpgrade.mode: auto" in warnings[0]
    assert marker(site) == {
        "gerritVersion": OLD,
        "initialisedAt": "2026-01-01T00:00:00Z",
    }
    assert java_calls(site) == []


@pytest.mark.parametrize("mode", ["off", "refuse", "auto"])
def test_same_release_does_nothing(site: dict[str, str], mode: str) -> None:
    write_marker(Path(site["GERRIT_SITE"]), {"gerritVersion": NEW})
    proc = run_site_init({**site, "SITE_UPGRADE_MODE": mode})
    assert proc.returncode == 0, proc.stderr
    assert "warning" not in proc.stderr
    assert marker(site) == {"gerritVersion": NEW}
    assert java_calls(site) == []


def test_off_ignores_a_marker_without_a_version(site: dict[str, str]) -> None:
    write_marker(Path(site["GERRIT_SITE"]), {})
    proc = run_site_init(site)
    assert proc.returncode == 0, proc.stderr
    assert "warning" not in proc.stderr
    assert java_calls(site) == []


@XFAIL
def test_refuse_stops_with_the_message(site: dict[str, str]) -> None:
    proc = run_site_init({**site, "SITE_UPGRADE_MODE": "refuse"})
    assert proc.returncode != 0
    message = termination(site)
    for word in (OLD, NEW, "siteUpgrade.mode: auto"):
        assert word in message
    assert message.strip() in proc.stderr
    assert marker(site) is not None
    assert java_calls(site) == []
    assert "backups may run" not in proc.stdout


@XFAIL
def test_auto_refuses_an_older_image(site: dict[str, str]) -> None:
    write_marker(Path(site["GERRIT_SITE"]), {"gerritVersion": "3.14.0"})
    proc = run_site_init({**site, "SITE_UPGRADE_MODE": "auto"})
    assert proc.returncode != 0
    assert "does not support downgrades" in termination(site)
    assert "backup" in termination(site)
    assert marker(site) == {"gerritVersion": "3.14.0"}
    assert java_calls(site) == []


@XFAIL
def test_auto_upgrades_and_rewrites_the_marker(site: dict[str, str]) -> None:
    env = {
        **site,
        "SITE_UPGRADE_MODE": "auto",
        "SITE_UPGRADE_JAVA_HEAP": "-Xmx3g",
        "SITE_UPGRADE_CPU_LIMIT": "1500m",
    }
    proc = run_site_init(env)
    assert proc.returncode == 0, proc.stderr
    calls = java_calls(site)
    assert len(calls) == 2
    assert "-Xmx3g" in calls[0].split()
    assert calls[0].endswith(f"init --batch --no-auto-start -d {site['GERRIT_SITE']}")
    assert calls[1].endswith(f"reindex --threads 2 -d {site['GERRIT_SITE']}")
    record = marker(site)
    assert record is not None
    assert record["gerritVersion"] == NEW
    assert record["upgradedFrom"] == OLD
    assert record["initialisedAt"] != "2026-01-01T00:00:00Z"
    root = Path(site["GERRIT_SITE"])
    assert not (root / ".chart" / "upgrade-pending").exists()
    assert (root / ".chart" / "backup-lock" / "lock").is_file()
    # The render is installed again over what init wrote
    assert installed(site, "init.ran") == []
    # Nothing is installed again once the upgrade releases the lock
    assert installed(site, "reindex.ran") == ["yes"]
    assert "# reindexed" in (root / "bin" / "gerrit.sh").read_text("utf-8")
    assert proc.stdout.count("Prepared the Gerrit site") == 2
    for step in ("gerrit.war init", "gerrit.war reindex"):
        assert f"{step}: done in" in proc.stdout
    assert "Gerrit site in" in proc.stdout


@XFAIL
def test_auto_without_reindex(site: dict[str, str]) -> None:
    env = {**site, "SITE_UPGRADE_MODE": "auto", "SITE_UPGRADE_REINDEX": "false"}
    proc = run_site_init(env)
    assert proc.returncode == 0, proc.stderr
    calls = java_calls(site)
    assert len(calls) == 1
    assert " init " in calls[0]
    assert (marker(site) or {}).get("gerritVersion") == NEW


@XFAIL
@pytest.mark.parametrize("failing", ["init", "reindex"])
def test_failure_leaves_the_marker_absent(site: dict[str, str], failing: str) -> None:
    env = {**site, "SITE_UPGRADE_MODE": "auto", "STUB_FAIL": failing}
    proc = run_site_init(env)
    assert proc.returncode != 0
    assert marker(site) is None
    assert f"gerrit.war {failing} failed" in termination(site)
    # No other start runs Gerrit or a plain init on the half-upgraded
    # site: off and refuse both stop
    for mode in ("off", "refuse"):
        retry = run_site_init({**site, "SITE_UPGRADE_MODE": mode})
        assert retry.returncode != 0
        assert "did not finish" in retry.stderr
        assert marker(site) is None
    # The next start in auto retries the upgrade from OLD
    proc = run_site_init({**site, "SITE_UPGRADE_MODE": "auto"})
    assert proc.returncode == 0, proc.stderr
    record = marker(site) or {}
    assert (record.get("gerritVersion"), record.get("upgradedFrom")) == (NEW, OLD)
    assert not (Path(site["GERRIT_SITE"]) / ".chart" / "upgrade-pending").exists()


@XFAIL
def test_retry_refuses_an_image_older_than_the_target(site: dict[str, str]) -> None:
    war = Path(site["GERRIT_IMAGE_BIN"]) / "gerrit.war"
    pending = Path(site["GERRIT_SITE"]) / ".chart" / "upgrade-pending"
    auto = {**site, "SITE_UPGRADE_MODE": "auto"}
    write_war(war, "3.14.0")
    assert run_site_init({**auto, "STUB_FAIL": "reindex"}).returncode != 0
    # An image between the site's release and the target is refused
    write_war(war, NEW)
    calls = len(java_calls(site))
    proc = run_site_init(auto)
    assert proc.returncode != 0
    for word in (OLD, "3.14.0", NEW):
        assert word in termination(site)
    assert len(java_calls(site)) == calls
    assert marker(site) is None
    record = json.loads(pending.read_text(encoding="utf-8"))
    assert (record["from"], record["to"]) == (OLD, "3.14.0")
    # The target's image finishes the upgrade
    write_war(war, "3.14.0")
    proc = run_site_init(auto)
    assert proc.returncode == 0, proc.stderr
    done = marker(site) or {}
    assert (done.get("gerritVersion"), done.get("upgradedFrom")) == ("3.14.0", OLD)


@pytest.mark.parametrize(
    ("value", "expect"),
    [
        pytest.param("refuse", "refuse", marks=XFAIL),
        pytest.param("auto\n", "auto", marks=XFAIL),
        ("", "off"),
        pytest.param("on", "off", marks=XFAIL),
    ],
)
def test_override_sets_the_mode(site: dict[str, str], value: str, expect: str) -> None:
    mount_configmap(site, {"chart.siteUpgrade.mode": value})
    proc = run_site_init(site)
    assert (proc.returncode == 0) == (expect != "refuse"), proc.stderr
    upgraded = (marker(site) or {}).get("gerritVersion") == NEW
    assert upgraded == (expect == "auto")
    if value.strip() in ("off", "refuse", "auto"):
        assert f"Runtime override: set chart.siteUpgrade.mode = {expect}" in proc.stdout
    elif value:
        assert (
            "chart.siteUpgrade.mode is not off, refuse or auto; ignored" in proc.stderr
        )
    # Never written to gerrit.config
    assert installed(site, "chart.siteUpgrade.mode") == []
    assert "Runtime override: removed" not in proc.stdout


@XFAIL
@needs_helm
def test_default_render(tmp_path: Path) -> None:
    docs = render(tmp_path)
    env = site_init_env(docs)
    assert env["SITE_UPGRADE_MODE"] == "off"
    assert env["SITE_UPGRADE_REINDEX"] == "true"
    assert env["SITE_UPGRADE_CPU_LIMIT"] == ""
    init = [c for c in pod_spec(docs)["initContainers"] if c["name"] == "site-init"]
    main = [c for c in pod_spec(docs)["containers"] if c["name"] == "gerrit"]
    assert init[0]["resources"] == main[0]["resources"]
    scripts = [
        d
        for d in docs
        if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "gerrit-scripts"
    ]
    assert "site-upgrade.py" in scripts[0]["data"]


@XFAIL
@needs_helm
@pytest.mark.parametrize(("mode", "expected"), [(False, "off"), ("refuse", "refuse")])
def test_mode_renders(tmp_path: Path, mode: Any, expected: str) -> None:
    docs = render(tmp_path, {"siteUpgrade": {"mode": mode}})
    assert site_init_env(docs)["SITE_UPGRADE_MODE"] == expected


@XFAIL
@needs_helm
@pytest.mark.parametrize("mode", [True, "on", "force"])
def test_bad_mode_fails(tmp_path: Path, mode: Any) -> None:
    error = helm_fails(tmp_path, {"siteUpgrade": {"mode": mode}})
    assert "siteUpgrade.mode must be off, refuse or auto" in error


@XFAIL
@needs_helm
def test_upgrade_resources(tmp_path: Path) -> None:
    resources = {"limits": {"cpu": "2", "memory": "6Gi"}}
    docs = render(tmp_path, {"siteUpgrade": {"resources": resources, "reindex": False}})
    init = [c for c in pod_spec(docs)["initContainers"] if c["name"] == "site-init"]
    assert init[0]["resources"] == resources
    env = site_init_env(docs)
    assert env["SITE_UPGRADE_CPU_LIMIT"] == "2"
    assert env["SITE_UPGRADE_JAVA_HEAP"] == "-XX:MaxRAMPercentage=70.0"
    assert env["SITE_UPGRADE_REINDEX"] == "false"
