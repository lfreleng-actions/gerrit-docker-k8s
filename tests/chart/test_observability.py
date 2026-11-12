# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for the Gerrit chart's health checks, metrics and logs.

Checks the probes and ``etc/healthcheck.config`` with
``plugins.healthcheck`` on and off, ``etc/events-log.config`` with
``plugins.events-log`` on and off, its store file and its refusal of
store keys and bad store files, the
metrics plugin's Secret, its
bearer token in ``secure.config`` and the NetworkPolicy rule for the
scraper, the ``[log]`` section of ``gerrit.config`` and the
``etc/log4j.properties`` that sends the error log to stderr as JSON
and keeps the request logs on the volume. Compares the
ServiceMonitor, rendered where the cluster serves
``monitoring.coreos.com/v1``, with
``tests/chart/golden/servicemonitor.golden``, and the log cleanup
CronJob with ``tests/chart/golden/log-cleanup-cronjob.golden``, and
runs ``files/scripts/log-cleanup.sh`` against a fixture site.

After an intended change to the rendered output, regenerate the
goldens with ``UPDATE_GOLDEN=1 uv run pytest tests/chart`` and review
the diff.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
import yaml
from chart.test_hooks import helm_fails, plugins
from chart.test_render import CHART, GOLDEN, GOLDEN_HEADER, read_golden
from chart.test_replication import find, run_helm
from chart.test_saml import container, gerrit_config
from chart.test_secure_config import SECRET_ENV, run_script

CRD = ("--api-versions", "monitoring.coreos.com/v1")
HEALTHCHECK = {"plugins": {"healthcheck": {"enabled": True}}}
METRICS = {"plugins": {"metrics-reporter-prometheus": {"enabled": True}}}
EVENTS_LOG = {"events-log": {"enabled": True}}
MONITOR = {**METRICS, "metrics": {"serviceMonitor": {"enabled": True}}}
CLEANUP = {"logs": {"retentionDays": 30}}
CLEANUP_SCRIPT = CHART / "files" / "scripts" / "log-cleanup.sh"
needs_helm = pytest.mark.skipif(shutil.which("helm") is None, reason="needs helm")


def render(
    tmp_path: Path, values: dict[str, Any] | None = None, *args: str
) -> list[Any]:
    """Return every object the chart renders with *values* and *args*."""
    path = tmp_path / "values.yaml"
    path.write_text(yaml.safe_dump(values or {}), encoding="utf-8")
    proc = run_helm("-f", str(path), *args)
    assert proc.returncode == 0, proc.stderr
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


def render_only(template: str, *args: str) -> str:
    """Return one template's render with the given helm options."""
    proc = run_helm(*args, "--show-only", f"templates/{template}")
    assert proc.returncode == 0, proc.stderr
    out: str = proc.stdout
    return out


def assert_matches_golden(out: str, name: str) -> None:
    """Compare a rendered template with ``tests/chart/golden/<name>``."""
    golden = GOLDEN / name
    if os.environ.get("UPDATE_GOLDEN"):
        golden.write_text(GOLDEN_HEADER + out, encoding="utf-8")
    assert out == read_golden(golden)


def probe_paths(docs: list[Any]) -> dict[str, str]:
    """Return the HTTP path of each probe of the Gerrit container."""
    gerrit = container(docs, "gerrit")
    return {
        probe: gerrit[probe]["httpGet"]["path"]
        for probe in ("startupProbe", "readinessProbe", "livenessProbe")
    }


def config_data(docs: list[Any]) -> dict[str, str]:
    """Return the data of the rendered gerrit-config ConfigMap."""
    config = find(docs, "ConfigMap", "gerrit-config")
    assert config is not None
    data: dict[str, str] = config["data"]
    return data


