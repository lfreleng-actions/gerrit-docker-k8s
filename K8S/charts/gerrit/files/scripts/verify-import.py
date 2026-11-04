# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Verify a release imported a site export with nothing lost.

Compares a release whose ``site-import`` init container imported an
export against that export's ``manifest.json`` and the snapshot
``tests/k8s/build_fixture_site.py`` took of the source site:

- the chart's state file records a complete import of this manifest,
  with each expected warning;
- every exported SSH host key is there, unchanged;
- every repository is there; each project repository has its ref
  count and refs/meta/config head, and each system one at least its
  ref count, since init may add refs;
- each project repository holds exactly the refs the source site
  held, each naming the same object; each system one holds every
  source ref, on the same object but for the refs init and Gerrit
  update (refs/meta/config, refs/meta/version, refs/sequences/*);
- the change number sequence did not go back;
- over REST, as the site's administrator: the accounts, the fixture
  users with their identities and SSH keys, the groups and members,
  the projects and their states, the open changes the index finds,
  each recorded change's status, patch sets, votes, comments and the
  administrator's drafts and stars, and All-Projects' access rules,
  equal to the source's: none missing, changed or added;
- a fixture user can push a new patch set to a change over HTTP.

The push comes last: it changes the site. Run it before any check
that would add to the site, and only on a release under test.

    python3 K8S/charts/gerrit/files/scripts/verify-import.py \\
        --namespace gerrit-import --manifest export/manifest.json \\
        --expected fixture/expected.json --password-file fixture/password
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType
from typing import Any

PROBE = Path(__file__).with_name("verify_import_probe.py")
SYSTEM_REPOSITORIES = ("All-Projects.git", "All-Users.git")
# Refs of a system repository whose object init or Gerrit may move
UPDATED_REFS = ("refs/meta/config", "refs/meta/version")
UPDATED_PREFIXES = ("refs/sequences/",)


def _load(name: str, filename: str) -> ModuleType:
    """Load a script from beside this one."""
    path = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


site = _load("verify_import_site", "verify-site.py")
snapshot = _load("verify_import_snapshot", "verify_import_snapshot.py")
CheckError = site.CheckError


def fail_on(problems: list[str], detail: str) -> str:
    """Raise one CheckError naming every problem, or return detail."""
    if problems:
        raise CheckError("; ".join(problems[:20]))
    return detail


def check_state(found: Any, digest: str, warnings: list[str]) -> str:
    """The state file records a complete import of this manifest."""
    if not isinstance(found, dict):
        raise CheckError("no readable .chart/site-state.json")
    problems = []
    if found.get("state") != "complete":
        problems.append(f"state is {found.get('state')!r}, not 'complete'")
    if found.get("manifestSha256") != digest:
        problems.append(
            f"manifestSha256 is {found.get('manifestSha256')}, not {digest}"
        )
    recorded = [str(w) for w in found.get("warnings") or []]
    for want in warnings:
        if not any(want in line for line in recorded):
            problems.append(f"no warning names {want!r}")
    return fail_on(problems, f"{len(recorded)} warnings")


def check_host_keys(expected: dict[str, str], found: dict[str, str]) -> str:
    """Every exported host key is there with its fingerprint."""
    problems = [
        f"{name} is {found.get(name) or 'missing'}, not {print_}"
        for name, print_ in sorted(expected.items())
        if found.get(name) != print_
    ]
    return fail_on(problems, f"{len(expected)} keys")


def check_repositories(expected: dict[str, Any], found: dict[str, Any]) -> str:
    """Project repositories are as exported; system ones may only grow."""
    problems = []
    for name, want in sorted(expected.items()):
        have = found.get(name)
        if have is None:
            problems.append(f"{name} missing")
        elif name in SYSTEM_REPOSITORIES:
            if have["refs"] < want["refs"]:
                problems.append(
                    f"{name} has {have['refs']} refs, fewer than {want['refs']}"
                )
        elif have != want:
            problems.append(f"{name} is {have}, the manifest {want}")
    return fail_on(problems, f"{len(expected)} repositories")


def ref_problems(name: str, want: dict[str, str], have: dict[str, str]) -> list[str]:
    """How one repository's refs differ from the source's."""
    system = name in SYSTEM_REPOSITORIES
    problems = []
    for ref, oid in sorted(want.items()):
        if ref not in have:
            problems.append(f"{name} {ref} missing")
        elif have[ref] != oid and not (
            system and (ref in UPDATED_REFS or ref.startswith(UPDATED_PREFIXES))
        ):
            problems.append(f"{name} {ref} is {have[ref]}, the source {oid}")
    if not system:
        problems.extend(f"{name} {ref} is new" for ref in sorted(set(have) - set(want)))
    return problems


def check_refs(expected: dict[str, Any], found: dict[str, Any]) -> str:
    """Every source ref names its object; project repositories gain none."""
    problems = []
    for name, want in sorted(expected.items()):
        have = found.get(name)
        if have is None:
            problems.append(f"{name} has no ref listing")
        else:
            problems.extend(ref_problems(name, want, have))
    return fail_on(problems, f"{sum(map(len, expected.values()))} refs")


def check_sequence(expected: int, found: int | None) -> str:
    if found is None or found < expected:
        raise CheckError(f"next change number is {found}, before {expected}")
    return f"next change {found}"


def run_probe(namespace: str, pod: str, container: str, site_path: str) -> Any:
    """Run the probe in the gerrit container and return its report."""
    cmd = [
        "kubectl",
        f"--namespace={namespace}",
        "exec",
        "-i",
        pod,
        "-c",
        container,
        "--",
        "python3",
        "-",
        "--site",
        site_path,
    ]
    proc = subprocess.run(
        cmd,
        input=PROBE.read_text(),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if proc.returncode != 0:
        lines = proc.stderr.strip().splitlines()
        raise CheckError(f"probe in {pod}: {lines[-1] if lines else proc.returncode}")
    return json.loads(proc.stdout)


def git_env(user: str, email: str, password: str) -> dict[str, str]:
    """Git's environment for one user over HTTP, credentials kept off argv."""
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {token}",
        "GIT_AUTHOR_NAME": user,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": user,
        "GIT_COMMITTER_EMAIL": email,
    }


def git(work: str, env: dict[str, str], *args: str) -> None:
    proc = subprocess.run(
        ["git", "-C", work, *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
        check=False,
    )
    if proc.returncode != 0:
        lines = [ln for ln in proc.stderr.splitlines() if ln.strip()]
        raise CheckError(f"git {args[0]}: {lines[-1] if lines else proc.returncode}")


def push_patchset(base: str, get: Any, push: dict[str, Any], password: str) -> str:
    """Push a new patch set to the change as its owner; check it landed."""
    ident = push["change"]
    info = get(f"/a/changes/{ident}?o=ALL_REVISIONS")
    before = len(info["revisions"])
    ref = info["revisions"][info["current_revision"]]["ref"]
    url = f"{base}/a/{info['project']}"
    env = git_env(push["user"], push["email"], password)
    with tempfile.TemporaryDirectory(prefix="verify-import-") as work:
        git(work, env, "init", "--quiet")
        git(work, env, "fetch", "--quiet", url, ref)
        git(work, env, "checkout", "--quiet", "FETCH_HEAD")
        Path(work, "imported.txt").write_text("pushed after the import\n")
        git(work, env, "add", "imported.txt")
        git(work, env, "commit", "--quiet", "--amend", "--no-edit")
        git(work, env, "push", "--quiet", url, f"HEAD:refs/for/{info['branch']}")
    after = len(get(f"/a/changes/{ident}?o=ALL_REVISIONS")["revisions"])
    if after != before + 1:
        raise CheckError(
            f"{ident} has {after} patch sets after the push, not {before + 1}"
        )
    return f"{ident} patch set {after}"


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify a release imported a site export with nothing lost."
    )
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--fullname", default="gerrit")
    parser.add_argument("--pod", default="gerrit-0")
    parser.add_argument("--container", default="gerrit")
    parser.add_argument("--site", default="/var/gerrit")
    parser.add_argument("--url-prefix", default="/r")
    parser.add_argument("--local-port", type=int, default=18080)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--expected", type=Path, required=True)
    parser.add_argument("--password-file", type=Path, required=True)
    parser.add_argument(
        "--expect-warning",
        action="append",
        default=[],
        help="text a warning in the state file must contain; repeatable",
    )
    parser.add_argument("--report", type=Path, help="write what was found here")
    return parser.parse_args(argv)


def check_disk(
    args: argparse.Namespace, found: dict[str, Any], failures: list[str]
) -> None:
    manifest = json.loads(args.manifest.read_text())
    expected = json.loads(args.expected.read_text())
    digest = hashlib.sha256(args.manifest.read_bytes()).hexdigest()
    warnings = [*expected.get("warnings", []), *args.expect_warning]
    checks = {
        "state file": lambda: check_state(found["state"], digest, warnings),
        "SSH host keys": lambda: check_host_keys(
            manifest["ssh_host_keys"], found["ssh_host_keys"]
        ),
        "repositories": lambda: check_repositories(
            manifest["git"]["repositories"], found["repositories"]
        ),
        "refs": lambda: check_refs(expected["refs"], found["refs"]),
        "change sequence": lambda: check_sequence(
            expected["change_sequence"], found["change_sequence"]
        ),
    }
    for name, check in checks.items():
        site.run_check(failures, name, check)


def check_rest(
    args: argparse.Namespace, report: dict[str, Any], failures: list[str]
) -> None:
    expected = json.loads(args.expected.read_text())
    prefix = site.normalise_prefix(args.url_prefix)
    service = f"{args.fullname}-http"
    with site.port_forward(args.namespace, service, args.local_port, 60) as url:
        base = url + prefix
        opener, _xsrf, _account = site.start_session(base)

        def get(path: str) -> Any:
            return site.parse_gerrit_json(site.http_get(opener, base + path))

        def compare() -> str:
            report["snapshot"] = snapshot.take(
                get,
                sorted(expected["snapshot"]["users"]),
                sorted(expected["snapshot"]["changes"]),
            )
            problems = snapshot.compare(expected["snapshot"], report["snapshot"])
            return fail_on(problems, f"{len(report['snapshot']['changes'])} changes")

        def push() -> str:
            password = args.password_file.read_text().strip()
            return push_patchset(base, get, expected["push"], password)

        if site.run_check(failures, "REST snapshot", compare):
            site.run_check(failures, "new patch set over HTTP", push)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    failures: list[str] = []
    report: dict[str, Any] = {}

    def probe() -> str:
        report["probe"] = run_probe(args.namespace, args.pod, args.container, args.site)
        return f"{len(report['probe']['repositories'])} repositories"

    if site.run_check(failures, "site probe", probe):
        check_disk(args, report["probe"], failures)
    site.run_check(failures, "REST", lambda: check_rest(args, report, failures))
    if args.report:
        args.report.write_text(json.dumps({"failures": failures, **report}, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
