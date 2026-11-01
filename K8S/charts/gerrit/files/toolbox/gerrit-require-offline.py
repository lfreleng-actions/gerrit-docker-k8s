#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""\
Usage: gerrit-require-offline [--force]

Exit 0 when the Gerrit pod does not exist, so a helper may change the
site volume, and 1 when it does or when the lookup cannot tell.

Looks up GERRIT_POD_DNS, the pod's fully qualified name under the
headless Service, which publishes not-ready addresses: the name resolves
while the pod starts, serves and stops, and does not exist once Gerrit
is scaled to zero. Only a definite "no such name" counts as stopped; a
timeout or any other resolver error refuses. With --force, a refusal
becomes a warning and the exit status is 0.

The check is a point in time, not a lock, and DNS answers can outlive a
scale-down by a few seconds: retry a refusal shortly after scaling down.
"""

from __future__ import annotations

import os
import socket
import sys

USAGE = __doc__ or ""
PRESENT = "present"
ABSENT = "absent"


def pod_state(name: str) -> str:
    """Return PRESENT, ABSENT, or a description of the lookup failure."""
    try:
        socket.getaddrinfo(name, None)
    except socket.gaierror as exc:
        if exc.errno == socket.EAI_NONAME:
            return ABSENT
        return f"lookup of {name} failed: {exc}"
    except (OSError, UnicodeError) as exc:
        return f"lookup of {name} failed: {exc}"
    return PRESENT


def main(argv: list[str]) -> int:
    """Run the check and return the exit status."""
    if argv in (["-h"], ["--help"]):
        print(USAGE, end="")
        return 0
    if argv not in ([], ["--force"]):
        print(USAGE, end="", file=sys.stderr)
        return 2
    name = os.environ.get("GERRIT_POD_DNS", "")
    if not name:
        problem = "GERRIT_POD_DNS is not set, so whether Gerrit runs is unknown"
    else:
        state = pod_state(name)
        if state == ABSENT:
            return 0
        if state == PRESENT:
            problem = f"the Gerrit pod {name} exists; scale Gerrit to zero first"
        else:
            problem = f"cannot tell whether Gerrit runs: {state}"
    if argv == ["--force"]:
        print(f"WARNING: {problem}; continuing because of --force", file=sys.stderr)
        return 0
    print(f"ERROR: {problem}. Pass --force to override.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
