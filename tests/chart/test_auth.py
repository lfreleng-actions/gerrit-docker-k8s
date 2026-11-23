# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for the chart's choice of authentication and its NOTES.txt.

The chart has no default authentication: values enable SAML, set a
Gerrit auth type, or set ``DEVELOPMENT_BECOME_ANY_ACCOUNT`` together
with ``auth.allowDevelopmentAccounts``. These renders leave out the
development accounts the other chart tests use, so each test makes its
own choice.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from chart.test_render import CHART, GOLDEN, IMAGE_TAG, read_golden
from chart.test_saml import SAML_ATTRS, SOURCES

DEVELOPMENT = "DEVELOPMENT_BECOME_ANY_ACCOUNT"
SAML = {"enabled": True, **SAML_ATTRS, **SOURCES["url"]}
CHOOSE = "choose authentication"
OPT_IN = "set auth.allowDevelopmentAccounts: true to use it on purpose"
WARNING = "WARNING: DEVELOPMENT ACCOUNTS ARE ENABLED"
NOT_BOOLEAN = "auth.allowDevelopmentAccounts must be a boolean"
NOT_STRING = "auth.type must be a string"
EXTRA_AUTH = "gerrit.extraConfig cannot carry an [auth] section"
COOKIE_SECURE = "auth.cookieSecure was removed in Gerrit 3.13"
CHOICES: list[dict[str, Any]] = [
    {"type": "HTTP"},
    {"type": "LDAP"},
    {"saml": SAML},
    {"type": DEVELOPMENT, "allowDevelopmentAccounts": True},
]

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="needs helm")


