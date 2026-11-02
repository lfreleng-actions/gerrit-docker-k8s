# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for the Gerrit chart's hooks and gerrit-to-platform support.

Compares the ``gerrit.config`` the chart renders with ``hooks`` enabled,
and with a mixed set of plugin toggles, with
``tests/chart/golden/hooks-on.config`` and
``tests/chart/golden/plugins-mixed.config``, and checks the plugin list
``site-init`` copies, the hook scripts ConfigMap, the
gerrit-to-platform Secret and mounts, and the render failures for bad
values. Then runs ``site-init.sh`` against a scratch site, with stand-ins
for the image's files, to check the hooks directory and the INI it
installs. The default render must carry none of it.

After an intended change to the rendered output, regenerate the
goldens with ``UPDATE_GOLDEN=1 uv run pytest tests/chart`` and review
the diff.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from chart.test_render import CHART, GOLDEN, GOLDEN_HEADER, read_golden
from chart.test_replication import find, pod_spec, render, run_helm, site_init_env
from chart.test_saml import container, gerrit_config

NOT_YET = pytest.mark.xfail(strict=True, reason="hooks are not in the chart yet")

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="needs helm")

SITE_INIT = CHART / "files" / "scripts" / "site-init.sh"
G2P_HOOKS = ["patchset-created", "comment-added", "change-merged"]
STUB = '#!/bin/sh\necho "$*" >> /var/gerrit/logs/ref-updated.log\n'
# gerrit-to-platform finds the platform and repositories in the
# replication remotes, so g2p needs replication with one
REPLICATION: dict[str, Any] = {
    "enabled": True,
    "remotes": [{"name": "github", "url": "git@github.com:org/${name}.git"}],
}
HOOKS: dict[str, Any] = {
    "replication": REPLICATION,
    "hooks": {
        "enabled": True,
        "syncHookTimeout": 45,
        "scripts": {"ref-updated": STUB},
        "g2p": {"enabled": True, "existingSecret": "g2p-ini"},
    },
}
MIXED: dict[str, Any] = {
    "pluginCatalogue": {"plugins": ["events-log", "hooks", "webhooks"]},
    "replication": REPLICATION,
    "hooks": {"enabled": True, "path": "site-hooks"},
    "plugins": {
        "its-jira": {
            "enabled": True,
            "url": "https://jira.example.org",
            "association": "SUGGESTED",
        }
    },
}


def plugins(docs: list[Any]) -> list[str]:
    """Return the plugins site-init copies from the catalogue."""
    return str(site_init_env(docs)["GERRIT_PLUGINS"]).split()


def assert_golden(config: str, name: str) -> None:
    """Compare a rendered config with ``tests/chart/golden/<name>``."""
    golden = GOLDEN / name
    if os.environ.get("UPDATE_GOLDEN"):
        golden.write_text(GOLDEN_HEADER + config, encoding="utf-8")
    assert config == read_golden(golden)


def helm_fails(tmp_path: Path, values: dict[str, Any]) -> str:
    """Render with *values*, expecting failure; return helm's stderr."""
    path = tmp_path / "values.yaml"
    path.write_text(yaml.safe_dump(values), encoding="utf-8")
    proc = run_helm("-f", str(path))
    assert proc.returncode != 0
    return str(proc.stderr)


@NOT_YET
def test_disabled_renders_none_of_it(tmp_path: Path) -> None:
    docs = render(tmp_path)
    config = gerrit_config(docs)
    assert "[hooks]" not in config
    assert "its-jira" not in config
    assert plugins(docs) == []
    env = site_init_env(docs)
    assert env["GERRIT_HOOKS_PATH"] == ""
    assert env["GERRIT_G2P_HOOKS"] == ""
    assert "GERRIT_G2P_INI" not in env
    assert find(docs, "ConfigMap", "gerrit-hooks") is None
    assert find(docs, "Secret", "gerrit-g2p") is None
    names = {v["name"] for v in pod_spec(docs)["volumes"]}
    assert not names & {"hook-scripts", "g2p"}
    gerrit_env = [e["name"] for e in container(docs, "gerrit")["env"]]
    assert "XDG_CONFIG_HOME" not in gerrit_env


