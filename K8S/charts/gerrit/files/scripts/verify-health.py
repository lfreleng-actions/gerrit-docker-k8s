# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Verify the healthcheck plugin of a release reports a healthy site.

Port-forwards the release's HTTP Service and checks that every check
the healthcheck plugin runs at ``config/server/healthcheck~status``
passes or is disabled, naming each that fails. Use it for a release
with ``plugins.healthcheck.enabled``; it reuses the helpers of
``verify-site.py``.

Run it from the repository root, for example:

    python3 K8S/charts/gerrit/files/scripts/verify-health.py \\
        --namespace gerrit-smoke --fullname gerrit --url-prefix /r
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
import urllib.error
import urllib.request
from pathlib import Path
from types import ModuleType
from typing import Any

# Results that count as healthy; the plugin also reports failed and
# timeout
HEALTHY = ("passed", "disabled")


def _load_site() -> ModuleType:
    """Load ``verify-site.py`` from beside this script."""
    path = Path(__file__).with_name("verify-site.py")
    spec = importlib.util.spec_from_file_location("verify_health_site", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


site = _load_site()
CheckError = site.CheckError


def fetch_report(url: str) -> tuple[int, Any]:
    """GET the plugin's report; return the HTTP status and the report.

    The plugin answers 200 when every enabled check passes and 500
    when one does not, with the report in both cases.
    """
    try:
        with urllib.request.build_opener().open(url, timeout=site.HTTP_TIMEOUT) as resp:
            status: int = resp.status
            body: bytes = resp.read()
    except urllib.error.HTTPError as exc:
        status, body = exc.code, exc.read()
        exc.close()
        if status != 500:
            raise CheckError(f"GET {url}: HTTP {status}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise CheckError(f"GET {url}: {exc}") from exc
    return status, site.parse_gerrit_json(body)


def check_healthcheck(base: str) -> str:
    """Check every enabled check passes; return the checks that passed."""
    url = f"{base}/config/server/healthcheck~status"
    status, report = fetch_report(url)
    if not isinstance(report, dict):
        raise CheckError(f"unexpected healthcheck payload: {report!r}")
    # Besides one object per check, the report carries ts and elapsed
    results = {
        name: str(check.get("result"))
        for name, check in report.items()
        if isinstance(check, dict)
    }
    if not results:
        raise CheckError(f"healthcheck reported no checks: {report!r}")
    failing = [
        f"{name} {result}"
        for name, result in sorted(results.items())
        if result not in HEALTHY
    ]
    if failing:
        raise CheckError(f"HTTP {status}, failing checks: {', '.join(failing)}")
    if status != 200:
        raise CheckError(f"GET {url}: HTTP {status}")
    passed = sorted(name for name, result in results.items() if result == "passed")
    return f"passed: {', '.join(passed) or 'none'}"


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(
        description="Verify the healthcheck plugin reports a healthy site."
    )
    parser.add_argument("--namespace", required=True)
    parser.add_argument(
        "--fullname",
        default="gerrit",
        help="the chart's fullname: Service <fullname>-http (default: gerrit)",
    )
    parser.add_argument("--url-prefix", default="/r")
    parser.add_argument("--local-port", type=int, default=18080)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the healthcheck check and return the process exit code."""
    args = parse_args(argv)
    prefix = site.normalise_prefix(args.url_prefix)
    service = f"{args.fullname}-http"

    def run() -> str:
        with site.port_forward(args.namespace, service, args.local_port, 60) as url:
            return check_healthcheck(url + prefix)

    failures: list[str] = []
    if not site.run_check(failures, "healthcheck status", run):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
