# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for the chart's ``verify-logs.py`` smoke check.

Feeds ``check_console`` captured container output, and stubs the
``kubectl`` helper ``check_request_logs`` reads the site volume with.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.xfail(strict=True, reason="verify-logs.py lands in #244")

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "K8S"
    / "charts"
    / "gerrit"
    / "files"
    / "scripts"
    / "verify-logs.py"
)


@pytest.fixture(scope="module")
def verify_logs() -> ModuleType:
    """Load verify-logs.py, which loads its own copy of verify-site.py."""
    spec = importlib.util.spec_from_file_location("verify_logs", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def entry(message: str) -> str:
    """Return one line as Gerrit's ErrorLogJsonLayout writes it."""
    return json.dumps(
        {
            "@timestamp": "2026-11-02T11:14:46.541Z",
            "source_host": "10.0.0.1",
            "message": message,
            "level": "INFO",
            "logger_name": "com.google.gerrit.pgm.Daemon",
        }
    )


def test_console_counts_the_json_entries(verify_logs: ModuleType) -> None:
    out = "\n".join(
        [
            "Running Gerrit Code Review: /var/gerrit/bin/gerrit.war",
            entry("Gerrit Code Review 3.13.1 ready"),
            entry("Started httpd"),
        ]
    )
    assert verify_logs.check_console(out) == "2 JSON entries"


def test_console_fails_without_json(verify_logs: ModuleType) -> None:
    with pytest.raises(verify_logs.CheckError, match="no JSON error log entry"):
        verify_logs.check_console("Running Gerrit Code Review\n[]\n")


def test_console_fails_on_the_built_in_text_layout(verify_logs: ModuleType) -> None:
    line = (
        "[2026-11-02T11:14:46.541Z] [main] INFO  com.google.gerrit.pgm.Daemon : ready"
    )
    with pytest.raises(verify_logs.CheckError, match="text error log line"):
        verify_logs.check_console(f"{entry('a')}\n{line}\n")


def test_console_fails_on_a_log4j_complaint(verify_logs: ModuleType) -> None:
    line = "log4j:ERROR Could not instantiate class [com.example.Layout]."
    with pytest.raises(verify_logs.CheckError, match="log4j reported"):
        verify_logs.check_console(f"{entry('a')}\n{line}\n")


def test_console_fails_on_a_missing_request_log_appender(
    verify_logs: ModuleType,
) -> None:
    warning = entry("No appender with the name: httpd_log was found. httpd_log ...")
    with pytest.raises(verify_logs.CheckError, match="httpd_log"):
        verify_logs.check_console(f"{entry('a')}\n{warning}\n")


def stub_kubectl(
    monkeypatch: pytest.MonkeyPatch, verify_logs: ModuleType, out: str
) -> list[tuple[str, ...]]:
    """Make verify-logs' kubectl return *out*; return the calls made."""
    calls: list[tuple[str, ...]] = []

    def kubectl(namespace: str, *args: str, timeout: float = 120) -> str:
        calls.append((namespace, *args))
        return out

    monkeypatch.setattr(verify_logs.site, "kubectl", kubectl)
    return calls


def test_request_logs_pass(
    monkeypatch: pytest.MonkeyPatch, verify_logs: ModuleType
) -> None:
    out = (
        "  2048 /var/gerrit/logs/httpd_log\n"
        "     0 /var/gerrit/logs/sshd_log\n"
        "  2048 total\n"
    )
    calls = stub_kubectl(monkeypatch, verify_logs, out)
    result = verify_logs.check_request_logs("ns", "gerrit-0")
    assert result == "httpd_log 2048 bytes, sshd_log 0 bytes"
    assert calls[0][:6] == ("ns", "exec", "gerrit-0", "-c", "gerrit", "--")


def test_request_logs_fail_on_an_empty_httpd_log(
    monkeypatch: pytest.MonkeyPatch, verify_logs: ModuleType
) -> None:
    out = "0 /var/gerrit/logs/httpd_log\n0 /var/gerrit/logs/sshd_log\n0 total\n"
    stub_kubectl(monkeypatch, verify_logs, out)
    with pytest.raises(verify_logs.CheckError, match="httpd_log is empty"):
        verify_logs.check_request_logs("ns", "gerrit-0")


def test_request_logs_fail_on_a_missing_log(
    monkeypatch: pytest.MonkeyPatch, verify_logs: ModuleType
) -> None:
    stub_kubectl(monkeypatch, verify_logs, "12 /var/gerrit/logs/httpd_log\n")
    with pytest.raises(verify_logs.CheckError, match="missing .*: sshd_log"):
        verify_logs.check_request_logs("ns", "gerrit-0")


def test_parse_args(verify_logs: ModuleType) -> None:
    args = verify_logs.parse_args(["--namespace", "ns"])
    assert (args.namespace, args.fullname) == ("ns", "gerrit")
    with pytest.raises(SystemExit):
        verify_logs.parse_args([])
