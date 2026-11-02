# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Verify a release logs as the chart's ``etc/log4j.properties`` says.

Reads the Gerrit container's output with ``kubectl logs`` and checks
that Gerrit's error log reaches it as JSON, one object per line, with
no line in Gerrit's built-in text layout, no complaint from log4j
about the configuration and no warning that a request log appender is
missing. Then checks, with ``kubectl exec``, that ``httpd_log`` in
``logs/`` on the site volume holds the requests the probes made, and
that ``sshd_log`` exists. It reuses the helpers of ``verify-site.py``.

Run it from the repository root, for example:

    python3 K8S/charts/gerrit/files/scripts/verify-logs.py \\
        --namespace gerrit-smoke --fullname gerrit
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType

LOGS = "/var/gerrit/logs"
REQUEST_LOGS = ("httpd_log", "sshd_log")

# Gerrit's built-in error log layout, which a log4j configuration that
# failed to load falls back to: "[2026-11-02T11:14:46.541Z] [main] ..."
TEXT_LINE = re.compile(r"^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
# Gerrit's warning when log4j.properties lacks a request log appender
NO_APPENDER = "No appender with the name"


def _load_site() -> ModuleType:
    """Load ``verify-site.py`` from beside this script."""
    path = Path(__file__).with_name("verify-site.py")
    spec = importlib.util.spec_from_file_location("verify_logs_site", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


site = _load_site()
CheckError = site.CheckError


def check_console(text: str) -> str:
    """Check the container's output carries the error log as JSON."""
    entries = 0
    for line in text.splitlines():
        if line.startswith("log4j:"):
            raise CheckError(f"log4j reported: {line[:200]}")
        if TEXT_LINE.match(line):
            raise CheckError(f"text error log line: {line[:200]}")
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            # gerrit.sh and the JVM print a few plain lines of their own
            continue
        if not isinstance(entry, dict) or "@timestamp" not in entry:
            continue
        message = str(entry.get("message", ""))
        if NO_APPENDER in message:
            raise CheckError(message[:200])
        entries += 1
    if not entries:
        raise CheckError("no JSON error log entry in the container's output")
    return f"{entries} JSON entries"


def check_request_logs(namespace: str, pod: str) -> str:
    """Check httpd_log has entries and sshd_log exists on the volume."""
    paths = [f"{LOGS}/{name}" for name in REQUEST_LOGS]
    out = site.kubectl(namespace, "exec", pod, "-c", "gerrit", "--", "wc", "-c", *paths)
    sizes: dict[str, int] = {}
    for line in out.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0].isdigit():
            sizes[Path(fields[1]).name] = int(fields[0])
    missing = [name for name in REQUEST_LOGS if name not in sizes]
    if missing:
        raise CheckError(f"missing from {LOGS}: {', '.join(missing)}")
    if not sizes["httpd_log"]:
        raise CheckError(f"{LOGS}/httpd_log is empty")
    return ", ".join(f"{name} {sizes[name]} bytes" for name in REQUEST_LOGS)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(
        description="Verify a release's container and request logs."
    )
    parser.add_argument("--namespace", required=True)
    parser.add_argument(
        "--fullname",
        default="gerrit",
        help="the chart's fullname: pod <fullname>-0 (default: gerrit)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the log checks and return the process exit code."""
    args = parse_args(argv)
    pod = f"{args.fullname}-0"

    def console() -> str:
        return check_console(site.kubectl(args.namespace, "logs", pod, "-c", "gerrit"))

    failures: list[str] = []
    site.run_check(failures, "JSON container log", console)
    site.run_check(
        failures,
        "request logs",
        lambda: check_request_logs(args.namespace, pod),
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
