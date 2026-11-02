# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Verify that a change runs the site's patchset-created hook.

Port-forwards the release's HTTP Service, creates a project with an
empty commit on ``main`` and a change for review in it, then waits
until the hook log in the Gerrit container names that project. The
smoke install's stub hook appends its arguments to that log.

Creating the change through REST fires the same patchset-created event
as a push to ``refs/for/main``, without the HTTP credentials a push
needs. Like the persistence check in ``verify-site.py``, it relies on
the ``DEVELOPMENT_BECOME_ANY_ACCOUNT`` authentication, and it reuses
that script's helpers.

Run it from the repository root, for example:

    python3 K8S/charts/gerrit/files/scripts/verify-hook.py \\
        --namespace gerrit-smoke --hook-log /var/gerrit/logs/smoke-hook.log
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import secrets
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import ModuleType
from typing import Any


def _load_site() -> ModuleType:
    """Load ``verify-site.py`` from beside this script."""
    path = Path(__file__).with_name("verify-site.py")
    spec = importlib.util.spec_from_file_location("verify_hook_site", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


site = _load_site()
CheckError = site.CheckError


def send_json(
    opener: urllib.request.OpenerDirector,
    xsrf: str,
    method: str,
    url: str,
    payload: dict[str, Any],
) -> Any:
    """Send a JSON request in a session; return the decoded reply."""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        method=method,
        headers={"Content-Type": "application/json", "X-Gerrit-Auth": xsrf},
    )
    try:
        with opener.open(req, timeout=site.HTTP_TIMEOUT) as resp:
            return site.parse_gerrit_json(resp.read())
    except urllib.error.HTTPError as exc:
        raise CheckError(f"{method} {url}: HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise CheckError(f"{method} {url}: {exc}") from exc


def create_test_change(base: str) -> str:
    """Create a project with a change for review; return the project name."""
    opener, xsrf, _ = site.start_session(base)
    name = f"verify-hook-{secrets.token_hex(6)}"
    project = {
        "description": "verify-hook.py hook check",
        "create_empty_commit": True,
        "branches": ["main"],
    }
    created = send_json(opener, xsrf, "PUT", f"{base}/projects/{name}", project)
    if not isinstance(created, dict) or created.get("id") != name:
        raise CheckError(f"create project {name}: unexpected reply {created!r}")
    change = {"project": name, "branch": "main", "subject": "verify-hook.py check"}
    reply = send_json(opener, xsrf, "POST", f"{base}/changes/", change)
    if not isinstance(reply, dict) or reply.get("project") != name:
        raise CheckError(f"create a change in {name}: unexpected reply {reply!r}")
    return name


def wait_for_hook(
    namespace: str, pod: str, path: str, project: str, timeout: float
) -> None:
    """Wait until the hook log in the Gerrit container names *project*."""
    want = f" --project {project} "
    deadline = time.monotonic() + timeout
    while True:
        try:
            log = site.kubectl(
                namespace, "exec", pod, "-c", "gerrit", "--", "cat", path
            )
        except CheckError:
            log = ""
        if any(want in f" {line} " for line in log.splitlines()):
            return
        if time.monotonic() > deadline:
            raise CheckError(f"no hook call in {path} names {project} in {timeout}s")
        time.sleep(2)


def check_hook(base: str, namespace: str, pod: str, path: str, timeout: float) -> str:
    """Create a test change and wait for the stub hook to log it."""
    project = create_test_change(base)
    wait_for_hook(namespace, pod, path, project, timeout)
    return project


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(
        description="Verify a change runs the site's patchset-created hook."
    )
    parser.add_argument("--namespace", required=True)
    parser.add_argument(
        "--fullname",
        default="gerrit",
        help="the chart's fullname: pod <fullname>-0, "
        "Service <fullname>-http (default: gerrit)",
    )
    parser.add_argument("--url-prefix", default="/r")
    parser.add_argument(
        "--hook-log",
        required=True,
        help="file in the Gerrit container the stub hook appends its arguments to",
    )
    parser.add_argument("--local-port", type=int, default=18080)
    parser.add_argument(
        "--timeout",
        type=float,
        default=120,
        help="seconds to wait for the hook to log the change (default: 120)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the hook check and return the process exit code."""
    args = parse_args(argv)
    prefix = site.normalise_prefix(args.url_prefix)
    service = f"{args.fullname}-http"
    pod = f"{args.fullname}-0"

    def run() -> str:
        with site.port_forward(args.namespace, service, args.local_port, 60) as url:
            return check_hook(
                url + prefix, args.namespace, pod, args.hook_log, args.timeout
            )

    failures: list[str] = []
    if not site.run_check(failures, "patchset-created hook runs", run):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
