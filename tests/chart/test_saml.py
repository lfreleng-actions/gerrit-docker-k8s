# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for the Gerrit chart's SAML single sign-on support.

Compares the ``gerrit.config`` the chart renders with ``auth.saml``
enabled, once per identity provider metadata source, with
``tests/chart/golden/saml-metadata-<source>.config``, and checks the
Secret, the mounts, the library, the NetworkPolicy egress and the
passwords ``render-config.sh`` writes to ``secure.config``. The default
render must carry none of it. With ``memberOfAttr`` set, the render
must match ``tests/chart/golden/saml-member-of-attr.config``. No test needs a real keystore: the chart
never reads the keystore's contents.

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
from chart.test_replication import (
    egress_ports,
    find,
    pod_spec,
    render,
    run_helm,
    site_init_env,
)
from chart.test_secure_config import SECRET_ENV, run_script

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="needs helm")

FILTER = "com.googlesource.gerrit.plugins.saml.SamlWebFilter"
MODULE = "com.googlesource.gerrit.plugins.saml.Module"
PASSWORD_ENV = ("GERRIT_SAML_KEYSTORE_PASSWORD", "GERRIT_SAML_PRIVATE_KEY_PASSWORD")
SECRET_KEYS = [
    "samlKeystore.jks",
    "keystore-password",
    "private-key-password",
    "idp-metadata.xml",
]
SAML_ATTRS: dict[str, Any] = {
    "userNameAttr": "uid",
    "displayNameAttr": "displayName",
    "emailAddressAttr": "mail",
    "firstNameAttr": None,
    "lastNameAttr": "",
}
SOURCES: dict[str, dict[str, Any]] = {
    "url": {"metadataUrl": "https://idp.example.org/app/sso/saml/metadata"},
    "secret": {"metadataFromSecret": True},
}


def saml_values(source: str, **extra: Any) -> dict[str, Any]:
    """Return values enabling SAML with the given metadata *source*."""
    saml = {"enabled": True, **SAML_ATTRS, **SOURCES[source], **extra}
    return {"auth": {"saml": saml}}


def gerrit_config(docs: list[Any]) -> str:
    """Return the rendered ``gerrit.config``."""
    config = find(docs, "ConfigMap", "gerrit-config")
    assert config is not None
    text: str = config["data"]["gerrit.config"]
    return text


def container(docs: list[Any], name: str) -> dict[str, Any]:
    """Return the Gerrit pod's container or init container *name*."""
    spec = pod_spec(docs)
    found: list[dict[str, Any]] = [
        c for c in spec["containers"] + spec["initContainers"] if c["name"] == name
    ]
    assert len(found) == 1
    return found[0]


def saml_mounts(docs: list[Any]) -> list[dict[str, Any]]:
    """Return every SAML volume mount in the Gerrit pod."""
    spec = pod_spec(docs)
    return [
        m
        for c in spec["containers"] + spec["initContainers"]
        for m in c.get("volumeMounts", [])
        if m["name"] == "saml"
    ]


def test_disabled_renders_none_of_it(tmp_path: Path) -> None:
    docs = render(tmp_path)
    config = gerrit_config(docs)
    assert "[saml]" not in config
    assert "filterClass" not in config
    assert "type = DEVELOPMENT_BECOME_ANY_ACCOUNT" in config
    assert find(docs, "Secret", "gerrit-saml") is None
    assert all(v["name"] != "saml" for v in pod_spec(docs)["volumes"])
    assert saml_mounts(docs) == []
    assert "saml" not in site_init_env(docs)["GERRIT_LIBS"].split()
    env = [e["name"] for e in container(docs, "render-config")["env"]]
    assert not set(PASSWORD_ENV) & set(env)
    assert egress_ports(docs) == {53, 443}


@pytest.mark.parametrize("source", sorted(SOURCES))
def test_enabled_config_matches_golden(tmp_path: Path, source: str) -> None:
    config = gerrit_config(render(tmp_path, saml_values(source)))
    golden = GOLDEN / f"saml-metadata-{source}.config"
    if os.environ.get("UPDATE_GOLDEN"):
        golden.write_text(GOLDEN_HEADER + config, encoding="utf-8")
    assert config == read_golden(golden)


