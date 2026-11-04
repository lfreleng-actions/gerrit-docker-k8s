# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Report what an imported site holds on disk, as one JSON object.

``verify-import.py`` pipes this file to ``python3 -`` in the gerrit
container, so it imports nothing beyond the standard library. It
prints the chart's state file, the SSH host key fingerprints, each
repository's ref count and refs/meta/config head, counted as
``scripts/gerrit-site-export.py`` counts them, each repository's ref
names and the objects they point at, and the next change number.

    python3 verify_import_probe.py --site /var/gerrit
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

FOR_EACH_REF = ["for-each-ref", "--format=%(refname) %(objectname) %(*objectname)"]


def git(*args: str) -> str | None:
    """Run git; return its output, or None when it fails."""
    proc = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    return proc.stdout if proc.returncode == 0 else None


def config(site: Path, key: str, default: str) -> str:
    found = git("config", "-f", str(site / "etc" / "gerrit.config"), "--get", key)
    return found.strip() if found and found.strip() else default


def stores(root: Path) -> list[tuple[str, Path]]:
    """Each repository under root, by its path relative to root."""
    found = []
    for top, dirs, _files in os.walk(root):
        for name in sorted(d for d in dirs if d.endswith(".git")):
            dirs.remove(name)
            found.append(
                (Path(top, name).relative_to(root).as_posix(), Path(top, name))
            )
    return sorted(found)


def listing(repo: Path) -> list[list[str]] | None:
    """Each ref's name, object and peeled object; None when git fails."""
    found = git(f"--git-dir={repo}", *FOR_EACH_REF)
    return None if found is None else [ln.split(" ") for ln in found.splitlines()]


def repositories(root: Path) -> dict[str, dict[str, Any]]:
    """Map each repository under root to its ref count and refs/meta/config."""
    found = {}
    for rel, repo in stores(root):
        refs = listing(repo) or []
        meta = [r[2] or r[1] for r in refs if r[0] == "refs/meta/config"]
        found[rel] = {"refs": len(refs), "meta_config": next(iter(meta), None)}
    return found


def ref_maps(root: Path) -> dict[str, dict[str, str] | None]:
    """Map each repository under root to its refs and their objects.

    A ref's object is the one it names, an annotated tag's own id
    rather than its commit's, so a repointed tag shows. A repository
    git cannot list maps to None.
    """
    found: dict[str, dict[str, str] | None] = {}
    for rel, repo in stores(root):
        refs = listing(repo)
        found[rel] = None if refs is None else {r[0]: r[1] for r in refs}
    return found


def host_keys(etc: Path) -> dict[str, str]:
    keys = {}
    for key in sorted(etc.glob("ssh_host_*")):
        if key.suffix == ".pub" or key.is_symlink() or not key.is_file():
            continue
        proc = subprocess.run(
            ["ssh-keygen", "-l", "-E", "sha256", "-f", str(key)],
            capture_output=True,
            text=True,
            check=False,
        )
        fields = proc.stdout.split()
        keys[key.name] = fields[1] if proc.returncode == 0 and len(fields) > 1 else ""
    return keys


def state(site: Path) -> Any:
    try:
        return json.loads((site / ".chart" / "site-state.json").read_text())
    except (OSError, ValueError):
        return None


def change_sequence(store: Path, all_projects: str) -> int | None:
    repo = store / f"{all_projects}.git"
    found = git(f"--git-dir={repo}", "cat-file", "blob", "refs/sequences/changes")
    try:
        return int(found.strip()) if found else None
    except ValueError:
        return None


def probe(site: Path) -> dict[str, Any]:
    """Everything the import verification reads from the site's disk."""
    store = site / config(site, "gerrit.basePath", "git")
    return {
        "state": state(site),
        "ssh_host_keys": host_keys(site / "etc"),
        "repositories": repositories(store),
        "refs": ref_maps(store),
        "change_sequence": change_sequence(
            store, config(site, "gerrit.allProjects", "All-Projects")
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").partition("\n")[0])
    parser.add_argument("--site", type=Path, default=Path("/var/gerrit"))
    args = parser.parse_args(argv)
    json.dump(probe(args.site), sys.stdout, sort_keys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