def git_config(text: str, tmp_path: Path, *args: str) -> str:
    """Return ``git config`` output for a rendered config file."""
    path = tmp_path / "rendered.config"
    path.write_text(text, encoding="utf-8")
    proc = subprocess.run(
        ["git", "config", "-f", str(path), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.stdout.strip()


def kinds(docs: list[Any], kind: str) -> list[dict[str, Any]]:
    """Return every rendered object of *kind*."""
    return [d for d in docs if d["kind"] == kind]


def http_ingress_rule(docs: list[Any]) -> dict[str, Any]:
    """Return the allow policy's ingress rule for port 8080."""
    policy = find(docs, "NetworkPolicy", "gerrit-allow")
    assert policy is not None
    rules: list[dict[str, Any]] = [
        r for r in policy["spec"]["ingress"] if r["ports"][0]["port"] == 8080
    ]
    (rule,) = rules
    return rule


@needs_helm
def test_defaults_render_only_healthcheck(tmp_path: Path) -> None:
    docs = render(tmp_path)
    assert probe_paths(docs) == {
        "startupProbe": "/r/config/server/version",
        "readinessProbe": "/r/config/server/healthcheck~status",
        "livenessProbe": "/r/config/server/version",
    }
    assert plugins(docs) == ["healthcheck"]
    assert "healthcheck.config" in config_data(docs)
    assert find(docs, "Secret", "gerrit-metrics") is None
    env = [e["name"] for e in container(docs, "render-config")["env"]]
    assert "GERRIT_METRICS_BEARER_TOKEN" not in env
    assert kinds(docs, "CronJob") == []


@needs_helm
def test_healthcheck_disabled_probes_the_version_endpoint(tmp_path: Path) -> None:
    docs = render(tmp_path, {"plugins": {"healthcheck": {"enabled": False}}})
    assert probe_paths(docs) == dict.fromkeys(
        ("startupProbe", "readinessProbe", "livenessProbe"), "/r/config/server/version"
    )
    assert plugins(docs) == []
    assert "healthcheck.config" not in config_data(docs)


@needs_helm
@pytest.mark.parametrize(("prefix", "path"), [("/r", "/r"), ("", "")])
def test_healthcheck_points_only_readiness_at_status(
    tmp_path: Path, prefix: str, path: str
) -> None:
    values = {**HEALTHCHECK, "gerrit": {"urlPrefix": prefix}}
    docs = render(tmp_path, values)
    assert plugins(docs) == ["healthcheck"]
    version = f"{path}/config/server/version"
    assert probe_paths(docs) == {
        "startupProbe": version,
        "readinessProbe": f"{path}/config/server/healthcheck~status",
        "livenessProbe": version,
    }


@needs_helm
def test_healthcheck_config_turns_off_auth_and_querychanges(tmp_path: Path) -> None:
    config = config_data(render(tmp_path, HEALTHCHECK))["healthcheck.config"]
    assert config == (
        '[healthcheck "auth"]\n\tenabled = false\n'
        '[healthcheck "querychanges"]\n\tenabled = false\n'
    )


@needs_helm
def test_healthcheck_config_renders_the_values(tmp_path: Path) -> None:
    values = {
        "plugins": {
            "healthcheck": {
                "enabled": True,
                "config": {
                    "timeout": "2s",
                    "auth": None,
                    "querychanges": {"enabled": False, "timeout": "5s"},
                },
            }
        }
    }
    config = config_data(render(tmp_path, values))["healthcheck.config"]
    assert git_config(config, tmp_path, "--list").splitlines() == [
        "healthcheck.timeout=2s",
        "healthcheck.querychanges.enabled=false",
        "healthcheck.querychanges.timeout=5s",
    ]


@needs_helm
def test_plugin_values_stay_out_of_the_plugins_section(tmp_path: Path) -> None:
    values = {"plugins": {**HEALTHCHECK["plugins"], **METRICS["plugins"], **EVENTS_LOG}}
    docs = render(tmp_path, values)
    config = gerrit_config(docs)
    assert "[plugins]\n\tallowRemoteAdmin = false\n" in config
    assert '[plugins "' not in config
    assert plugins(docs) == ["healthcheck", "events-log", "metrics-reporter-prometheus"]


@needs_helm
def test_events_log_is_off_by_default(tmp_path: Path) -> None:
    docs = render(tmp_path)
    assert "events-log" not in plugins(docs)
    assert "events-log.config" not in config_data(docs)


@needs_helm
def test_events_log_keeps_its_store_under_the_site(tmp_path: Path) -> None:
    docs = render(tmp_path, {"plugins": EVENTS_LOG})
    assert plugins(docs) == ["healthcheck", "events-log"]
    config = config_data(docs)["events-log.config"]
    assert git_config(config, tmp_path, "--list").splitlines() == [
        "events-log.localstorepath=/var/gerrit/events-db/",
        "events-log.storeurl=jdbc:h2:/var/gerrit/data/db",
    ]


@needs_helm
def test_events_log_store_file_names_the_store(tmp_path: Path) -> None:
    values = {
        "plugins": {"events-log": {"enabled": True, "storeFile": "events-log-db"}}
    }
    config = config_data(render(tmp_path, values))["events-log.config"]
    url = git_config(config, tmp_path, "--get", "events-log.storeUrl")
    assert url == "jdbc:h2:/var/gerrit/data/events-log-db"


@needs_helm
@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize(
    "store_file", ["../db", "a/db", ".", "..", "db;MODE=x", "e db"]
)
def test_events_log_refuses_a_bad_store_file(
    tmp_path: Path, store_file: str, enabled: bool
) -> None:
    values = {"plugins": {"events-log": {"enabled": enabled, "storeFile": store_file}}}
    message = helm_fails(tmp_path, values)
    assert f'plugins.events-log.storeFile "{store_file}" is not supported' in message


@needs_helm
def test_events_log_config_renders_the_values(tmp_path: Path) -> None:
    config_values = {"maxAge": 30, "returnLimit": 5000, "copyLocal": ""}
    values = {"plugins": {"events-log": {"enabled": True, "config": config_values}}}
    config = config_data(render(tmp_path, values))["events-log.config"]
    assert git_config(config, tmp_path, "--list").splitlines() == [
        "events-log.localstorepath=/var/gerrit/events-db/",
        "events-log.maxage=30",
        "events-log.returnlimit=5000",
        "events-log.storeurl=jdbc:h2:/var/gerrit/data/db",
    ]


@needs_helm
@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize(
    "key",
    [
        "storeUrl",
        "storeUsername",
        "storePassword",
        "localStorePath",
        "STOREURL",
        "storeurl",
        "storepassword",
        "StoreUsername",
        "LocalStorePath",
    ],
)
def test_events_log_refuses_store_keys(tmp_path: Path, key: str, enabled: bool) -> None:
    values = {"plugins": {"events-log": {"enabled": enabled, "config": {key: "x"}}}}
    message = helm_fails(tmp_path, values)
    assert f"plugins.events-log.config.{key} is not supported" in message


@needs_helm
@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize(
    "key",
    [
        "storePassword ",
        "storeurl ",
        " storeUrl",
        "store Url",
        "storeUrl\n",
        "1maxAge",
        "max_age",
    ],
)
def test_events_log_refuses_a_key_git_cannot_name(
    tmp_path: Path, key: str, enabled: bool
) -> None:
    values = {"plugins": {"events-log": {"enabled": enabled, "config": {key: "x"}}}}
    message = helm_fails(tmp_path, values)
    assert "is not supported: it must be a git config variable name" in message


@needs_helm
@pytest.mark.parametrize("enabled", [True, False])
def test_events_log_refuses_a_subsection(tmp_path: Path, enabled: bool) -> None:
    config = {"sub": {"storePassword": "x"}}
    values = {"plugins": {"events-log": {"enabled": enabled, "config": config}}}
    message = helm_fails(tmp_path, values)
    assert "plugins.events-log.config.sub is not supported" in message


@needs_helm
@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize(
    "value",
    [
        [["x\nstoreUrl = jdbc:h2:/tmp/other\n#"]],
        [{"storePassword": "x"}],
        ["a", ["b"]],
    ],
)
def test_events_log_refuses_a_nested_list(
    tmp_path: Path, value: list[Any], enabled: bool
) -> None:
    values = {"plugins": {"events-log": {"enabled": enabled, "config": {"zzz": value}}}}
    message = helm_fails(tmp_path, values)
    assert "plugins.events-log.config.zzz is not supported: a list" in message


@needs_helm
@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize(
    ("key", "value", "name"),
    [
        ("urlOptions", "USER=sa", "USER"),
        ("urlOptions", ["MODE=MySQL", "PASSWORD=x"], "PASSWORD"),
        ("urloptions", [" user = sa"], "user"),
        ("URLOPTIONS", "MODE=MySQL;Password=x", "Password"),
        ("urlOptions", ["userName=sa"], "userName"),
        ("urlOptions", "FILE_PASSWORD=x", "FILE_PASSWORD"),
    ],
)
def test_events_log_refuses_credentials_in_url_options(
    tmp_path: Path, key: str, value: Any, name: str, enabled: bool
) -> None:
    values = {"plugins": {"events-log": {"enabled": enabled, "config": {key: value}}}}
    message = helm_fails(tmp_path, values)
    assert (
        f'plugins.events-log.config.{key} option "{name}" is not supported' in message
    )


@needs_helm
def test_events_log_keeps_other_url_options(tmp_path: Path) -> None:
    config_values = {"urlOptions": ["MODE=MySQL", "DB_CLOSE_DELAY=-1"]}
    values = {"plugins": {"events-log": {"enabled": True, "config": config_values}}}
    config = config_data(render(tmp_path, values))["events-log.config"]
    options = git_config(config, tmp_path, "--get-all", "events-log.urlOptions")
    assert options.splitlines() == ["MODE=MySQL", "DB_CLOSE_DELAY=-1"]


@needs_helm
def test_events_log_config_repeats_a_list_key(tmp_path: Path) -> None:
    values = {"plugins": {"events-log": {"enabled": True, "config": {"x": ["a", 2]}}}}
    config = config_data(render(tmp_path, values))["events-log.config"]
    assert git_config(config, tmp_path, "--get-all", "events-log.x").splitlines() == [
        "a",
        "2",
    ]


@needs_helm
def test_metrics_token_comes_from_a_generated_secret(tmp_path: Path) -> None:
    docs = render(tmp_path, METRICS)
    assert plugins(docs) == ["healthcheck", "metrics-reporter-prometheus"]
    secret = find(docs, "Secret", "gerrit-metrics")
    assert secret is not None
    assert len(secret["data"]["token"]) > 40
    assert secret["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"
    env = {e["name"]: e for e in container(docs, "render-config")["env"]}
    ref = env["GERRIT_METRICS_BEARER_TOKEN"]["valueFrom"]["secretKeyRef"]
    assert ref == {"name": "gerrit-metrics", "key": "token"}


@needs_helm
def test_metrics_existing_secret_is_used(tmp_path: Path) -> None:
    values = {**METRICS, "metrics": {"existingSecret": "prom-token"}}
    docs = render(tmp_path, values)
    assert find(docs, "Secret", "gerrit-metrics") is None
    env = {e["name"]: e for e in container(docs, "render-config")["env"]}
    ref = env["GERRIT_METRICS_BEARER_TOKEN"]["valueFrom"]["secretKeyRef"]
    assert ref == {"name": "prom-token", "key": "token"}


@needs_helm
def test_service_monitor_needs_the_crd(tmp_path: Path) -> None:
    assert kinds(render(tmp_path, MONITOR), "ServiceMonitor") == []
    assert kinds(render(tmp_path, METRICS, *CRD), "ServiceMonitor") == []
    (monitor,) = kinds(render(tmp_path, MONITOR, *CRD), "ServiceMonitor")
    assert monitor["metadata"]["name"] == "gerrit"


@needs_helm
def test_service_monitor_matches_golden() -> None:
    out = render_only(
        "servicemonitor.yaml",
        "--namespace",
        "gerrit-test",
        *CRD,
        "--set",
        "plugins.metrics-reporter-prometheus.enabled=true",
        "--set",
        "metrics.serviceMonitor.enabled=true",
        "--set",
        "metrics.serviceMonitor.labels.release=prometheus",
        "--set",
        "metrics.serviceMonitor.scrapeTimeout=30s",
    )
    assert_matches_golden(out, "servicemonitor.golden")


@needs_helm
def test_service_monitor_selects_only_the_http_service(tmp_path: Path) -> None:
    docs = render(tmp_path, MONITOR, *CRD)
    (monitor,) = kinds(docs, "ServiceMonitor")
    selector = monitor["spec"]["selector"]["matchLabels"].items()
    selected = [
        s["metadata"]["name"]
        for s in kinds(docs, "Service")
        if selector <= s["metadata"]["labels"].items()
    ]
    assert selected == ["gerrit-http"]


@needs_helm
def test_service_monitor_needs_the_metrics_plugin(tmp_path: Path) -> None:
    values = {"metrics": {"serviceMonitor": {"enabled": True}}}
    assert "needs plugins.metrics-reporter-prometheus.enabled" in helm_fails(
        tmp_path, values
    )


@needs_helm
def test_network_policy_admits_the_scraper_namespace(tmp_path: Path) -> None:
    ingress = {"matchLabels": {"kubernetes.io/metadata.name": "traefik"}}
    scraper = {"matchLabels": {"kubernetes.io/metadata.name": "monitoring"}}
    selectors = {
        "networkPolicy": {
            "ingressNamespaceSelector": ingress,
            "metricsNamespaceSelector": scraper,
        }
    }
    rule = http_ingress_rule(render(tmp_path, {**METRICS, **selectors}))
    assert rule["from"] == [
        {"namespaceSelector": ingress},
        {"namespaceSelector": scraper},
    ]
    # Without the plugin the scraper's namespace is not admitted
    rule = http_ingress_rule(render(tmp_path, selectors))
    assert rule["from"] == [{"namespaceSelector": ingress}]


@needs_helm
def test_network_policy_any_source_stays_open(tmp_path: Path) -> None:
    scraper = {"matchLabels": {"kubernetes.io/metadata.name": "monitoring"}}
    values = {**METRICS, "networkPolicy": {"metricsNamespaceSelector": scraper}}
    assert "from" not in http_ingress_rule(render(tmp_path, values))


@needs_helm
def test_log_section_defaults(tmp_path: Path) -> None:
    config = gerrit_config(render(tmp_path))
    assert git_config(config, tmp_path, "--get-regexp", r"^log\.").splitlines() == [
        "log.compress true",
        "log.rotate true",
    ]


def log4j_properties(docs: list[Any]) -> dict[str, str]:
    """Return the rendered ``etc/log4j.properties`` as a dict."""
    props: dict[str, str] = {}
    for line in config_data(docs)["log4j.properties"].splitlines():
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            props[key] = value
    return props


@needs_helm
def test_java_options_name_the_log4j_properties(tmp_path: Path) -> None:
    config = gerrit_config(render(tmp_path))
    options = git_config(config, tmp_path, "--get-all", "container.javaOptions")
    assert (
        "-Dlog4j.configuration=file:///var/gerrit/etc/log4j.properties"
        in options.splitlines()
    )


@needs_helm
def test_log4j_sends_the_error_log_to_stderr_as_json(tmp_path: Path) -> None:
    props = log4j_properties(render(tmp_path))
    assert props["log4j.rootLogger"] == "INFO, stderr"
    assert props["log4j.appender.stderr"] == "org.apache.log4j.ConsoleAppender"
    assert props["log4j.appender.stderr.Target"] == "System.err"
    assert props["log4j.appender.stderr.layout"] == (
        "com.google.gerrit.pgm.util.ErrorLogJsonLayout"
    )


@needs_helm
@pytest.mark.parametrize(
    ("name", "layout"),
    [
        ("httpd_log", "com.google.gerrit.pgm.http.jetty.HttpLogLayout"),
        ("sshd_log", "com.google.gerrit.sshd.SshLogLayout"),
    ],
)
def test_log4j_keeps_the_request_logs_on_the_volume(
    tmp_path: Path, name: str, layout: str
) -> None:
    props = log4j_properties(render(tmp_path))
    prefix = f"log4j.appender.{name}"
    assert props[f"log4j.logger.{name}"] == f"INFO, {name}"
    assert props[f"log4j.additivity.{name}"] == "false"
    assert props[prefix] == "org.apache.log4j.DailyRollingFileAppender"
    assert props[f"{prefix}.DatePattern"] == "'.'yyyy-MM-dd"
    assert props[f"{prefix}.File"] == f"/var/gerrit/logs/{name}"
    assert props[f"{prefix}.layout"] == layout


@needs_helm
def test_log4j_follows_rotate_and_level(tmp_path: Path) -> None:
    values = {"log": {"rotate": False}, "logs": {"level": "warn"}}
    props = log4j_properties(render(tmp_path, values))
    assert props["log4j.rootLogger"] == "WARN, stderr"
    for name in ("httpd_log", "sshd_log"):
        assert props[f"log4j.appender.{name}"] == "org.apache.log4j.FileAppender"
        assert f"log4j.appender.{name}.DatePattern" not in props


@needs_helm
@pytest.mark.parametrize("key", ["jsonLogging", "textLogging"])
def test_log4j_rejects_the_built_in_log_toggles(tmp_path: Path, key: str) -> None:
    message = helm_fails(tmp_path, {"log": {key: True}})
    assert f"log.{key} is not supported" in message


@needs_helm
def test_log4j_rejects_an_unknown_level(tmp_path: Path) -> None:
    message = helm_fails(tmp_path, {"logs": {"level": "LOUD"}})
    assert "logs.level must be a log4j level" in message


@needs_helm
def test_log_cleanup_matches_golden() -> None:
    out = render_only(
        "log-cleanup-cronjob.yaml",
        "--namespace",
        "gerrit-test",
        "--set",
        "logs.retentionDays=30",
    )
    assert_matches_golden(out, "log-cleanup-cronjob.golden")


@needs_helm
def test_log_cleanup_pod_is_isolated(tmp_path: Path) -> None:
    docs = render(tmp_path, CLEANUP)
    (cron,) = kinds(docs, "CronJob")
    assert cron["metadata"]["name"] == "gerrit-log-cleanup"
    assert cron["spec"]["concurrencyPolicy"] == "Forbid"
    pod = cron["spec"]["jobTemplate"]["spec"]["template"]
    spec = pod["spec"]
    assert spec["automountServiceAccountToken"] is False
    assert spec["securityContext"]["runAsUser"] == 1001
    claims = [
        v["persistentVolumeClaim"]
        for v in spec["volumes"]
        if "persistentVolumeClaim" in v
    ]
    assert claims == [{"claimName": "site-gerrit-0"}]
    (cleanup,) = spec["containers"]
    env = {e["name"]: e["value"] for e in cleanup["env"]}
    assert env["LOG_RETENTION_DAYS"] == "30"
    labels = pod["metadata"]["labels"]
    for svc in kinds(docs, "Service"):
        assert not svc["spec"]["selector"].items() <= labels.items()
    policies = [
        p
        for p in kinds(docs, "NetworkPolicy")
        if p["spec"]["podSelector"]["matchLabels"].items() <= labels.items()
    ]
    assert [p["metadata"]["name"] for p in policies] == ["gerrit-log-cleanup-deny"]
    assert "ingress" not in policies[0]["spec"]
    assert "egress" not in policies[0]["spec"]


@needs_helm
@pytest.mark.parametrize(
    "override",
    [("--set", "nameOverride=" + "n" * 63), ("--set", "fullnameOverride=" + "f" * 52)],
)
def test_log_cleanup_names_fit_kubernetes_limits(
    tmp_path: Path, override: tuple[str, str]
) -> None:
    docs = render(tmp_path, CLEANUP, *override)
    (cron,) = kinds(docs, "CronJob")
    assert len(cron["metadata"]["name"]) <= 52
    labels = cron["spec"]["jobTemplate"]["spec"]["template"]["metadata"]["labels"]
    assert len(labels["app.kubernetes.io/name"]) <= 63


@needs_helm
@pytest.mark.parametrize("days", [-1, 1.5, "a week"])
def test_log_cleanup_rejects_a_bad_retention(tmp_path: Path, days: object) -> None:
    message = helm_fails(tmp_path, {"logs": {"retentionDays": days}})
    assert "logs.retentionDays must be a whole number of days" in message


def test_metrics_token_lands_in_secure_config(tmp_path: Path) -> None:
    (tmp_path / "etc").mkdir()
    env = {**SECRET_ENV, "GERRIT_METRICS_BEARER_TOKEN": "fixed-metrics-token"}
    proc = run_script(tmp_path, env)
    assert proc.returncode == 0, proc.stderr
    secure = tmp_path / "etc" / "secure.config"
    assert stat.S_IMODE(secure.stat().st_mode) == 0o600
    key = "plugin.metrics-reporter-prometheus.prometheusBearerToken"
    assert git_config(secure.read_text(encoding="utf-8"), tmp_path, key) == (
        "fixed-metrics-token"
    )


def run_cleanup(site: Path, days: str) -> subprocess.CompletedProcess[str]:
    """Run log-cleanup.sh on a site with the given retention."""
    env = {**os.environ, "GERRIT_SITE": str(site), "LOG_RETENTION_DAYS": days}
    return subprocess.run(
        ["sh", str(CLEANUP_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env=env,
    )


def make_log(logs: Path, name: str, age_days: float) -> Path:
    """Create a log file last written *age_days* days ago."""
    path = logs / name
    path.write_text("entry\n", encoding="utf-8")
    then = time.time() - age_days * 86400
    os.utime(path, (then, then))
    return path


def test_cleanup_deletes_only_old_rotated_logs(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    old = [
        make_log(logs, "httpd_log.2026-01-01.gz", 40),
        make_log(logs, "sshd_log.json.2026-01-02.gz", 31),
    ]
    kept = [
        make_log(logs, "httpd_log.2026-02-01.gz", 29),
        make_log(logs, "sshd_log", 400),
        make_log(logs, "error_log.json", 400),
        make_log(logs, "notes-2026-01-01", 400),
    ]
    (logs / "archive.2026-01-01").mkdir()
    nested = make_log(logs / "archive.2026-01-01", "x.2026-01-01.gz", 400)
    proc = run_cleanup(tmp_path, "30")
    assert proc.returncode == 0, proc.stderr
    assert not any(p.exists() for p in old)
    assert all(p.exists() for p in kept)
    assert nested.exists()
    assert sorted(proc.stdout.splitlines()[:-1]) == sorted(str(p) for p in old)


@pytest.mark.parametrize("days", ["", "0", "07", "-1", "1.5", "ten"])
def test_cleanup_rejects_a_bad_retention(tmp_path: Path, days: str) -> None:
    (tmp_path / "logs").mkdir()
    proc = run_cleanup(tmp_path, days)
    assert proc.returncode != 0
    assert "LOG_RETENTION_DAYS" in proc.stderr


def test_cleanup_without_a_log_directory(tmp_path: Path) -> None:
    proc = run_cleanup(tmp_path, "30")
    assert proc.returncode == 0, proc.stderr
    assert "nothing to delete" in proc.stdout
