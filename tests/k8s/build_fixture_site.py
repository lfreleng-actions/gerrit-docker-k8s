# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Build a Gerrit site with an official image and export it.

Runs ``gerritcodereview/gerrit:<tag>``, fills it with
``fixture_content.py``, snapshots what it serves with
``verify_import_snapshot.py``, stops it, copies its site out, adds a
Prolog ``rules.pl`` to one project, and exports the site with
``scripts/gerrit-site-export.py final --pack``. It writes:

- ``<out>/site``: the stopped site;
- ``<out>/export``: the export to import;
- ``<out>/expected.json``: the snapshot, each repository's refs and
  their objects, the change to push to, the next change number and
  the warnings the import must report;
- ``<out>/password``: the HTTP password of the user who pushes, mode
  0600; keep it out of uploaded artifacts.

Run it from the repository root with docker, git and ssh-keygen:

    python3 tests/k8s/build_fixture_site.py --tag 3.9.11-ubuntu22 --out build/fixture
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "K8S" / "charts" / "gerrit" / "files" / "scripts"
EXPORT = ROOT / "scripts" / "gerrit-site-export.py"
IMAGE = "gerritcodereview/gerrit"
CONTAINER_SITE = "/var/gerrit"
PROLOG_PROJECT = "rules"
# Substrings of the warnings site-import writes to the state file
WARN_PROLOG = "carry rules.pl"
WARN_IMPLICIT_MERGES = "does not set receive.rejectImplicitMerges"

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixture_content  # noqa: E402


def _load(name: str, file: str) -> ModuleType:
    """Load one of the chart's scripts, which are not a package."""
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / file)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {file}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


snapshot = _load("fixture_snapshot", "verify_import_snapshot.py")
probe = _load("fixture_probe", "verify_import_probe.py")


def run(*cmd: str, capture: bool = False) -> str:
    proc = subprocess.run(
        cmd, check=True, text=True, stdout=subprocess.PIPE if capture else None
    )
    return proc.stdout or ""


def git(repo: Path, *args: str, stdin: str | None = None) -> str:
    proc = subprocess.run(
        ["git", f"--git-dir={repo}", *args], input=stdin, capture_output=True, text=True
    )
    if proc.returncode:
        raise fixture_content.FixtureError(
            f"git {args[0]} in {repo}: {proc.stderr.strip()}"
        )
    return proc.stdout.strip()


def wait_for(base: str, timeout: float) -> str:
    """Wait for Gerrit to answer; return its version."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            with urllib.request.urlopen(
                f"{base}/config/server/version", timeout=10
            ) as resp:
                return str(
                    json.loads(resp.read().decode().removeprefix(fixture_content.XSSI))
                )
        except (urllib.error.URLError, OSError, ValueError):
            if time.monotonic() > deadline:
                raise
            time.sleep(3)


def admin_client(site: ModuleType, base: str) -> fixture_content.Client:
    opener, xsrf, _account = site.start_session(base)
    return fixture_content.Client(base, opener, {"X-Gerrit-Auth": xsrf})


def add_prolog(store: Path) -> None:
    """Give one project a rules.pl, as a site that used Prolog has."""
    repo = store / f"{PROLOG_PROJECT}.git"
    blob = git(repo, "hash-object", "-w", "--stdin", stdin="submit_rule(submit(ok)).\n")
    parent = git(repo, "rev-parse", "refs/meta/config")
    tree = git(repo, "ls-tree", parent)
    tree = git(repo, "mktree", stdin=f"{tree}\n100644 blob {blob}\trules.pl\n")
    commit = git(
        repo,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.org",
        "commit-tree",
        tree,
        "-p",
        parent,
        "-m",
        "Add a Prolog submit rule",
    )
    git(repo, "update-ref", "refs/meta/config", commit, parent)


def expected_warnings(store: Path) -> list[str]:
    """The warnings site-import must report for this site."""
    warnings = [WARN_PROLOG]
    blob = "refs/meta/config:project.config"
    proc = subprocess.run(
        [
            "git",
            f"--git-dir={store / 'All-Projects.git'}",
            "config",
            "--blob",
            blob,
            "--get",
            "receive.rejectImplicitMerges",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode or not proc.stdout.strip():
        warnings.append(WARN_IMPLICIT_MERGES)
    return warnings


def source_refs(store: Path) -> dict[str, dict[str, str]]:
    """Each exported repository's refs and their objects; all must list."""
    found = probe.ref_maps(store)
    if failed := sorted(name for name, refs in found.items() if refs is None):
        raise RuntimeError(f"git cannot list the refs of {', '.join(failed)}")
    return {name: refs for name, refs in found.items() if refs is not None}


def build(tag: str, out: Path, timeout: float) -> dict[str, Any]:
    """Run the image, fill and stop it, and copy its site to out/site."""
    name = f"fixture-{tag.replace('.', '-')}"
    scratch = out / "scratch"
    scratch.mkdir(parents=True)
    run(
        "docker",
        "run",
        "--detach",
        "--name",
        name,
        "--publish",
        "127.0.0.1::8080",
        f"{IMAGE}:{tag}",
    )
    try:
        port = run("docker", "port", name, "8080", capture=True).split(":")[-1].strip()
        base = f"http://127.0.0.1:{port}"
        print(f"Gerrit {wait_for(base, timeout)} answers at {base}", flush=True)
        admin = admin_client(_load("fixture_site", "verify-site.py"), base)
        filled = fixture_content.populate(base, admin, scratch)
        taken = snapshot.take(admin.get, filled["users"], filled["changes"])
        run("docker", "stop", "--time", "120", name)
        run("docker", "cp", f"{name}:{CONTAINER_SITE}", str(out / "site"))
    finally:
        subprocess.run(
            ["docker", "logs", name],
            stdout=(out / "gerrit.log").open("w"),
            stderr=subprocess.STDOUT,
            check=False,
        )
        subprocess.run(
            ["docker", "rm", "--force", name], capture_output=True, check=False
        )
    return {**filled, "snapshot": taken}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build and export a Gerrit fixture site."
    )
    parser.add_argument("--tag", required=True, help="a gerritcodereview/gerrit tag")
    parser.add_argument("--out", type=Path, required=True, help="a directory to create")
    parser.add_argument("--timeout", type=float, default=600, help="seconds to start")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True)
    filled = build(args.tag, args.out, args.timeout)
    store = args.out / "site" / "git"
    add_prolog(store)
    refs = source_refs(store)
    sequence = int(
        git(store / "All-Projects.git", "cat-file", "blob", "refs/sequences/changes")
    )
    run(
        sys.executable,
        str(EXPORT),
        "final",
        "--pack",
        "--i-stopped-writes",
        "--site",
        str(args.out / "site"),
        "--out",
        str(args.out / "export"),
    )
    password = args.out / "password"
    password.touch(mode=0o600)
    password.write_text(filled.pop("password") + "\n")
    expected = {
        "tag": args.tag,
        "snapshot": filled["snapshot"],
        "push": filled["push"],
        "change_sequence": sequence,
        "refs": refs,
        "warnings": expected_warnings(store),
    }
    (args.out / "expected.json").write_text(
        json.dumps(expected, indent=2, sort_keys=True)
    )
    print(json.dumps(expected, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
