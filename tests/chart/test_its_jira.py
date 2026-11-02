# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for the Gerrit chart's Jira integration through its-jira.

Compares the ``gerrit.config`` and ``etc/its/actions-its-jira.config``
the chart renders with ``plugins.its-jira`` enabled with
``tests/chart/golden/its-jira-on.config`` and
``tests/chart/golden/its-jira-actions.config``, and checks the plugins
``site-init`` copies, the Secret, the credentials ``render-config.sh``
writes to ``secure.config`` and the NetworkPolicy egress to the Jira
URL. The default render must carry none of it.

After an intended change to the rendered output, regenerate the
goldens with ``UPDATE_GOLDEN=1 uv run pytest tests/chart`` and review
the diff.
"""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from chart.test_hooks import assert_golden, helm_fails, plugins
from chart.test_render import CHART, IMAGE_TAG
from chart.test_replication import egress_ports, find, render
from chart.test_saml import container, gerrit_config
from chart.test_secure_config import SECRET_ENV, run_script

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="needs helm")

CREDENTIAL_ENV = ("GERRIT_ITS_JIRA_USERNAME", "GERRIT_ITS_JIRA_PASSWORD")
EXTRA_RULE = '[rule "resolve"]\n\tevent-type = comment-added\n\taction = In Review\n'


def jira_values(**extra: Any) -> dict[str, Any]:
    """Return values enabling its-jira, with *extra* plugin values."""
    jira = {
        "enabled": True,
        "url": "https://jira.example.org",
        "association": "MANDATORY",
        "commentOnChangeCreated": True,
        "commentOnPatchSetCreated": False,
        "commentOnChangeMerged": True,
        "commentOnChangeAbandoned": True,
        "extraConfig": EXTRA_RULE,
        **extra,
    }
    return {"plugins": {"its-jira": jira}}


def config_data(docs: list[Any]) -> dict[str, str]:
    """Return the data of the rendered gerrit-config ConfigMap."""
    config = find(docs, "ConfigMap", "gerrit-config")
    assert config is not None
    data: dict[str, str] = config["data"]
    return data


def test_disabled_renders_none_of_it(tmp_path: Path) -> None:
    docs = render(tmp_path)
    config = gerrit_config(docs)
    assert "its-jira" not in config
    assert "[plugins]\n\tallowRemoteAdmin = false\n" in config
    assert "actions-its-jira.config" not in config_data(docs)
    assert find(docs, "Secret", "gerrit-its-jira") is None
    env = [e["name"] for e in container(docs, "render-config")["env"]]
    assert not set(CREDENTIAL_ENV) & set(env)
    assert not {"its-base", "its-jira"} & set(plugins(docs))


def test_enabled_config_matches_golden(tmp_path: Path) -> None:
    docs = render(tmp_path, jira_values())
    assert_golden(gerrit_config(docs), "its-jira-on.config")
    assert_golden(
        config_data(docs)["actions-its-jira.config"], "its-jira-actions.config"
    )


def test_enabled_adds_only_its_jira(tmp_path: Path) -> None:
    # its-base is compiled into its-jira.jar, not a plugin of its own
    assert plugins(render(tmp_path, jira_values())) == ["its-jira", "healthcheck"]


def test_plugin_values_stay_out_of_the_plugins_section(tmp_path: Path) -> None:
    config = gerrit_config(render(tmp_path, jira_values()))
    assert '[plugins "its-jira"]' not in config
    assert "commentOn" not in config
    assert "extraConfig" not in config


def test_patch_set_rule_covers_change_created(tmp_path: Path) -> None:
    values = jira_values(
        commentOnPatchSetCreated=True,
        commentOnChangeMerged=False,
        commentOnChangeAbandoned=False,
        extraConfig="",
    )
    actions = config_data(render(tmp_path, values))["actions-its-jira.config"]
    assert actions == (
        '[rule "comment-on-patchset-created"]\n'
        "\tevent-type = patchset-created\n"
        "\taction = add-standard-comment\n"
    )


def test_no_rules_render_an_empty_file(tmp_path: Path) -> None:
    values = jira_values(
        commentOnChangeCreated=False,
        commentOnChangeMerged=False,
        commentOnChangeAbandoned=False,
        extraConfig="",
    )
    assert config_data(render(tmp_path, values))["actions-its-jira.config"] == ""


def test_url_is_left_out_when_unset(tmp_path: Path) -> None:
    docs = render(tmp_path, jira_values(url=""))
    assert "[its-jira]" not in gerrit_config(docs)
    assert egress_ports(docs) == {53, 443}


def test_chart_creates_an_empty_secret(tmp_path: Path) -> None:
    secret = find(render(tmp_path, jira_values()), "Secret", "gerrit-its-jira")
    assert secret is not None
    assert secret["data"] == {"username": "", "password": ""}
    annotations = secret["metadata"]["annotations"]
    assert annotations["helm.sh/resource-policy"] == "keep"


def test_existing_secret_is_used(tmp_path: Path) -> None:
    docs = render(tmp_path, jira_values(existingSecret="jira-creds"))
    assert find(docs, "Secret", "gerrit-its-jira") is None
    env = container(docs, "render-config")["env"]
    refs = {
        e["name"]: e["valueFrom"]["secretKeyRef"]
        for e in env
        if e["name"] in CREDENTIAL_ENV
    }
    assert refs == {
        "GERRIT_ITS_JIRA_USERNAME": {"name": "jira-creds", "key": "username"},
        "GERRIT_ITS_JIRA_PASSWORD": {"name": "jira-creds", "key": "password"},
    }


@pytest.mark.parametrize(
    ("url", "ports"),
    [
        ("https://jira.example.org", set()),
        ("https://jira.example.org:8443/jira", {8443}),
        ("http://jira.example.org", {80}),
    ],
)
def test_egress_reaches_the_url(tmp_path: Path, url: str, ports: set[int]) -> None:
    docs = render(tmp_path, jira_values(url=url))
    assert egress_ports(docs) == {53, 443} | ports


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"url": "jira.example.org"}, "must be an http"),
        ({"association": "ALWAYS"}, "must be MANDATORY, SUGGESTED or OPTIONAL"),
    ],
)
def test_invalid_values_fail(
    tmp_path: Path, extra: dict[str, Any], message: str
) -> None:
    assert message in helm_fails(tmp_path, jira_values(**extra))


def test_helm_lint(tmp_path: Path) -> None:
    path = tmp_path / "values.yaml"
    path.write_text(yaml.safe_dump(jira_values()), encoding="utf-8")
    proc = subprocess.run(
        ["helm", "lint", str(CHART), *IMAGE_TAG, "-f", str(path)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.fixture
def site(tmp_path: Path) -> Path:
    """A scratch site directory with an etc/ directory."""
    (tmp_path / "etc").mkdir()
    return tmp_path


def test_credentials_land_in_private_secure_config(site: Path) -> None:
    env = {
        **SECRET_ENV,
        "GERRIT_ITS_JIRA_USERNAME": "gerrit-bot",
        "GERRIT_ITS_JIRA_PASSWORD": "fixed jira token; with # marks",
    }
    proc = run_script(site, env)
    assert proc.returncode == 0, proc.stderr
    secure = site / "etc" / "secure.config"
    assert stat.S_IMODE(secure.stat().st_mode) == 0o600
    read = subprocess.run(
        ["git", "config", "-f", str(secure), "--get-regexp", r"^its-jira\."],
        capture_output=True,
        text=True,
        check=True,
    )
    assert read.stdout.splitlines() == [
        "its-jira.username gerrit-bot",
        "its-jira.password fixed jira token; with # marks",
    ]


def test_empty_credentials_are_left_out(site: Path) -> None:
    env = dict.fromkeys(CREDENTIAL_ENV, "")
    proc = run_script(site, {**SECRET_ENV, **env})
    assert proc.returncode == 0, proc.stderr
    secure = (site / "etc" / "secure.config").read_text(encoding="utf-8")
    assert "[its-jira]" not in secure