@NOT_YET
def test_hooks_config_matches_golden(tmp_path: Path) -> None:
    assert_golden(gerrit_config(render(tmp_path, HOOKS)), "hooks-on.config")


@NOT_YET
def test_mixed_toggles_match_golden(tmp_path: Path) -> None:
    docs = render(tmp_path, MIXED)
    assert_golden(gerrit_config(docs), "plugins-mixed.config")
    assert sorted(plugins(docs)) == [
        "events-log",
        "hooks",
        "its-jira",
        "replication",
        "webhooks",
    ]
    assert site_init_env(docs)["GERRIT_HOOKS_PATH"] == "site-hooks"


@NOT_YET
def test_hooks_adds_only_the_hooks_plugin(tmp_path: Path) -> None:
    docs = render(tmp_path, {"hooks": {"enabled": True}})
    assert plugins(docs) == ["hooks"]
    env = site_init_env(docs)
    assert env["GERRIT_HOOKS_PATH"] == "hooks"
    assert env["GERRIT_G2P_HOOKS"] == ""
    assert find(docs, "ConfigMap", "gerrit-hooks") is None
    config = gerrit_config(docs)
    assert "[hooks]\n\tpath = /var/gerrit/hooks\n\tsyncHookTimeout = 30\n" in config


@NOT_YET
def test_scripts_render_into_a_config_map(tmp_path: Path) -> None:
    docs = render(tmp_path, HOOKS)
    hooks = find(docs, "ConfigMap", "gerrit-hooks")
    assert hooks is not None
    assert hooks["data"] == {"ref-updated": STUB}
    init = container(docs, "site-init")
    assert {
        "name": "hook-scripts",
        "mountPath": "/hook-scripts",
        "readOnly": True,
    } in init["volumeMounts"]
    volumes = [v for v in pod_spec(docs)["volumes"] if v["name"] == "hook-scripts"]
    assert volumes == [{"name": "hook-scripts", "configMap": {"name": "gerrit-hooks"}}]


@NOT_YET
def test_g2p_links_hooks_and_mounts_the_ini(tmp_path: Path) -> None:
    docs = render(tmp_path, HOOKS)
    env = site_init_env(docs)
    assert env["GERRIT_G2P_HOOKS"].split() == G2P_HOOKS
    assert env["GERRIT_G2P_INI"] == "/g2p/gerrit_to_platform.ini"
    assert find(docs, "Secret", "gerrit-g2p") is None
    volumes = [v for v in pod_spec(docs)["volumes"] if v["name"] == "g2p"]
    assert volumes == [
        {"name": "g2p", "secret": {"secretName": "g2p-ini", "defaultMode": 0o400}}
    ]
    init = container(docs, "site-init")
    assert {"name": "g2p", "mountPath": "/g2p", "readOnly": True} in init[
        "volumeMounts"
    ]
    gerrit = container(docs, "gerrit")
    assert all(m["name"] != "g2p" for m in gerrit["volumeMounts"])
    assert {"name": "XDG_CONFIG_HOME", "value": "/var/gerrit/.config"} in gerrit["env"]


@NOT_YET
def test_g2p_chart_creates_an_empty_secret(tmp_path: Path) -> None:
    values = {
        "replication": REPLICATION,
        "hooks": {"enabled": True, "g2p": {"enabled": True}},
    }
    secret = find(render(tmp_path, values), "Secret", "gerrit-g2p")
    assert secret is not None
    assert secret["data"] == {"gerrit_to_platform.ini": ""}
    annotations = secret["metadata"]["annotations"]
    assert annotations["helm.sh/resource-policy"] == "keep"


@NOT_YET
def test_g2p_hooks_may_be_narrowed(tmp_path: Path) -> None:
    values = {
        "replication": REPLICATION,
        "hooks": {
            "enabled": True,
            "g2p": {"enabled": True, "hooks": ["change-merged", "change-merged"]},
        },
    }
    assert site_init_env(render(tmp_path, values))["GERRIT_G2P_HOOKS"] == (
        "change-merged"
    )