def test_member_of_attr_defaults_to_null() -> None:
    values = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
    saml = values["auth"]["saml"]
    assert "memberOfAttr" in saml
    assert saml["memberOfAttr"] is None


def test_member_of_attr_matches_golden(tmp_path: Path) -> None:
    values = saml_values("url", memberOfAttr="memberOf")
    config = gerrit_config(render(tmp_path, values))
    golden = GOLDEN / "saml-member-of-attr.config"
    if os.environ.get("UPDATE_GOLDEN"):
        golden.write_text(GOLDEN_HEADER + config, encoding="utf-8")
    assert config == read_golden(golden)
    path = tmp_path / "gerrit.config"
    path.write_text(config, encoding="utf-8")
    read = subprocess.run(
        ["git", "config", "-f", str(path), "saml.memberOfAttr"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert read.stdout == "memberOf\n"


@pytest.mark.parametrize("member_of", [None, ""])
@pytest.mark.parametrize("source", sorted(SOURCES))
def test_member_of_attr_unset_renders_no_key(
    tmp_path: Path, source: str, member_of: str | None
) -> None:
    config = gerrit_config(
        render(tmp_path, saml_values(source, memberOfAttr=member_of))
    )
    assert "[saml]" in config
    assert "memberofattr" not in config.lower()


@pytest.mark.parametrize("source", sorted(SOURCES))
def test_enabled_installs_the_module(tmp_path: Path, source: str) -> None:
    config = gerrit_config(render(tmp_path, saml_values(source)))
    gerrit = config.split("[gerrit]\n", 1)[1].split("\n[", 1)[0]
    assert f"\tinstallModule = {MODULE}" in gerrit.splitlines()
    assert "httpdisplaynameheader" not in config.lower()


@pytest.mark.parametrize(
    "values",
    [
        {"auth": {"httpDisplaynameHeader": "X-Name"}},
        {"auth": {"httpdisplaynameheader": "X-Name"}},
        {"auth": {"httpDisplaynameHeader": ["X-Name"]}},
        {"auth": {"httpDisplaynameHeader": [""]}},
        {"auth": {"httpDisplaynameHeader": False}},
        {"gerrit": {"extraConfig": "[auth]\n\thttpDisplayNameHeader = X-Name\n"}},
        {"gerrit": {"extraConfig": "[auth] httpDisplaynameHeader = X-Name\n"}},
        {"gerrit": {"extraConfig": "[AUTH]\n  HTTPDISPLAYNAMEHEADER\n"}},
        {"gerrit": {"extraConfig": "[auth]\n\thttpDisplaynameHeader=X-Name # set\n"}},
        {"gerrit": {"extraConfig": '[auth "x"]\n[auth]\nhttpDisplaynameHeader =\n'}},
        {"gerrit": {"extraConfig": "[auth]\n\u2003httpDisplaynameHeader = X-Name\n"}},
        {"gerrit": {"extraConfig": "[auth]\u2003httpDisplaynameHeader\u2003= X\n"}},
        {
            "gerrit": {
                "extraConfig": "[auth]\n\tlogoutUrl = a\\\\\nhttpDisplaynameHeader = X\n"
            }
        },
    ],
)
def test_display_name_header_is_refused(tmp_path: Path, values: dict[str, Any]) -> None:
    merged = saml_values("url")
    for key, value in values.items():
        merged.setdefault(key, {}).update(value)
    path = tmp_path / "values.yaml"
    path.write_text(yaml.safe_dump(merged), encoding="utf-8")
    proc = run_helm("-f", str(path))
    assert proc.returncode != 0
    assert "httpDisplaynameHeader is not compatible with SAML" in proc.stderr


@pytest.mark.parametrize("header", [None, "", []])
def test_empty_display_name_header_renders(tmp_path: Path, header: Any) -> None:
    values = saml_values("url")
    values["auth"]["httpDisplaynameHeader"] = header
    config = gerrit_config(render(tmp_path, values))
    assert f"\tfilterClass = {FILTER}\n" in config
    assert "httpdisplaynameheader" not in config.lower()


@pytest.mark.parametrize(
    "extra",
    [
        "# [auth] httpDisplaynameHeader = X-Name\n",
        "[auth]\n; httpDisplaynameHeader = X-Name\n",
        "[auth]\n\t# httpDisplaynameHeader = X-Name\n",
        '[plugin "x"]\n\tnote = "a] httpDisplaynameHeader = X-Name"\n',
        '[plugin "x"]\n\tnote = a ; [auth] httpDisplaynameHeader = X\n',
        '[auth "example"]\n\thttpDisplaynameHeader = X-Name\n',
        "[auth.example]\n\thttpDisplaynameHeader = X-Name\n",
        "[sendemail]\n\thttpDisplaynameHeader = X-Name\n",
        "httpDisplaynameHeader = X-Name\n",
        "[auth]\n\tlogoutUrl = a \\\nhttpDisplaynameHeader = X-Name\n",
        '[auth]\n\tlogoutUrl = "a \\\nhttpDisplaynameHeader = \\\nb"\n',
    ],
)
def test_inactive_display_name_header_renders(tmp_path: Path, extra: str) -> None:
    values = saml_values("url")
    values["gerrit"] = {"extraConfig": extra}
    config = gerrit_config(render(tmp_path, values))
    assert f"\tfilterClass = {FILTER}\n" in config
    assert extra.rstrip("\n") in config


def test_display_name_header_is_kept_without_saml(tmp_path: Path) -> None:
    values = {"auth": {"httpDisplaynameHeader": "X-Name"}}
    assert "\thttpDisplaynameHeader = X-Name\n" in gerrit_config(
        render(tmp_path, values)
    )


def test_max_auth_lifetime_attr_is_not_a_key() -> None:
    files = [p for d in (CHART, GOLDEN) for p in d.rglob("*") if p.is_file()]
    assert files
    named = [p for p in files if b"maxAuthLifetimeAttr" in p.read_bytes()]
    assert named == []


def test_enabled_overrides_auth_type(tmp_path: Path) -> None:
    values = saml_values("url")
    values["auth"]["type"] = "LDAP"
    config = gerrit_config(render(tmp_path, values))
    assert "\ttype = HTTP\n" in config
    assert "LDAP" not in config
    assert f"\tfilterClass = {FILTER}\n" in config


def test_entity_id_may_be_set(tmp_path: Path) -> None:
    values = saml_values("url", serviceProviderEntityId="urn:example:gerrit")
    path = tmp_path / "gerrit.config"
    path.write_text(gerrit_config(render(tmp_path, values)), encoding="utf-8")
    read = subprocess.run(
        ["git", "config", "-f", str(path), "saml.serviceProviderEntityId"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert read.stdout == "urn:example:gerrit\n"


@pytest.mark.parametrize(
    ("source", "files"),
    [
        ("url", {"samlKeystore.jks": "samlKeystore.jks"}),
        (
            "secret",
            {
                "samlKeystore.jks": "samlKeystore.jks",
                "saml-idp-metadata.xml": "idp-metadata.xml",
            },
        ),
    ],
)
def test_secret_is_mounted_read_only_into_etc(
    tmp_path: Path, source: str, files: dict[str, str]
) -> None:
    docs = render(tmp_path, saml_values(source))
    gerrit = container(docs, "gerrit")
    mounts = [m for m in gerrit["volumeMounts"] if m["name"] == "saml"]
    assert mounts == [
        {
            "name": "saml",
            "mountPath": f"/var/gerrit/etc/{name}",
            "subPath": key,
            "readOnly": True,
        }
        for name, key in files.items()
    ]
    assert len(saml_mounts(docs)) == len(mounts)
    volume = [v for v in pod_spec(docs)["volumes"] if v["name"] == "saml"]
    assert volume == [
        {"name": "saml", "secret": {"secretName": "gerrit-saml", "defaultMode": 0o400}}
    ]


def test_chart_creates_an_empty_secret(tmp_path: Path) -> None:
    secret = find(render(tmp_path, saml_values("url")), "Secret", "gerrit-saml")
    assert secret is not None
    assert secret["data"] == dict.fromkeys(SECRET_KEYS, "")
    annotations = secret["metadata"]["annotations"]
    assert annotations["helm.sh/resource-policy"] == "keep"


def test_existing_secret_is_used(tmp_path: Path) -> None:
    docs = render(tmp_path, saml_values("secret", existingSecret="sso"))
    assert find(docs, "Secret", "gerrit-saml") is None
    volume = [v for v in pod_spec(docs)["volumes"] if v["name"] == "saml"]
    assert volume[0]["secret"]["secretName"] == "sso"
    env = container(docs, "render-config")["env"]
    refs = {
        e["name"]: e["valueFrom"]["secretKeyRef"]
        for e in env
        if e["name"] in PASSWORD_ENV
    }
    assert refs == {
        "GERRIT_SAML_KEYSTORE_PASSWORD": {"name": "sso", "key": "keystore-password"},
        "GERRIT_SAML_PRIVATE_KEY_PASSWORD": {
            "name": "sso",
            "key": "private-key-password",
        },
    }


def test_enabled_adds_the_library_once(tmp_path: Path) -> None:
    values: dict[str, Any] = {
        **saml_values("url"),
        "pluginCatalogue": {"libs": ["saml", "x"]},
    }
    listed = site_init_env(render(tmp_path, values))["GERRIT_LIBS"].split()
    assert sorted(listed) == ["saml", "x"]
    alone = site_init_env(render(tmp_path, saml_values("url")))["GERRIT_LIBS"]
    assert alone.split() == ["saml"]


@pytest.mark.parametrize(
    ("url", "ports"),
    [
        ("https://idp.example.org/metadata", set()),
        ("https://idp.example.org:8443/metadata", {8443}),
        ("http://idp.example.org/metadata", {80}),
        ("https://idp.example.org?metadata=1", set()),
        ("http://idp.example.org#metadata", {80}),
        ("https://idp.example.org:8443?metadata=1", {8443}),
        ("http://idp.example.org:8080#metadata", {8080}),
    ],
)
def test_egress_reaches_the_metadata_url(
    tmp_path: Path, url: str, ports: set[int]
) -> None:
    docs = render(tmp_path, saml_values("url", metadataUrl=url))
    assert egress_ports(docs) == {53, 443} | ports


def test_metadata_from_secret_needs_no_egress(tmp_path: Path) -> None:
    assert egress_ports(render(tmp_path, saml_values("secret"))) == {53, 443}


@pytest.mark.parametrize(
    ("saml", "message"),
    [
        ({}, "set metadataUrl or metadataFromSecret"),
        (
            {"metadataUrl": "https://idp.example.org/m", "metadataFromSecret": True},
            "not both",
        ),
        ({"metadataUrl": "idp.example.org/m"}, "must be an http"),
    ],
)
def test_invalid_metadata_fails(
    tmp_path: Path, saml: dict[str, Any], message: str
) -> None:
    path = tmp_path / "values.yaml"
    path.write_text(
        yaml.safe_dump({"auth": {"saml": {"enabled": True, **saml}}}),
        encoding="utf-8",
    )
    proc = run_helm("-f", str(path))
    assert proc.returncode != 0
    assert message in proc.stderr


@pytest.mark.parametrize("source", sorted(SOURCES))
def test_helm_lint(tmp_path: Path, source: str) -> None:
    path = tmp_path / "values.yaml"
    path.write_text(yaml.safe_dump(saml_values(source)), encoding="utf-8")
    proc = subprocess.run(
        ["helm", "lint", str(CHART), "-f", str(path)],
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


def test_passwords_land_in_private_secure_config(site: Path) -> None:
    env = {
        **SECRET_ENV,
        "GERRIT_SAML_KEYSTORE_PASSWORD": "fixed keystore pass; with # marks",
        "GERRIT_SAML_PRIVATE_KEY_PASSWORD": "fixed-private-key-pass",
    }
    proc = run_script(site, env)
    assert proc.returncode == 0, proc.stderr
    secure = site / "etc" / "secure.config"
    assert stat.S_IMODE(secure.stat().st_mode) == 0o600
    read = subprocess.run(
        ["git", "config", "-f", str(secure), "--get-regexp", r"^saml\."],
        capture_output=True,
        text=True,
        check=True,
    )
    assert read.stdout.splitlines() == [
        "saml.keystorepassword fixed keystore pass; with # marks",
        "saml.privatekeypassword fixed-private-key-pass",
    ]


def test_empty_passwords_are_left_out(site: Path) -> None:
    env = dict.fromkeys(PASSWORD_ENV, "")
    proc = run_script(site, {**SECRET_ENV, **env})
    assert proc.returncode == 0, proc.stderr
    secure = (site / "etc" / "secure.config").read_text(encoding="utf-8")
    assert "[saml]" not in secure
