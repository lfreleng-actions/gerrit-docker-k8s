# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Wait for a pod to become Ready, and fail as soon as it cannot.

Polls ``kubectl get pod`` and exits 0 once the pod is Ready. It exits
1, naming the reason, as soon as an init container exits non-zero or
backs off, or a container exits, restarts or backs off, rather than
waiting out the deadline as ``helm install --wait`` does; and when the
timeout passes.

    python3 tests/k8s/watch_pod.py --namespace gerrit-import --pod gerrit-0
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from typing import Any

# Waiting reasons a container does not recover from on its own
STUCK = (
    "CrashLoopBackOff",
    "ImagePullBackOff",
    "ErrImagePull",
    "InvalidImageName",
    "CreateContainerError",
    "CreateContainerConfigError",
)


def container_problem(found: dict[str, Any], init: bool) -> str:
    """Why one container status means the pod will not start, or ""."""
    kind = "init container" if init else "container"
    where = f"{kind} {found.get('name')}"
    state, last = found.get("state") or {}, found.get("lastState") or {}
    for ended in (state.get("terminated"), last.get("terminated")):
        # An init container that exits 0 is done; any other exit fails
        if ended and (not init or ended.get("exitCode")):
            return (
                f"{where} exited {ended.get('exitCode')} "
                f"({ended.get('reason') or 'no reason'})"
            )
    waiting = (state.get("waiting") or {}).get("reason")
    if waiting in STUCK:
        return f"{where} is in {waiting}"
    if not init and found.get("restartCount"):
        return f"{where} restarted {found['restartCount']} times"
    return ""


def verdict(pod: dict[str, Any]) -> tuple[str, str]:
    """("ready", ""), ("failed", why) or ("waiting", "")."""
    status = pod.get("status") or {}
    for init, key in ((True, "initContainerStatuses"), (False, "containerStatuses")):
        for found in status.get(key) or []:
            if problem := container_problem(found, init):
                return "failed", problem
    for condition in status.get("conditions") or []:
        if condition.get("type") == "Ready" and condition.get("status") == "True":
            return "ready", ""
    return "waiting", ""


def get_pod(namespace: str, pod: str) -> dict[str, Any] | None:
    proc = subprocess.run(
        ["kubectl", "get", "pod", "-n", namespace, pod, "-o", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    return json.loads(proc.stdout) if proc.returncode == 0 else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").partition("\n")[0])
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--pod", required=True)
    parser.add_argument("--timeout", type=float, default=1200, help="seconds")
    parser.add_argument("--interval", type=float, default=5, help="seconds")
    args = parser.parse_args(argv)
    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        found = get_pod(args.namespace, args.pod)
        state, why = verdict(found) if found else ("waiting", "")
        if state == "ready":
            print(f"{args.pod} is Ready")
            return 0
        if state == "failed":
            print(f"{args.pod} will not start: {why}", file=sys.stderr)
            return 1
        time.sleep(args.interval)
    print(f"{args.pod} is not Ready after {args.timeout:.0f}s", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