@NOT_YET
@pytest.mark.parametrize(
    ("values", "message"),
    [
        (
            {"hooks": {"enabled": False, "g2p": {"enabled": True}}},
            "hooks.g2p.enabled needs hooks.enabled",
        ),
        (
            {"hooks": {"enabled": True, "g2p": {"enabled": True}}},
            "hooks.g2p.enabled needs replication.enabled with a remote",
        ),
        (
            {
                "replication": {"enabled": True},
                "hooks": {"enabled": True, "g2p": {"enabled": True}},
            },
            "hooks.g2p.enabled needs replication.enabled with a remote",
        ),
        (
            {
                "replication": REPLICATION,
                "hooks": {
                    "enabled": True,
                    "g2p": {"enabled": True, "hooks": ["ref-updated"]},
                },
            },
            'gerrit-to-platform has no "ref-updated" hook',
        ),
        (
            {
                "replication": REPLICATION,
                "hooks": {
                    "enabled": True,
                    "scripts": {"change-merged": STUB},
                    "g2p": {"enabled": True},
                },
            },
            '"change-merged" is also in hooks.g2p.hooks',
        ),
        (
            {"hooks": {"enabled": True, "scripts": {"Bad_Name": STUB}}},
            '"Bad_Name" is not a hook name',
        ),
        ({"hooks": {"enabled": True, "path": "../hooks"}}, "must be a relative path"),
        ({"hooks": {"enabled": True, "path": "/srv/hooks"}}, "must be a relative"),
        ({"hooks": {"enabled": True, "path": "a/./b"}}, "must be a relative path"),
        ({"hooks": {"enabled": True, "path": ".ssh"}}, "must be a relative path"),
        ({"hooks": {"enabled": True, "path": ""}}, "must be a relative path"),
        ({"hooks": {"enabled": True, "path": "etc"}}, "site directory"),
        ({"hooks": {"enabled": True, "path": "git/hooks"}}, "site directory"),
    ],
)
def test_invalid_values_fail(
    tmp_path: Path, values: dict[str, Any], message: str
) -> None:
    assert message in helm_fails(tmp_path, values)


@NOT_YET
def test_disabled_hooks_ignore_a_bad_path(tmp_path: Path) -> None:
    values = {"hooks": {"enabled": False, "path": "etc"}}
    assert site_init_env(render(tmp_path, values))["GERRIT_HOOKS_PATH"] == ""