def template(
    tmp_path: Path, auth: dict[str, Any] | None, chart: Path = CHART, *args: str
) -> subprocess.CompletedProcess[str]:
    """Run ``helm template`` with *auth* as the only auth values.

    *args* go after the values file, so ``--set`` flags override it.
    """
    path = tmp_path / "values.yaml"
    values = {} if auth is None else {"auth": auth}
    path.write_text(yaml.safe_dump(values), encoding="utf-8")
    return subprocess.run(
        ["helm", "template", "gerrit", str(chart), *IMAGE_TAG, "-f", str(path), *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def template_with_extra(
    tmp_path: Path, auth: dict[str, Any], extra: str
) -> subprocess.CompletedProcess[str]:
    """Run ``helm template`` with *auth* and *extra* as ``gerrit.extraConfig``."""
    path = tmp_path / "values.yaml"
    values = {"auth": auth, "gerrit": {"extraConfig": extra}}
    path.write_text(yaml.safe_dump(values), encoding="utf-8")
    return subprocess.run(
        ["helm", "template", "gerrit", str(CHART), *IMAGE_TAG, "-f", str(path)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def configmap_data(proc: subprocess.CompletedProcess[str], name: str, key: str) -> str:
    """Return *key* of the ConfigMap *name* in a successful render."""
    assert proc.returncode == 0, proc.stderr
    for doc in yaml.safe_load_all(proc.stdout):
        if doc and doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == name:
            data: str = doc["data"][key]
            return data
    raise AssertionError(f"no {name} ConfigMap")


def gerrit_config(tmp_path: Path, auth: dict[str, Any]) -> str:
    """Return the ``gerrit.config`` the chart renders with *auth*."""
    return configmap_data(template(tmp_path, auth), "gerrit-config", "gerrit.config")


def notes(tmp_path: Path, auth: dict[str, Any]) -> str:
    """Return the chart's NOTES.txt as rendered with *auth*.

    ``helm template`` leaves NOTES.txt out, so a copy of the chart
    renders it into a probe ConfigMap.
    """
    chart = tmp_path / "gerrit"
    shutil.copytree(CHART, chart)
    shutil.copy(chart / "templates" / "NOTES.txt", chart / "files" / "NOTES.txt")
    (chart / "templates" / "notes-probe.yaml").write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: notes-probe\n"
        'data:\n  notes: {{ tpl (.Files.Get "files/NOTES.txt") . | toJson }}\n',
        encoding="utf-8",
    )
    return configmap_data(template(tmp_path, auth, chart), "notes-probe", "notes")


def test_values_choose_no_authentication() -> None:
    values = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
    assert values["auth"]["type"] == ""
    assert values["auth"]["allowDevelopmentAccounts"] is False
    assert values["auth"]["saml"]["enabled"] is False


@pytest.mark.parametrize("auth", [None, {"type": ""}, {"type": None}])
def test_no_choice_fails(tmp_path: Path, auth: dict[str, Any] | None) -> None:
    proc = template(tmp_path, auth)
    assert proc.returncode != 0
    assert CHOOSE in proc.stderr
    assert "auth.saml.enabled: true" in proc.stderr
    assert "auth.allowDevelopmentAccounts: true" in proc.stderr


@pytest.mark.parametrize(
    "auth_type",
    [DEVELOPMENT, "development_become_any_account", "Development-Become-Any-Account"],
)
def test_development_accounts_need_the_opt_in(tmp_path: Path, auth_type: str) -> None:
    proc = template(tmp_path, {"type": auth_type})
    assert proc.returncode != 0
    assert OPT_IN in proc.stderr


@pytest.mark.parametrize("allow", [None, False])
def test_development_accounts_refused_without_a_true_opt_in(
    tmp_path: Path, allow: bool | None
) -> None:
    proc = template(tmp_path, {"type": DEVELOPMENT, "allowDevelopmentAccounts": allow})
    assert proc.returncode != 0
    assert OPT_IN in proc.stderr


@pytest.mark.parametrize("allow", ["false", "true", "", 1, 0, ["true"], {"a": 1}])
@pytest.mark.parametrize("auth_type", [DEVELOPMENT, "LDAP"])
def test_opt_in_must_be_a_boolean(tmp_path: Path, allow: Any, auth_type: str) -> None:
    proc = template(tmp_path, {"type": auth_type, "allowDevelopmentAccounts": allow})
    assert proc.returncode != 0
    assert NOT_BOOLEAN in proc.stderr


def test_opt_in_refuses_set_string_false(tmp_path: Path) -> None:
    proc = template(
        tmp_path,
        {"type": DEVELOPMENT},
        CHART,
        "--set-string",
        "auth.allowDevelopmentAccounts=false",
    )
    assert proc.returncode != 0
    assert NOT_BOOLEAN in proc.stderr


@pytest.mark.parametrize("allow", ["true", "false"])
def test_opt_in_accepts_set_booleans(tmp_path: Path, allow: str) -> None:
    proc = template(
        tmp_path,
        {"type": DEVELOPMENT},
        CHART,
        "--set",
        f"auth.allowDevelopmentAccounts={allow}",
    )
    if allow == "true":
        assert proc.returncode == 0, proc.stderr
    else:
        assert proc.returncode != 0
        assert OPT_IN in proc.stderr


@pytest.mark.parametrize(
    "auth_type", [[DEVELOPMENT], ["LDAP"], {DEVELOPMENT: True}, 1, True]
)
@pytest.mark.parametrize("allow", [False, True])
def test_auth_type_must_be_a_string(
    tmp_path: Path, auth_type: Any, allow: bool
) -> None:
    proc = template(tmp_path, {"type": auth_type, "allowDevelopmentAccounts": allow})
    assert proc.returncode != 0
    assert NOT_STRING in proc.stderr


def test_development_accounts_render_with_the_opt_in(tmp_path: Path) -> None:
    config = gerrit_config(
        tmp_path, {"type": DEVELOPMENT, "allowDevelopmentAccounts": True}
    )
    assert f"[auth]\n\ttype = {DEVELOPMENT}\n" in config
    assert "allowDevelopmentAccounts" not in config


def test_development_accounts_refused_with_saml(tmp_path: Path) -> None:
    proc = template(tmp_path, {"allowDevelopmentAccounts": True, "saml": SAML})
    assert proc.returncode != 0
    assert "cannot be combined with auth.saml.enabled" in proc.stderr


def test_saml_alone_renders_http(tmp_path: Path) -> None:
    config = gerrit_config(tmp_path, {"saml": SAML})
    assert "\ttype = HTTP\n" in config
    assert config == read_golden(GOLDEN / "saml-metadata-url.config")


@pytest.mark.parametrize("auth_type", ["LDAP", "OAUTH", "HTTP"])
def test_other_auth_types_render(tmp_path: Path, auth_type: str) -> None:
    config = gerrit_config(tmp_path, {"type": auth_type})
    assert f"[auth]\n\ttype = {auth_type}\n" in config


def test_notes_warn_about_development_accounts(tmp_path: Path) -> None:
    text = notes(tmp_path, {"type": DEVELOPMENT, "allowDevelopmentAccounts": True})
    assert WARNING in text
    assert "the administrator included" in text


def test_notes_summarise_saml(tmp_path: Path) -> None:
    text = notes(tmp_path, {"saml": SAML})
    assert WARNING not in text
    assert "Authentication: SAML single sign-on" in text
    assert SOURCES["url"]["metadataUrl"] in text


@pytest.mark.parametrize(("auth_type", "proxy"), [("HTTP", True), ("LDAP", False)])
def test_notes_summarise_other_auth_types(
    tmp_path: Path, auth_type: str, proxy: bool
) -> None:
    text = notes(tmp_path, {"type": auth_type})
    assert WARNING not in text
    assert f"Authentication: auth.type {auth_type}." in text
    assert ("authenticating proxy" in text) is proxy


@pytest.mark.parametrize(
    "extra",
    [
        f"[auth]\n\ttype = {DEVELOPMENT}\n",
        f"[auth] type = {DEVELOPMENT}\n",
        "[AUTH]\n  TYPE = development_become_any_account\n",
        "[auth]\n\ttype=LDAP # set\n",
        '[auth "x"]\n[auth]\ntype =\n',
        f"[auth]\n\u2003type = {DEVELOPMENT}\n",
        f"[auth]\u2003type\u2003= {DEVELOPMENT}\n",
        f"[auth]\n\tlogoutUrl = a\\\\\ntype = {DEVELOPMENT}\n",
        f"[auth]\n\rtype = {DEVELOPMENT}\n",
        f"[auth]\r\n\ttype = {DEVELOPMENT}\r\n",
        f"[auth] logoutUrl = a \\\ntype = {DEVELOPMENT}\n",
        f"[auth]\n; type = {DEVELOPMENT}\n",
        f"[auth]\n\t# type = {DEVELOPMENT}\n",
        f"[auth]\n\tlogoutUrl = a \\\ntype = {DEVELOPMENT}\n",
        '[auth]\n\tlogoutUrl = "a \\\ntype = \\\nb"\n',
        f'[plugin "x"]\n\tnote = a \\\n[auth]\ntype = {DEVELOPMENT}\n',
        "[auth]\n",
        " [Auth ]\n",
        "\r[AUTH]\n\tlogoutUrl = https://example.org\n",
    ],
)
@pytest.mark.parametrize("auth", CHOICES)
def test_extra_config_auth_section_is_refused(
    tmp_path: Path, auth: dict[str, Any], extra: str
) -> None:
    proc = template_with_extra(tmp_path, auth, extra)
    assert proc.returncode != 0
    assert EXTRA_AUTH in proc.stderr
    assert "auth.saml.enabled" in proc.stderr
    assert "auth.allowDevelopmentAccounts" in proc.stderr


@pytest.mark.parametrize(
    "extra",
    [
        f"# [auth] type = {DEVELOPMENT}\n",
        f'[auth "example"]\n\ttype = {DEVELOPMENT}\n',
        f"[auth.example]\n\ttype = {DEVELOPMENT}\n",
        f"[sendemail]\n\ttype = {DEVELOPMENT}\n",
        f'[plugin "x"]\n\tnote = "a] type = {DEVELOPMENT}"\n',
        f'[plugin "x"]\n\tnote = a ; [auth] type = {DEVELOPMENT}\n',
        f"type = {DEVELOPMENT}\n",
    ],
)
@pytest.mark.parametrize("auth", CHOICES)
def test_extra_config_without_auth_section_renders(
    tmp_path: Path, auth: dict[str, Any], extra: str
) -> None:
    config = configmap_data(
        template_with_extra(tmp_path, auth, extra), "gerrit-config", "gerrit.config"
    )
    assert extra.rstrip("\n") in config


def test_values_drop_cookie_secure() -> None:
    values = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
    assert "cookiesecure" not in {key.lower() for key in values["auth"]}


@pytest.mark.parametrize("value", [True, False, "true", ["true"]])
@pytest.mark.parametrize("key", ["cookieSecure", "cookiesecure"])
def test_cookie_secure_is_refused(tmp_path: Path, key: str, value: Any) -> None:
    proc = template(tmp_path, {"type": "HTTP", key: value})
    assert proc.returncode != 0
    assert COOKIE_SECURE in proc.stderr


@pytest.mark.parametrize("value", [None, "", []])
def test_unset_cookie_secure_renders(tmp_path: Path, value: Any) -> None:
    config = gerrit_config(tmp_path, {"type": "HTTP", "cookieSecure": value})
    assert "cookiesecure" not in config.lower()


LOGIN_KEYS = {
    "loginUrl": "https://sso.example.org/login",
    "logoutUrl": "https://sso.example.org/logout",
    "loginText": "Sign in with SSO",
}
BAD_URL = "must be an absolute http:// or https:// URL"


def auth_key(config: str, tmp_path: Path, key: str) -> list[str]:
    """Return every value of ``auth.<key>`` in a rendered *config*."""
    path = tmp_path / "gerrit.config"
    path.write_text(config, encoding="utf-8")
    proc = subprocess.run(
        ["git", "config", "-f", str(path), "--get-all", f"auth.{key}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.splitlines()


@pytest.mark.xfail(strict=True, reason="auth login keys not yet added")
def test_login_keys_default_to_unset() -> None:
    values = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
    for key in LOGIN_KEYS:
        assert values["auth"][key] is None


@pytest.mark.parametrize("key", list(LOGIN_KEYS))
@pytest.mark.parametrize("choice", [{"type": "HTTP"}, {"saml": SAML}])
def test_login_keys_render(tmp_path: Path, key: str, choice: dict[str, Any]) -> None:
    config = gerrit_config(tmp_path, {**choice, key: LOGIN_KEYS[key]})
    assert auth_key(config, tmp_path, key) == [LOGIN_KEYS[key]]
    assert auth_key(config, tmp_path, "type") == ["HTTP"]


def test_login_keys_keep_the_saml_keys(tmp_path: Path) -> None:
    config = gerrit_config(tmp_path, {"saml": SAML, **LOGIN_KEYS})
    for key, value in LOGIN_KEYS.items():
        assert auth_key(config, tmp_path, key) == [value]
    assert auth_key(config, tmp_path, "httpHeader") == ["X-SAML-UserName"]
    assert auth_key(config, tmp_path, "httpEmailHeader") == ["X-SAML-EmailHeader"]
    assert auth_key(config, tmp_path, "httpExternalIdHeader") == ["X-SAML-ExternalId"]


@pytest.mark.parametrize("value", [None, ""])
@pytest.mark.parametrize("key", list(LOGIN_KEYS))
def test_unset_login_keys_render_nothing(tmp_path: Path, key: str, value: Any) -> None:
    config = gerrit_config(tmp_path, {"type": "HTTP", key: value})
    assert auth_key(config, tmp_path, key) == []


@pytest.mark.xfail(strict=True, reason="auth login keys not yet added")
@pytest.mark.parametrize(
    "value",
    [
        "/login",
        "login/",
        "ftp://sso.example.org/login",
        "https://",
        "https:///login",
        "https://?next=/login",
        "https://#login",
        "https:// /login",
        "https://sso.example.org /login",
        "https://@/login",
        "https://:8443/login",
        "https://[deadbeef]:8443/login",
        "https://[:::]:8443/login",
        "https://[1::2::3]/login",
        "https://[1:2:3:4:5:6:7:8:9]/login",
        "https://[::ffff:256.0.0.1]/login",
        "https://sso.example.org%/login",
        "https://sso.example.org|/login",
        "https://sso%2.example.org/login",
        "https://us%er@sso.example.org/login",
        "https://sso.example.org/log%zzin",
        "https://sso.example.org/a|b",
        "https://sso.example.org/login?next=%",
        "https://sso.example.org/login#a^b",
        ["https://sso.example.org/login"],
        True,
    ],
)
@pytest.mark.parametrize("key", ["loginUrl", "logoutUrl", "loginurl", "LOGOUTURL"])
@pytest.mark.parametrize("choice", [{"type": "HTTP"}, {"saml": SAML}])
def test_bad_login_url_fails(
    tmp_path: Path, key: str, value: Any, choice: dict[str, Any]
) -> None:
    proc = template(tmp_path, {**choice, key: value})
    assert proc.returncode != 0
    assert BAD_URL in proc.stderr
    assert f"auth.{key.lower().replace('url', 'Url')}" in proc.stderr


@pytest.mark.parametrize(
    "value",
    [
        "http://sso.example.org/",
        "HTTPS://sso:8443/x",
        "https://sso.example.org",
        "https://sso.example.org?next=/",
        "https://user@sso.example.org/login",
        "https://[2001:db8::1]:8443/login",
    ],
)
def test_login_url_accepts_http_and_https(tmp_path: Path, value: str) -> None:
    config = gerrit_config(tmp_path, {"type": "HTTP", "loginUrl": value})
    assert auth_key(config, tmp_path, "loginUrl") == [value]