@pytest.mark.parametrize("values", [HOOKS, MIXED])
def test_helm_lint(tmp_path: Path, values: dict[str, Any]) -> None:
    path = tmp_path / "values.yaml"
    path.write_text(yaml.safe_dump(values), encoding="utf-8")
    proc = subprocess.run(
        ["helm", "lint", str(CHART), "-f", str(path)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.fixture
def image(tmp_path: Path) -> dict[str, str]:
    """Stand-ins for the image's files, and site-init's environment."""
    bin_dir = tmp_path / "image-bin"
    bin_dir.mkdir()
    for name in ("gerrit.war", "gerrit.sh"):
        (bin_dir / name).write_text("", encoding="utf-8")
    tools = tmp_path / "tools-bin"
    tools.mkdir()
    for name in G2P_HOOKS:
        (tools / name).write_text("#!/bin/sh\n", encoding="utf-8")
    rendered = tmp_path / "rendered"
    rendered.mkdir()
    (rendered / "gerrit.config").write_text("[gerrit]\n\tbasePath = git\n")
    for name in ("jgit.config", "notedb.config"):
        (rendered / name).write_text("", encoding="utf-8")
    scripts = tmp_path / "hook-scripts"
    scripts.mkdir()
    (scripts / "ref-updated").write_text(STUB, encoding="utf-8")
    ini = tmp_path / "g2p.ini"
    ini.write_text("[github.com]\ntoken = fixed-test-token\n", encoding="utf-8")
    ini.chmod(0o440)
    site = tmp_path / "site"
    (site / "git" / "All-Projects.git").mkdir(parents=True)
    (site / "etc").mkdir()
    return {
        "GERRIT_SITE": str(site),
        "GERRIT_IMAGE_BIN": str(bin_dir),
        "GERRIT_RENDERED_CONFIG": str(rendered),
        "GERRIT_CATALOGUE": str(tmp_path / "catalogue"),
        "GERRIT_TOOLS_BIN": str(tools),
        "GERRIT_HOOK_SCRIPTS": str(scripts),
        "GERRIT_HOOKS_PATH": "hooks",
        "GERRIT_G2P_HOOKS": " ".join(G2P_HOOKS),
        "GERRIT_G2P_INI": str(ini),
    }


def run_site_init(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run site-init.sh with only the given GERRIT_ variables set."""
    base = {
        k: v for k, v in os.environ.items() if not k.startswith(("GERRIT_", "GIT_"))
    }
    return subprocess.run(
        ["sh", str(SITE_INIT)],
        env={**base, "HOME": env["GERRIT_SITE"], **env},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@NOT_YET
def test_site_init_installs_hooks_and_ini(image: dict[str, str]) -> None:
    site = Path(image["GERRIT_SITE"])
    stale = site / "hooks" / "change-abandoned"
    stale.parent.mkdir()
    stale.write_text("#!/bin/sh\n", encoding="utf-8")
    proc = run_site_init(image)
    assert proc.returncode == 0, proc.stderr
    hooks = site / "hooks"
    assert sorted(p.name for p in hooks.iterdir()) == sorted(
        [*G2P_HOOKS, "ref-updated"]
    )
    tools = Path(image["GERRIT_TOOLS_BIN"])
    for name in G2P_HOOKS:
        assert os.readlink(hooks / name) == str(tools / name)
    script = hooks / "ref-updated"
    assert script.read_text(encoding="utf-8") == STUB
    assert stat.S_IMODE(script.stat().st_mode) == 0o755
    g2p = site / ".config" / "gerrit_to_platform"
    ini = g2p / "gerrit_to_platform.ini"
    assert ini.read_text(encoding="utf-8") == Path(image["GERRIT_G2P_INI"]).read_text(
        encoding="utf-8"
    )
    assert stat.S_IMODE(ini.stat().st_mode) == 0o600
    assert stat.S_IMODE(g2p.stat().st_mode) == 0o700
    assert os.readlink(g2p / "replication.config") == (
        "/var/gerrit/etc/replication.config"
    )


@NOT_YET
def test_site_init_removes_what_is_disabled(image: dict[str, str]) -> None:
    assert run_site_init(image).returncode == 0
    site = Path(image["GERRIT_SITE"])
    env = {**image, "GERRIT_G2P_HOOKS": "", "GERRIT_HOOK_SCRIPTS": "/nonexistent"}
    del env["GERRIT_G2P_INI"]
    proc = run_site_init(env)
    assert proc.returncode == 0, proc.stderr
    assert list((site / "hooks").iterdir()) == []
    g2p = site / ".config" / "gerrit_to_platform"
    assert list(g2p.iterdir()) == []


@NOT_YET
def test_site_init_leaves_hooks_alone_when_disabled(image: dict[str, str]) -> None:
    site = Path(image["GERRIT_SITE"])
    kept = site / "hooks" / "ref-updated"
    kept.parent.mkdir()
    kept.write_text("#!/bin/sh\n", encoding="utf-8")
    proc = run_site_init({**image, "GERRIT_HOOKS_PATH": ""})
    assert proc.returncode == 0, proc.stderr
    assert [p.name for p in (site / "hooks").iterdir()] == ["ref-updated"]


@NOT_YET
def test_site_init_fails_on_a_missing_entry_point(image: dict[str, str]) -> None:
    proc = run_site_init({**image, "GERRIT_G2P_HOOKS": "ref-updated"})
    assert proc.returncode != 0
    assert "ref-updated is not in the image" in proc.stderr
