# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for scripts/gerrit-site-export.py.

Each test builds a small Gerrit site fixture (bare repositories with
refs, an ``etc/gerrit.config`` naming ``basePath``, SSH host keys and
the entries the export must leave out) and runs the tool the way the
documentation does, as ``python3 scripts/gerrit-site-export.py``.

The tool must run on Python 3.8, and CI runs this module under 3.8 as
well, so it keeps to 3.8 syntax and APIs too.

Process detection reads a fake ``/proc`` tree named by the
``GERRIT_SITE_EXPORT_PROC`` variable, which the tool reserves for tests.
Every test starts with one holding only pid 1 and the exporter's own
``self`` entry, sharing one root and mount namespace, so processes on
the host running the suite cannot affect it; the tests that exercise
the real ``/proc`` point the variable back at it.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "gerrit-site-export.py"
PROC_ENV = "GERRIT_SITE_EXPORT_PROC"
GERRIT_VERSION = "3.10.1"
EXCLUDED = ["cache", "index", "logs", "tmp", "bin", ".gerritcodereview/tmp", ".chart"]
ARCHIVES = ["git.tar.zst", "site.tar.zst"]
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "Fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
}
HAS_PROC = os.path.isdir("/proc/self")
# The mount namespace every fake process, and the exporter, shares by default.
MNT_NS = "mnt:[4026531841]"
# Stands in for zstd: passes data through and accepts any archive.
FAKE_ZSTD = 'if [ "$1" = -tq ]; then exit 0; fi; cat'

pytestmark = pytest.mark.skipif(
    not (shutil.which("rsync") and shutil.which("git")),
    reason="the export needs rsync and git",
)
needs_zstd = pytest.mark.skipif(not shutil.which("zstd"), reason="packing needs zstd")
needs_proc = pytest.mark.skipif(not HAS_PROC, reason="needs a Linux /proc")


@pytest.fixture(autouse=True)
def empty_proc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point process detection at a fake /proc holding an init and self."""
    proc = tmp_path / "proc"
    fake_process(proc, 1, ["/sbin/init"], Path("/"))
    fake_process(proc, "self", ["python3"], Path("/"))
    monkeypatch.setenv(PROC_ENV, str(proc))
    monkeypatch.delenv("GERRIT_SITE", raising=False)
    return proc


def _git(repo: Path, *args: str, stdin: str | None = None) -> str:
    result = subprocess.run(
        ["git", "--git-dir", str(repo), *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **GIT_ENV},
    )
    return result.stdout.strip()


def _bare_repo(path: Path, branches: list[str], meta_config: bool) -> str | None:
    """Create a bare repository holding one commit under each ref."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--bare", "-q", str(path)],
        check=True,
        capture_output=True,
    )
    tree = _git(path, "mktree", stdin="")
    commit = _git(path, "commit-tree", tree, "-m", "Initial")
    for branch in branches:
        _git(path, "update-ref", f"refs/heads/{branch}", commit)
    if not meta_config:
        return None
    meta = _git(path, "commit-tree", tree, "-m", "Project config")
    _git(path, "update-ref", "refs/meta/config", meta)
    return meta


def _write_war(path: Path, version: str = f"v{GERRIT_VERSION}") -> None:
    """Lay out a war as a release does: its version in a nested jar."""
    nested = io.BytesIO()
    with zipfile.ZipFile(nested, "w") as jar:
        jar.writestr("com/google/gerrit/common/Version", f"{version}\n")
    with zipfile.ZipFile(path, "w") as war:
        war.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\r\n")
        war.writestr(
            "WEB-INF/lib/com_google_gerrit_common_libversion.jar", nested.getvalue()
        )


@pytest.fixture()
def site(tmp_path: Path) -> dict[str, Any]:
    """Build a Gerrit site and return its paths and expected values."""
    root = tmp_path / "site"
    (root / "etc").mkdir(parents=True)
    (root / "etc" / "gerrit.config").write_text(
        '[gerrit]\n\tbasePath = git\n\tcanonicalWebUrl = "http://localhost/"\n'
    )
    for name in ("cache", "index", "logs", "tmp", ".gerritcodereview/tmp"):
        (root / name).mkdir(parents=True)
        (root / name / "data").write_text(f"{name} contents\n")
    (root / ".gerritcodereview" / "keep").write_text("kept\n")
    (root / "plugins").mkdir()
    (root / "plugins" / "replication.jar").write_bytes(b"jar")
    (root / "bin").mkdir()
    _write_war(root / "bin" / "gerrit.war")

    meta = {
        "All-Projects.git": _bare_repo(
            root / "git" / "All-Projects.git", [], meta_config=True
        ),
        "apps/web.git": _bare_repo(
            root / "git" / "apps" / "web.git", ["master", "stable"], meta_config=True
        ),
        "plain.git": _bare_repo(root / "git" / "plain.git", ["main"], False),
    }
    refs = {"All-Projects.git": 1, "apps/web.git": 3, "plain.git": 1}

    fingerprints: dict[str, str] = {}
    if shutil.which("ssh-keygen"):
        key = root / "etc" / "ssh_host_ed25519_key"
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
            check=True,
        )
        listing = subprocess.run(
            ["ssh-keygen", "-l", "-E", "sha256", "-f", str(key)],
            stdout=subprocess.PIPE,
            text=True,
            check=True,
        ).stdout
        fingerprints[key.name] = listing.split()[1]

    return {
        "root": root,
        "out": tmp_path / "export",
        "meta": meta,
        "refs": refs,
        "fingerprints": fingerprints,
    }


def repositories_of(site: dict[str, Any]) -> dict[str, Any]:
    """The manifest's git.repositories for the fixture site's repositories."""
    return {
        name: {
            "refs": site["refs"][name],
            "meta_config": site["meta"][name],
            "head": _git(site["root"] / "git" / name, "symbolic-ref", "HEAD"),
        }
        for name in site["refs"]
    }


def run_export(*args: str, env: dict[str, str] | None = None) -> Any:
    """Run the export tool and return the completed process."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
        check=False,
    )


def run_on(site: dict[str, Any], *args: str, env: dict[str, str] | None = None) -> Any:
    """Run the export tool against the fixture site."""
    return run_export(
        *args, "--site", str(site["root"]), "--out", str(site["out"]), env=env
    )


def export_ok(
    site: dict[str, Any], *args: str, env: dict[str, str] | None = None
) -> dict[str, Any]:
    """Run an export that must succeed and return its manifest."""
    result = run_on(site, *args, env=env)
    assert result.returncode == 0, result.stderr
    manifest: dict[str, Any] = json.loads((site["out"] / "manifest.json").read_text())
    return manifest


def export_fails(
    site: dict[str, Any], message: str, *args: str, env: dict[str, str] | None = None
) -> Any:
    """Run an export that must fail with message and leave no manifest."""
    result = run_on(site, *args, env=env)
    assert result.returncode != 0
    errors = [line for line in result.stderr.splitlines() if ": error: " in line]
    assert len(errors) == 1, result.stderr
    assert message in errors[0]
    assert not (site["out"] / "manifest.json").exists()
    assert not (site["out"] / "manifest.json.tmp").exists()
    return result


def snapshot(root: Path) -> dict[str, tuple[int, float, str]]:
    """Map each path under root to its mode, mtime and content hash."""
    state = {}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
        state[str(path.relative_to(root))] = (info.st_mode, info.st_mtime, digest)
    return state


def tool_path(
    tmp_path: Path, hide: tuple[str, ...] = (), fakes: dict[str, str] | None = None
) -> str:
    """Build a PATH offering every current tool but hide, plus fakes.

    Each fake is a shell script body standing in for the named tool.
    """
    shadow = tmp_path / "bin-shadow"
    shadow.mkdir(exist_ok=True)
    for name, body in (fakes or {}).items():
        fake = shadow / name
        fake.write_text(f"#!/bin/sh\n{body}\n")
        fake.chmod(0o755)
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not os.path.isdir(directory):
            continue
        for entry in os.scandir(directory):
            link = shadow / entry.name
            if entry.name in hide or link.exists() or link.is_symlink():
                continue
            if entry.is_file() and os.access(entry.path, os.X_OK):
                link.symlink_to(entry.path)
    return str(shadow)


def fake_process(
    proc: Path,
    pid: int | str,
    argv: list[str] | None,
    cwd: Path | None = None,
    root: Path | None = Path("/"),
    mnt: str | None = MNT_NS,
) -> Path:
    """Add a process to a fake /proc with its command line, cwd, root and
    mount namespace; by default it shares the exporter's root and namespace."""
    base = proc / str(pid)
    base.mkdir(parents=True)
    if argv is not None:
        (base / "cmdline").write_bytes(b"".join(os.fsencode(a) + b"\0" for a in argv))
    if cwd is not None:
        (base / "cwd").symlink_to(cwd)
    if root is not None:
        (base / "root").symlink_to(root)
    if mnt is not None:
        (base / "ns").mkdir()
        (base / "ns" / "mnt").symlink_to(mnt)
    return base


def _fake_gerrit(argv: list[str], cwd: Path) -> subprocess.Popen[bytes]:
    """Start a real process whose command line reads as the given argv."""
    return subprocess.Popen(
        ["java", "-c", "sleep 60; :", *argv],
        executable="/bin/sh",
        cwd=cwd,
    )


def test_help_documents_every_option() -> None:
    result = run_export("--help")
    assert result.returncode == 0
    for option in (
        "seed",
        "final",
        "backup",
        "--site",
        "--git",
        "--out",
        "--i-stopped-writes",
        "--pack",
        "--encrypt",
        "--upload",
        "--include-cache",
        "--dry-run",
        "--help",
    ):
        assert option in result.stdout, option


def test_seed_builds_layout_without_excluded_dirs(site: dict[str, Any]) -> None:
    manifest = export_ok(site, "seed")
    out = site["out"]

    assert (out / "site" / "etc" / "gerrit.config").is_file()
    assert (out / "site" / "plugins" / "replication.jar").is_file()
    assert (out / "site" / ".gerritcodereview" / "keep").is_file()
    for name in EXCLUDED:
        assert not (out / "site" / name).exists(), name
    assert not (out / "site" / "git").exists()
    for name in site["refs"]:
        assert (out / "git" / name / "HEAD").is_file(), name
    assert manifest["mode"] == "seed"
    assert "consistency" not in manifest


def test_final_manifest_contents(site: dict[str, Any]) -> None:
    manifest = export_ok(site, "final")
    root = site["root"].resolve()

    assert list(manifest) == [
        "schema",
        "created",
        "mode",
        "consistency",
        "gerrit_version",
        "site",
        "git",
        "ssh_host_keys",
        "excluded",
        "archives",
    ]
    assert manifest["schema"] == 1
    assert manifest["mode"] == "final"
    assert manifest["consistency"] == {"git": "verified", "h2": "stopped"}
    assert manifest["created"].endswith("Z")
    assert manifest["gerrit_version"] == GERRIT_VERSION
    assert manifest["site"]["path"] == str(root)
    assert manifest["site"]["bytes"] > 0
    assert manifest["git"]["path"] == str(root / "git")
    assert manifest["git"]["bytes"] > 0
    assert manifest["git"]["repositories"] == repositories_of(site)
    assert manifest["ssh_host_keys"] == site["fingerprints"]
    assert manifest["excluded"] == EXCLUDED
    assert manifest["archives"] == {}


def test_final_reads_the_version_of_a_release_war(site: dict[str, Any]) -> None:
    """The release's own tag, without its leading v."""
    _write_war(site["root"] / "bin" / "gerrit.war", "v3.7.2")

    assert export_ok(site, "final")["gerrit_version"] == "3.7.2"


def test_final_records_no_version_without_the_version_jar(
    site: dict[str, Any],
) -> None:
    """A war's manifest does not name the version a release runs."""
    with zipfile.ZipFile(site["root"] / "bin" / "gerrit.war", "w") as war:
        war.writestr(
            "META-INF/MANIFEST.MF",
            f"Manifest-Version: 1.0\r\nImplementation-Version: {GERRIT_VERSION}\r\n",
        )

    assert export_ok(site, "final")["gerrit_version"] is None


def test_rerun_is_idempotent_and_follows_deletions(site: dict[str, Any]) -> None:
    first = export_ok(site, "seed")
    layout = sorted(str(p.relative_to(site["out"])) for p in site["out"].rglob("*"))
    second = export_ok(site, "seed")
    del first["created"], second["created"]
    assert first == second
    assert layout == sorted(
        str(p.relative_to(site["out"])) for p in site["out"].rglob("*")
    )

    shutil.rmtree(site["root"] / "git" / "plain.git")
    (site["root"] / "plugins" / "replication.jar").unlink()
    manifest = export_ok(site, "final")
    assert not (site["out"] / "git" / "plain.git").exists()
    assert not (site["out"] / "site" / "plugins" / "replication.jar").exists()
    assert "plain.git" not in manifest["git"]["repositories"]


def test_export_never_modifies_the_source(site: dict[str, Any]) -> None:
    before = snapshot(site["root"])
    export_ok(site, "seed")
    export_ok(site, "final")
    assert snapshot(site["root"]) == before


def test_include_cache_copies_the_cache(site: dict[str, Any]) -> None:
    manifest = export_ok(site, "seed", "--include-cache")
    assert (site["out"] / "site" / "cache" / "data").is_file()
    assert "cache" not in manifest["excluded"]

    manifest = export_ok(site, "seed")
    assert not (site["out"] / "site" / "cache").exists()
    assert "cache" in manifest["excluded"]


def test_dry_run_writes_nothing(site: dict[str, Any]) -> None:
    result = run_on(site, "final", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert not site["out"].exists()
    assert "rsync" in result.stdout
    assert "--checksum" in result.stdout
    assert "manifest.json" in result.stdout
    assert "zstd" not in result.stdout


@needs_zstd
def test_dry_run_with_pack_writes_nothing(site: dict[str, Any]) -> None:
    result = run_on(site, "final", "--dry-run", "--pack")
    assert result.returncode == 0, result.stderr
    assert not site["out"].exists()
    assert "zstd" in result.stdout
    assert "site.tar.zst" in result.stdout


def test_dry_run_leaves_a_previous_export_alone(site: dict[str, Any]) -> None:
    export_ok(site, "seed")
    before = snapshot(site["out"])
    result = run_on(site, "final", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert snapshot(site["out"]) == before


def run_lone_copy(site: dict[str, Any], tmp_path: Path, *args: str) -> Any:
    """Run a copy of only the tool, alone in a directory, as on a Gerrit host."""
    lone = tmp_path / "lone"
    lone.mkdir(exist_ok=True)
    tool = Path(shutil.copy(SCRIPT, lone))
    paths = ["--site", str(site["root"]), "--out", str(site["out"])]
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    # -S: the project's editable install puts scripts/ on site-packages' path.
    return subprocess.run(
        [
            sys.executable,
            "-S",
            str(tool),
            *args,
            *(paths if args != ("--help",) else []),
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=lone,
        check=False,
    )


def test_seed_and_final_run_from_a_lone_copy_of_the_tool(
    site: dict[str, Any], tmp_path: Path
) -> None:
    result = run_lone_copy(site, tmp_path, "--help")
    assert result.returncode == 0, result.stderr
    assert "backup" in result.stdout
    result = run_lone_copy(site, tmp_path, "seed")
    assert result.returncode == 0, result.stderr
    manifest = json.loads((site["out"] / "manifest.json").read_text())
    assert manifest["git"]["repositories"] == repositories_of(site)
    result = run_lone_copy(site, tmp_path, "final", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "--checksum" in result.stdout


def test_backup_from_a_lone_copy_names_the_missing_module(
    site: dict[str, Any], tmp_path: Path
) -> None:
    result = run_lone_copy(site, tmp_path, "backup")
    assert result.returncode != 0
    assert result.stderr.splitlines() == [
        "gerrit-site-export.py: error: backup needs gerrit_export_git.py beside"
        " the script, as in the chart image"
    ]
    assert not site["out"].exists()


@needs_zstd
def test_pack_records_archive_checksums(site: dict[str, Any]) -> None:
    manifest = export_ok(site, "final", "--pack")
    archives = manifest["archives"]
    assert sorted(archives) == ARCHIVES
    for name, digest in archives.items():
        data = (site["out"] / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == digest

    tar_path = site["out"] / "site.tar"
    subprocess.run(
        ["zstd", "-q", "-d", str(site["out"] / "site.tar.zst"), "-o", str(tar_path)],
        check=True,
    )
    with tarfile.open(tar_path) as tar:
        names = tar.getnames()
    assert "site/etc/gerrit.config" in names
    assert not any(n.startswith("site/cache") for n in names)

    manifest = export_ok(site, "final")
    assert manifest["archives"] == {}
    assert not (site["out"] / "site.tar.zst").exists()


def test_git_option_overrides_base_path(site: dict[str, Any], tmp_path: Path) -> None:
    repos = tmp_path / "repos"
    shutil.move(str(site["root"] / "git"), str(repos))
    manifest = export_ok(site, "final", "--git", str(repos))
    assert manifest["git"]["path"] == str(repos.resolve())
    assert sorted(manifest["git"]["repositories"]) == sorted(site["refs"])


def test_site_defaults_to_gerrit_site_env(site: dict[str, Any]) -> None:
    result = run_export(
        "seed",
        "--out",
        str(site["out"]),
        env={"GERRIT_SITE": str(site["root"])},
    )
    assert result.returncode == 0, result.stderr
    assert (site["out"] / "manifest.json").is_file()


def test_final_refuses_while_gerrit_runs(
    site: dict[str, Any], empty_proc: Path
) -> None:
    (site["root"] / "logs" / "gerrit.pid").write_text("4242\n")
    fake_process(empty_proc, 4242, ["sleep", "60"])

    result = run_on(site, "final")
    assert result.returncode != 0
    assert "Gerrit is running" in result.stderr
    assert "pid 4242" in result.stderr
    assert not site["out"].exists()

    assert run_on(site, "seed").returncode == 0
    result = run_on(site, "final", "--i-stopped-writes")
    assert result.returncode == 0, result.stderr


@needs_proc
def test_final_honours_pid_file_on_real_proc(site: dict[str, Any]) -> None:
    (site["root"] / "logs" / "gerrit.pid").write_text(f"{os.getpid()}\n")
    result = run_on(site, "final", env={PROC_ENV: "/proc"})
    assert result.returncode != 0
    assert "Gerrit is running" in result.stderr
    assert not site["out"].exists()


@needs_proc
@pytest.mark.parametrize(
    "launch",
    ["relative-war", "default-site-path", "symlinked-site", "site-path-equals"],
)
def test_final_refuses_while_foreground_gerrit_runs(
    site: dict[str, Any], tmp_path: Path, launch: str
) -> None:
    root = site["root"]
    link = tmp_path / "site-link"
    link.symlink_to(root)
    argv, cwd = {
        "relative-war": (["-jar", "bin/gerrit.war", "daemon", "-d", str(root)], root),
        "default-site-path": (["-jar", "bin/gerrit.war", "daemon"], root),
        "symlinked-site": (
            ["-jar", f"{link}/bin/gerrit.war", "daemon", "-d", str(link)],
            tmp_path,
        ),
        "site-path-equals": (
            ["-jar", "/opt/gerrit.war", "daemon", f"--site-path={link}"],
            tmp_path,
        ),
    }[launch]
    proc = _fake_gerrit(argv, cwd)
    try:
        result = run_on(site, "final", env={PROC_ENV: "/proc"})
    finally:
        proc.kill()
        proc.wait()
    assert result.returncode != 0
    assert "--i-stopped-writes" in result.stderr
    assert not site["out"].exists()


@needs_proc
@pytest.mark.parametrize("war", ["other-site", "this-site"])
def test_final_ignores_gerrit_on_another_site(
    site: dict[str, Any], tmp_path: Path, war: str
) -> None:
    other = tmp_path / "other-site"
    (other / "bin").mkdir(parents=True)
    war_path = (other if war == "other-site" else site["root"]) / "bin" / "gerrit.war"
    proc = _fake_gerrit(["-jar", str(war_path), "daemon", "-d", str(other)], tmp_path)
    try:
        export_ok(site, "final", env={PROC_ENV: "/proc"})
    finally:
        proc.kill()
        proc.wait()


def test_final_runs_with_stale_pid_file(site: dict[str, Any]) -> None:
    (site["root"] / "logs" / "gerrit.pid").write_text("999999999\n")
    export_ok(site, "final")


# A classpath long enough that any column-limited listing truncates it.
LONG_CLASSPATH = ":".join(f"/opt/lib/dependency-{i}.jar" for i in range(2000))


@pytest.mark.parametrize(
    "case",
    [
        "absolute-site-path",
        "relative-site-path",
        "default-site-path",
        "site-path-equals",
        "short-site-path",
        "symlinked-site-path",
        "war-outside-the-site",
        "long-command-line",
    ],
)
def test_proc_detects_gerrit_on_this_site(
    site: dict[str, Any], tmp_path: Path, empty_proc: Path, case: str
) -> None:
    root = site["root"]
    link = tmp_path / "site-link"
    link.symlink_to(root)
    war = ["java", "-jar", "/opt/gerrit/gerrit.war", "daemon"]
    argv, cwd = {
        "absolute-site-path": ([*war, "-d", str(root)], tmp_path),
        "relative-site-path": ([*war, "-d", "site"], tmp_path),
        "default-site-path": ([*war, "--console-log"], root),
        "site-path-equals": ([*war, f"--site-path={root}"], tmp_path),
        "short-site-path": ([*war, f"-d{root}"], tmp_path),
        "symlinked-site-path": ([*war, "--site-path", "site-link"], tmp_path),
        "war-outside-the-site": (
            ["java", "-jar", str(tmp_path / "gerrit.war"), "daemon", "-d", str(root)],
            tmp_path,
        ),
        "long-command-line": (
            ["java", "-cp", LONG_CLASSPATH, *war[1:], "-d", str(root)],
            tmp_path,
        ),
    }[case]
    fake_process(empty_proc, 100, ["/sbin/init"], Path("/"))
    fake_process(empty_proc, 200, argv, cwd)

    result = run_on(site, "final")
    assert result.returncode != 0
    assert "Gerrit is running" in result.stderr
    assert "pid 200" in result.stderr
    assert not site["out"].exists()


@pytest.mark.parametrize(
    "case",
    [
        "war-in-this-site-serving-another",
        "not-a-daemon",
        "not-gerrit",
        "zombie",
        "absolute-site-path-without-cwd",
        "vanished",
        "not-gerrit-in-another-namespace",
    ],
)
def test_proc_ignores_processes_not_serving_this_site(
    site: dict[str, Any], tmp_path: Path, empty_proc: Path, case: str
) -> None:
    root = site["root"]
    other = tmp_path / "other-site"
    other.mkdir()
    own_war = str(root / "bin" / "gerrit.war")
    if case == "war-in-this-site-serving-another":
        fake_process(
            empty_proc, 300, ["java", "-jar", own_war, "daemon", "-d", str(other)], root
        )
    elif case == "not-a-daemon":
        fake_process(empty_proc, 300, ["java", "-jar", own_war, "version"], root)
    elif case == "not-gerrit":
        fake_process(empty_proc, 300, ["java", "-jar", "app.jar", "daemon"], root)
    elif case == "zombie":
        fake_process(empty_proc, 300, [], root)
    elif case == "not-gerrit-in-another-namespace":
        argv = ["java", "-jar", "app.jar", "daemon"]
        fake_process(empty_proc, 300, argv, root, root=other, mnt="mnt:[1]")
    elif case == "absolute-site-path-without-cwd":
        fake_process(
            empty_proc, 300, ["java", "-jar", own_war, "daemon", "-d", str(other)]
        )
    else:
        (empty_proc / "300").symlink_to(tmp_path / "gone")

    export_ok(site, "final")


@pytest.mark.parametrize(
    "case",
    [
        "different-root",
        "different-mount-namespace",
        "unreadable-root",
        "unreadable-namespace-link",
        "own-namespace-unreadable",
    ],
)
def test_proc_fails_closed_for_gerrit_in_another_namespace(
    site: dict[str, Any], tmp_path: Path, empty_proc: Path, case: str
) -> None:
    # In a container, -d /var/gerrit names a path in Gerrit's own view.
    chroot = tmp_path / "chroot"
    (chroot / "var" / "gerrit").mkdir(parents=True)
    views: dict[str, dict[str, Any]] = {
        "different-root": {"root": chroot},
        "different-mount-namespace": {"mnt": "mnt:[4026532999]"},
        "unreadable-root": {"root": None},
        "unreadable-namespace-link": {"mnt": None},
    }
    if case == "own-namespace-unreadable":
        (empty_proc / "self" / "ns" / "mnt").unlink()
    war = ["java", "-jar", "/opt/gerrit.war", "daemon", "-d", "/var/gerrit"]
    fake_process(empty_proc, 450, war, Path("/"), **views.get(case, {}))

    result = run_on(site, "final")
    assert result.returncode != 0
    assert "Gerrit (pid 450) runs in another filesystem namespace" in result.stderr
    assert "its site cannot be compared" in result.stderr
    assert "--i-stopped-writes" in result.stderr
    assert not site["out"].exists()

    export_ok(site, "final", "--i-stopped-writes")
    export_ok(site, "seed")


def test_proc_compares_gerrit_in_the_same_namespace(
    site: dict[str, Any], empty_proc: Path
) -> None:
    war = ["java", "-jar", "/opt/gerrit.war", "daemon", "-d"]
    fake_process(empty_proc, 460, [*war, "/var/gerrit-elsewhere"], Path("/"))
    export_ok(site, "final")
    fake_process(empty_proc, 470, [*war, str(site["root"])], Path("/"))
    result = run_on(site, "final")
    assert result.returncode != 0
    assert "Gerrit is running" in result.stderr
    assert "pid 470" in result.stderr


@pytest.mark.parametrize("case", ["unreadable-cmdline", "unknown-cwd", "missing-value"])
def test_proc_fails_closed_when_a_gerrit_cannot_be_inspected(
    site: dict[str, Any], empty_proc: Path, case: str
) -> None:
    war = ["java", "-jar", "/opt/gerrit.war", "daemon"]
    if case == "unreadable-cmdline":
        (fake_process(empty_proc, 400, None, Path("/")) / "cmdline").mkdir()
    elif case == "unknown-cwd":
        fake_process(empty_proc, 400, [*war, "-d", "relative/site"])
    else:
        fake_process(empty_proc, 400, [*war, "-d"], Path("/"))

    result = run_on(site, "final")
    assert result.returncode != 0
    assert "cannot tell which site Gerrit (pid 400) serves" in result.stderr
    assert "--i-stopped-writes" in result.stderr
    assert not site["out"].exists()

    export_ok(site, "final", "--i-stopped-writes")


def test_final_refuses_proc_hiding_pid_1(
    site: dict[str, Any], empty_proc: Path
) -> None:
    # hidepid=invisible and ptraceable hide pid 1 like other users' processes.
    shutil.rmtree(str(empty_proc / "1"))
    fake_process(empty_proc, 700, ["sleep", "60"], Path("/"))
    result = run_on(site, "final")
    assert result.returncode != 0
    assert "hides pid 1 (hidepid?)" in result.stderr
    assert "--i-stopped-writes" in result.stderr
    assert not site["out"].exists()

    export_ok(site, "final", "--i-stopped-writes")
    export_ok(site, "seed")


def test_final_refuses_proc_hiding_process_details(
    site: dict[str, Any], empty_proc: Path
) -> None:
    # hidepid=noaccess lists every process but hides what each runs.
    (empty_proc / "1" / "cmdline").unlink()
    (empty_proc / "1" / "cmdline").mkdir()
    result = run_on(site, "final")
    assert result.returncode != 0
    assert "cannot tell which site Gerrit (pid 1) serves" in result.stderr
    assert not site["out"].exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root traverses any directory")
def test_proc_fails_closed_when_site_path_cannot_be_resolved(
    site: dict[str, Any], tmp_path: Path, empty_proc: Path
) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "alias").symlink_to(site["root"])
    war = ["java", "-jar", "/opt/gerrit.war", "daemon"]
    fake_process(empty_proc, 500, [*war, "-d", str(locked / "alias")], Path("/"))
    locked.chmod(0)
    try:
        result = run_on(site, "final")
    finally:
        locked.chmod(0o755)
    assert result.returncode != 0
    assert "cannot tell which site Gerrit (pid 500) serves" in result.stderr
    assert not site["out"].exists()


def test_final_needs_proc_unless_writes_were_stopped(
    site: dict[str, Any], tmp_path: Path
) -> None:
    env = {PROC_ENV: str(tmp_path / "no-proc")}
    result = run_on(site, "final", env=env)
    assert result.returncode != 0
    assert "cannot list processes" in result.stderr
    assert "--i-stopped-writes" in result.stderr
    assert not site["out"].exists()

    export_ok(site, "final", "--i-stopped-writes", env=env)
    export_ok(site, "seed", env=env)


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ((), "give a mode"),
        (("seed", "seed"), "mode given twice"),
        (("seed", "--bogus"), "unknown argument: --bogus"),
        (("seed", "--out"), "--out needs a value"),
        (("seed", "--encrypt", "rot13:x"), "--encrypt takes"),
        (("seed", "--encrypt", "age:"), "--encrypt takes"),
        (("final", "--upload", "https://example.invalid/x"), "--upload takes"),
        (("seed", "--pack"), "need final or backup mode"),
        (("seed", "--upload", "s3://bucket/prefix"), "need final or backup mode"),
    ],
)
def test_refuses_bad_arguments(
    site: dict[str, Any], args: tuple[str, ...], message: str
) -> None:
    result = run_on(site, *args)
    assert result.returncode != 0
    assert message in result.stderr
    assert len(result.stderr.strip().splitlines()) == 1


def test_refuses_missing_site(tmp_path: Path) -> None:
    result = run_export(
        "seed", "--site", str(tmp_path / "absent"), "--out", str(tmp_path / "o")
    )
    assert result.returncode != 0
    assert "cannot read site directory" in result.stderr


def test_refuses_without_site(tmp_path: Path) -> None:
    result = run_export("seed", "--out", str(tmp_path / "o"))
    assert result.returncode != 0
    assert "no site" in result.stderr


def test_refuses_missing_base_path(site: dict[str, Any]) -> None:
    (site["root"] / "etc" / "gerrit.config").write_text("[gerrit]\n")
    result = run_on(site, "seed")
    assert result.returncode != 0
    assert "gerrit.basePath is not set" in result.stderr


@pytest.mark.parametrize("where", ["inside-site", "inside-git", "site", "parent"])
def test_refuses_export_overlapping_the_source(
    site: dict[str, Any], tmp_path: Path, where: str
) -> None:
    target = {
        "inside-site": site["root"] / "export",
        "inside-git": site["root"] / "git" / "export",
        "site": site["root"],
        "parent": tmp_path,
    }[where]
    before = snapshot(site["root"])
    result = run_export("seed", "--site", str(site["root"]), "--out", str(target))
    assert result.returncode != 0
    assert "overlaps" in result.stderr
    assert snapshot(site["root"]) == before


def test_final_refuses_unreadable_repository(site: dict[str, Any]) -> None:
    (site["root"] / "git" / "broken.git").mkdir()
    export_fails(site, "cannot read refs of repository: broken.git", "final")


def test_refuses_dot_dot_after_missing_directory(
    site: dict[str, Any], tmp_path: Path
) -> None:
    before = snapshot(site["root"])
    out = f"{tmp_path}/missing/../site/export"
    result = run_export("seed", "--site", str(site["root"]), "--out", out)
    assert result.returncode != 0
    assert "overlaps" in result.stderr
    assert snapshot(site["root"]) == before
    assert not (tmp_path / "missing").exists()


def test_accepts_dot_dot_after_existing_directory(
    site: dict[str, Any], tmp_path: Path
) -> None:
    out = f"{tmp_path}/site/../export"
    result = run_export("seed", "--site", str(site["root"]), "--out", out)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "export" / "manifest.json").is_file()


@pytest.mark.parametrize("part", ["site", "git"])
def test_refuses_symlinked_destination(site: dict[str, Any], part: str) -> None:
    site["out"].mkdir()
    target = site["root"] if part == "site" else site["root"] / "git"
    (site["out"] / part).symlink_to(target)
    before = snapshot(site["root"])
    result = run_on(site, "seed")
    assert result.returncode != 0
    assert "refusing to write through symlink" in result.stderr
    assert snapshot(site["root"]) == before


@pytest.mark.parametrize("name", ["manifest.json", "manifest.json.tmp", "site.tar.zst"])
def test_refuses_symlinked_output_file(site: dict[str, Any], name: str) -> None:
    site["out"].mkdir()
    (site["out"] / name).symlink_to(site["root"] / "etc" / "gerrit.config")
    before = snapshot(site["root"])
    result = run_on(site, "seed")
    assert result.returncode != 0
    assert "refusing to write through symlink" in result.stderr
    assert snapshot(site["root"]) == before
    assert not (site["out"] / "site").exists()


def test_refuses_directory_at_manifest_path(site: dict[str, Any]) -> None:
    (site["out"] / "manifest.json").mkdir(parents=True)
    result = run_on(site, "seed")
    assert result.returncode != 0
    assert "not a regular file" in result.stderr
    assert not any((site["out"] / "manifest.json").iterdir())


@pytest.mark.parametrize("target", ["inside", "outside"])
def test_excludes_symlinked_base_path(
    site: dict[str, Any], tmp_path: Path, target: str
) -> None:
    root = site["root"]
    store = root / "repos" if target == "inside" else tmp_path / "store"
    shutil.move(str(root / "git"), str(store))
    (root / "git").symlink_to("repos" if target == "inside" else store)

    manifest = export_ok(site, "seed")
    for name in ("git", "repos"):
        path = site["out"] / "site" / name
        assert not path.exists() and not path.is_symlink(), name
    assert sorted(manifest["git"]["repositories"]) == sorted(site["refs"])
    assert manifest["git"]["path"] == str(store.resolve())
    for path in (site["out"] / "site").rglob("*"):
        assert not path.is_symlink(), path


@pytest.mark.parametrize("given", ["base-path", "git-option"])
def test_excludes_symlinked_ancestor_of_base_path(
    site: dict[str, Any], tmp_path: Path, given: str
) -> None:
    root = site["root"]
    storage = tmp_path / "srv-storage"
    storage.mkdir()
    shutil.move(str(root / "git"), str(storage / "repos"))
    (root / "storage").symlink_to(storage)
    (root / "etc" / "gerrit.config").write_text(
        "[gerrit]\n\tbasePath = storage/repos\n"
    )
    args = ["--git", str(root / "storage" / "repos")] if given == "git-option" else []

    manifest = export_ok(site, "seed", *args)
    assert not os.path.lexists(site["out"] / "site" / "storage")
    for path in (site["out"] / "site").rglob("*"):
        assert not path.is_symlink(), path
    assert sorted(manifest["git"]["repositories"]) == sorted(site["refs"])
    assert manifest["git"]["path"] == str((storage / "repos").resolve())


REFUSED_LINKS = "symlinks in the repository store are not followed"


@pytest.mark.parametrize("mode", ["seed", "final"])
@pytest.mark.parametrize("layout", ["repository", "namespace"])
def test_refuses_linked_repositories(
    site: dict[str, Any], tmp_path: Path, layout: str, mode: str
) -> None:
    store, mnt = site["root"] / "git", tmp_path / "mnt"
    mnt.mkdir()
    name = "plain.git" if layout == "repository" else "apps"
    shutil.move(str(store / name), str(mnt / name))
    (store / name).symlink_to(mnt / name)
    before = snapshot(site["root"])

    result = export_fails(site, REFUSED_LINKS, mode)
    assert str(store / name) in result.stderr
    assert not site["out"].exists()
    assert snapshot(site["root"]) == before


def test_refusal_names_every_linked_path(site: dict[str, Any], tmp_path: Path) -> None:
    store = site["root"] / "git"
    links = [store / "team.git", store / "apps" / "team", store / "plain.git" / "x"]
    for link in links:
        link.symlink_to(tmp_path)
    errors = export_fails(site, REFUSED_LINKS, "seed").stderr
    for link in links:
        assert str(link) in errors, link


@pytest.mark.parametrize("link", ["dangling", "looping"])
def test_refuses_unusable_repository_link(
    site: dict[str, Any], tmp_path: Path, link: str
) -> None:
    target = tmp_path / "gone" if link == "dangling" else Path("..")
    (site["root"] / "git" / "apps" / "mirror.git").symlink_to(target)
    export_fails(site, REFUSED_LINKS, "final")
    export_fails(site, REFUSED_LINKS, "seed")
    assert not site["out"].exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any directory")
def test_unreadable_store_directory_fails_the_copy(site: dict[str, Any]) -> None:
    namespace = site["root"] / "git" / "apps"
    namespace.chmod(0)
    try:
        export_fails(site, "rsync failed with exit status 23", "seed")
    finally:
        namespace.chmod(0o755)


@pytest.mark.parametrize("layout", ["under-linked-repository", "link-to-output"])
def test_refuses_output_reachable_through_a_store_link(
    site: dict[str, Any], tmp_path: Path, layout: str
) -> None:
    store, mnt = site["root"] / "git", tmp_path / "mnt"
    if layout == "under-linked-repository":
        mnt.mkdir()
        shutil.move(str(store / "plain.git"), str(mnt / "plain.git"))
        (store / "plain.git").symlink_to(mnt / "plain.git")
        out = mnt / "plain.git" / "export"
    else:
        out = site["out"]
        out.mkdir()
        (store / "apps" / "out.git").symlink_to(out)
    before = snapshot(site["root"])

    result = run_export("final", "--site", str(site["root"]), "--out", str(out))
    assert result.returncode != 0
    assert REFUSED_LINKS in result.stderr
    assert not out.exists() or not any(out.iterdir())
    assert snapshot(site["root"]) == before


@pytest.mark.parametrize("given", ["base-path", "git-option"])
def test_refuses_non_canonical_repository_path(
    site: dict[str, Any], tmp_path: Path, given: str
) -> None:
    root = site["root"]
    storage = tmp_path / "storage"
    (storage / "child").mkdir(parents=True)
    shutil.move(str(root / "git"), str(storage / "repos"))
    (root / "repos").mkdir()
    (root / "link").symlink_to(storage / "child")
    if given == "base-path":
        (root / "etc" / "gerrit.config").write_text(
            "[gerrit]\n\tbasePath = link/../repos\n"
        )
        result = run_on(site, "seed")
    else:
        result = run_on(site, "seed", "--git", f"{root}/link/../repos")
    assert result.returncode != 0
    assert "has '..' after a symlink" in result.stderr
    assert f"pass {(storage / 'repos').resolve()}" in result.stderr
    assert not site["out"].exists()

    manifest = export_ok(site, "seed", "--git", str(storage / "repos"))
    assert sorted(manifest["git"]["repositories"]) == sorted(site["refs"])


def test_refuses_site_path_with_dot_dot_after_symlink(
    site: dict[str, Any], tmp_path: Path
) -> None:
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "link").symlink_to(tmp_path / "a" / "b")
    given = f"{tmp_path}/link/../../site"
    result = run_export("seed", "--site", given, "--out", str(site["out"]))
    assert result.returncode != 0
    assert f"site path {given} has '..' after a symlink" in result.stderr
    assert not site["out"].exists()


def test_excludes_root_entries_whatever_their_type(
    site: dict[str, Any], tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    for name in ("cache", "logs", "index", "bin"):
        shutil.move(str(site["root"] / name), str(elsewhere / name))
        (site["root"] / name).symlink_to(elsewhere / name)
    (site["root"] / "tmp").rename(tmp_path / "tmp-dir")
    (site["root"] / "tmp").write_text("a file, not a directory\n")

    export_ok(site, "seed")
    for name in ("cache", "logs", "index", "bin", "tmp"):
        path = site["out"] / "site" / name
        assert not path.exists() and not path.is_symlink(), name


@pytest.mark.parametrize("kind", ["directory", "symlink"])
def test_excludes_chart_state(site: dict[str, Any], tmp_path: Path, kind: str) -> None:
    state = tmp_path / "state" if kind == "symlink" else site["root"] / ".chart"
    state.mkdir()
    (state / "site-state.json").write_text('{"source": "release"}\n')
    if kind == "symlink":
        (site["root"] / ".chart").symlink_to(state)

    manifest = export_ok(site, "final")
    path = site["out"] / "site" / ".chart"
    assert not path.exists() and not path.is_symlink()
    assert ".chart" in manifest["excluded"]


def test_final_copies_ref_rewritten_with_same_size_and_mtime(
    site: dict[str, Any],
) -> None:
    export_ok(site, "seed")
    ref = Path("apps/web.git/refs/heads/master")
    source = site["root"] / "git" / ref
    copy = site["out"] / "git" / ref
    source.write_text(f"{site['meta']['apps/web.git']}\n")
    assert source.stat().st_size == copy.stat().st_size
    stamp = copy.stat().st_mtime_ns
    os.utime(source, ns=(stamp, stamp))

    export_ok(site, "final")
    assert copy.read_text() == source.read_text()


@needs_zstd
def test_pack_archives_are_private(site: dict[str, Any]) -> None:
    saved = os.umask(0o022)
    try:
        export_ok(site, "final", "--pack")
    finally:
        os.umask(saved)
    for name in ARCHIVES:
        assert (site["out"] / name).stat().st_mode & 0o077 == 0, name


# Stand-ins that copy the archive and log its permissions while they run.
FAKE_ENCRYPT = {
    "age": 'ls -l "$5" | cut -c1-10 >> "$LOG"; cat "$5" > "$4"',
    "gpg": 'ls -l "$8" | cut -c1-10 >> "$LOG"; cat "$8" > "$7"',
}


@needs_zstd
@pytest.mark.parametrize("kind", ["age", "gpg"])
def test_encrypted_archives_are_private(
    site: dict[str, Any], tmp_path: Path, kind: str
) -> None:
    log = tmp_path / "modes.log"
    path = tool_path(tmp_path, fakes={kind: FAKE_ENCRYPT[kind]})
    saved = os.umask(0o022)
    try:
        manifest = export_ok(
            site,
            "final",
            "--encrypt",
            f"{kind}:recipient",
            env={"PATH": path, "LOG": str(log)},
        )
    finally:
        os.umask(saved)
    names = [f"{name}.{kind}" for name in ARCHIVES]
    assert sorted(manifest["archives"]) == names
    for name in names:
        assert (site["out"] / name).stat().st_mode & 0o077 == 0, name
    for name in ARCHIVES:
        assert not (site["out"] / name).exists(), name
    assert log.read_text().split() == ["-rw-------", "-rw-------"]


def test_final_fails_when_hashing_fails(site: dict[str, Any], tmp_path: Path) -> None:
    # The integrity check swaps the archive for a directory, which
    # cannot be read for hashing.
    zstd = 'if [ "$1" = -tq ]; then rm -f "$2" && mkdir "$2"; else cat; fi'
    path = tool_path(tmp_path, fakes={"zstd": zstd})
    export_fails(
        site,
        "cannot compute the sha256 of site.tar.zst",
        "final",
        "--pack",
        env={"PATH": path},
    )


@pytest.mark.parametrize(
    ("tool", "args", "message"),
    [
        ("rsync", ("seed",), "rsync failed with exit status 23"),
        ("zstd", ("final", "--pack"), "zstd failed with exit status 1"),
        ("ssh-keygen", ("final",), "cannot fingerprint SSH host key"),
        ("aws", ("final", "--upload", "s3://bucket/x"), "removal of the uploaded"),
        ("age", ("final", "--encrypt", "age:r"), "age encryption of"),
    ],
)
def test_failing_step_aborts_without_manifest(
    site: dict[str, Any], tmp_path: Path, tool: str, args: tuple[str, ...], message: str
) -> None:
    if tool == "ssh-keygen" and not site["fingerprints"]:
        pytest.skip("needs ssh-keygen to make host keys")
    fakes = {"zstd": FAKE_ZSTD, tool: "exit 23" if tool == "rsync" else "exit 1"}
    export_fails(site, message, *args, env={"PATH": tool_path(tmp_path, fakes=fakes)})


@pytest.mark.parametrize(
    ("hide", "args", "missing"),
    [
        (("zstd",), ("final", "--pack"), "zstd"),
        (("age",), ("final", "--encrypt", "age:r"), "age"),
        (("gpg",), ("final", "--encrypt", "gpg:KEY"), "gpg"),
        (("aws",), ("final", "--upload", "s3://bucket/x"), "aws"),
        (("ssh-keygen",), ("final",), "ssh-keygen"),
        (("rsync",), ("seed",), "rsync"),
    ],
)
def test_reports_the_missing_tool(
    site: dict[str, Any],
    tmp_path: Path,
    hide: tuple[str, ...],
    args: tuple[str, ...],
    missing: str,
) -> None:
    if missing == "ssh-keygen" and not site["fingerprints"]:
        pytest.skip("needs ssh-keygen to make host keys")
    fakes = {t: "exit 0" for t in ("zstd", "age", "gpg", "aws") if t not in hide}
    path = tool_path(tmp_path, hide=hide, fakes=fakes)
    result = run_on(site, *args, env={"PATH": path})
    assert result.returncode != 0
    assert f"required tool not found: {missing}" in result.stderr
    assert not site["out"].exists()


def test_optional_tools_are_not_needed(site: dict[str, Any], tmp_path: Path) -> None:
    path = tool_path(tmp_path, hide=("zstd", "age", "gpg", "aws"))
    export_ok(site, "seed", env={"PATH": path})
    export_ok(site, "final", env={"PATH": path})


def test_failed_rerun_leaves_no_manifest(site: dict[str, Any], tmp_path: Path) -> None:
    path = tool_path(tmp_path, fakes={"zstd": FAKE_ZSTD})
    manifest = export_ok(site, "final", "--pack", env={"PATH": path})
    assert sorted(manifest["archives"]) == ARCHIVES

    (site["root"] / "git" / "broken.git").mkdir()
    export_fails(
        site, "cannot read refs of repository", "final", "--pack", env={"PATH": path}
    )
    for name in ARCHIVES:
        assert not (site["out"] / name).exists(), name


def test_rerun_removes_the_manifest_before_the_archives(
    site: dict[str, Any], tmp_path: Path
) -> None:
    path = tool_path(tmp_path, fakes={"zstd": FAKE_ZSTD})
    export_ok(site, "final", "--pack", env={"PATH": path})
    result = run_on(site, "final", "--pack", "--dry-run", env={"PATH": path})
    assert result.returncode == 0, result.stderr
    removal = [line for line in result.stdout.splitlines() if " rm -f " in line]
    assert len(removal) == 1, result.stdout
    removed = [Path(p).name for p in removal[0].split()[3:]]
    assert removed[0] == "manifest.json", removed
    assert sorted(removed[1:]) == ARCHIVES


def test_upload_sends_archives_then_manifest(
    site: dict[str, Any], tmp_path: Path
) -> None:
    log = tmp_path / "aws.log"
    fakes = {"zstd": FAKE_ZSTD, "aws": 'echo "$@" >> "$LOG"'}
    env = {"PATH": tool_path(tmp_path, fakes=fakes), "LOG": str(log)}
    export_ok(site, "final", "--upload", "s3://bucket/prefix/", env=env)
    out = site["out"]
    assert log.read_text().splitlines() == [
        "s3 rm s3://bucket/prefix/manifest.json",
        f"s3 cp --sse aws:kms {out}/site.tar.zst s3://bucket/prefix/site.tar.zst",
        f"s3 cp --sse aws:kms {out}/git.tar.zst s3://bucket/prefix/git.tar.zst",
        f"s3 cp --sse aws:kms {out}/manifest.json.tmp s3://bucket/prefix/manifest.json",
    ]


def test_failed_upload_never_publishes_a_manifest(
    site: dict[str, Any], tmp_path: Path
) -> None:
    log = tmp_path / "aws.log"
    fail_git = 'echo "$@" >> "$LOG"; case "$5" in *git.tar.zst) exit 1;; esac'
    fakes = {"zstd": FAKE_ZSTD, "aws": fail_git}
    env = {"PATH": tool_path(tmp_path, fakes=fakes), "LOG": str(log)}
    export_fails(
        site, "upload of git.tar.zst", "final", "--upload", "s3://b/p", env=env
    )
    calls = log.read_text().splitlines()
    assert calls[0] == "s3 rm s3://b/p/manifest.json"
    assert not [c for c in calls if c.endswith("/manifest.json") and " cp " in c]


@needs_zstd
def test_backup_packs_a_running_site(site: dict[str, Any], empty_proc: Path) -> None:
    (site["root"] / "logs" / "gerrit.pid").write_text("4242\n")
    fake_process(empty_proc, 4242, ["sleep", "60"])

    manifest = export_ok(site, "backup")
    assert manifest["mode"] == "backup"
    assert manifest["consistency"] == {"git": "verified", "h2": "best-effort"}
    assert manifest["gerrit_version"] == GERRIT_VERSION
    assert manifest["git"]["repositories"] == repositories_of(site)
    assert sorted(manifest["archives"]) == ARCHIVES
    for name, digest in manifest["archives"].items():
        data = (site["out"] / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == digest
    for name in site["refs"]:
        _git(site["out"] / "git" / name, "fsck", "--no-progress")


def git_shim(
    tmp_path: Path,
    before_bundle: str = "",
    after_fetch: str = "",
    after_bundle: str = "",
) -> str:
    """A PATH whose git runs a script around a backup's repository copy.

    before_bundle and after_bundle run just before and after ``git bundle
    create`` on apps/web.git, as a Gerrit writing at that moment would;
    after_fetch runs after the fetch into its copy. All see the
    repository as $repo.
    """
    shim = tmp_path / "git-shim"
    shim.mkdir()
    real = shutil.which("git")
    (shim / "git").write_text(
        "#!/bin/sh\n"
        "repo=\n"
        "for arg; do case $arg in --git-dir=*) repo=${arg#--git-dir=} ;; esac; done\n"
        'case "$repo $*" in\n'
        f"  */apps/web.git*' bundle create '*) {before_bundle or ':'} ;;\n"
        "esac\n"
        f'{real} "$@" || exit $?\n'
        'case "$repo $*" in\n'
        f"  */apps/web.git*' bundle create '*) {after_bundle or ':'} ;;\n"
        f"  */apps/web.git*' fetch '*) {after_fetch or ':'} ;;\n"
        "esac\n"
    )
    (shim / "git").chmod(0o755)
    return f"{shim}{os.pathsep}{os.environ['PATH']}"


def all_paths(root: Path) -> list[str]:
    """Every path under root, not root itself."""
    return [os.path.join(t, n) for t, d, f in os.walk(root) for n in d + f]


def refs_of(repo: Path) -> str:
    return _git(repo, "for-each-ref", "--format=%(objectname) %(refname)")


def test_backup_copies_repositories_through_git(site: dict[str, Any]) -> None:
    (site["root"] / "git" / "notes.txt").write_text("not a repository\n")
    result = run_on(site, "backup", "--dry-run")
    assert result.returncode == 0, result.stderr
    lines = [shlex.split(line[2:]) for line in result.stdout.splitlines()]
    rsync = [
        line for line in lines if line[0] == "rsync" and line[-1].endswith("/git/")
    ]
    assert len(rsync) == 1, result.stdout
    assert "--exclude=*.git/" in rsync[0]
    assert "--exclude=*.lock" in rsync[0]
    bundles = [line for line in lines if "bundle" in line]
    git_dirs = [a for line in bundles for a in line if a.startswith("--git-dir=")]
    assert sorted(git_dirs) == sorted(
        f"--git-dir={site['root'] / 'git' / name}" for name in site["refs"]
    )


@needs_zstd
def test_backup_keeps_repository_metadata(site: dict[str, Any]) -> None:
    web = site["root"] / "git" / "apps" / "web.git"
    _git(web, "config", "receive.denyNonFastForwards", "true")
    _git(web, "symbolic-ref", "HEAD", "refs/heads/stable")
    (web / "description").write_text("The web app\n")
    (web / "info").mkdir(exist_ok=True)
    (web / "info" / "exclude").write_text("*.tmp\n")
    (site["root"] / "git" / "notes.txt").write_text("not a repository\n")
    subprocess.run(
        ["git", "init", "-q", "--bare", str(site["root"] / "git" / "empty.git")],
        check=True,
    )

    manifest = export_ok(site, "backup")
    copy = site["out"] / "git" / "apps" / "web.git"
    assert refs_of(copy) == refs_of(web)
    assert _git(copy, "config", "receive.denyNonFastForwards") == "true"
    assert _git(copy, "symbolic-ref", "HEAD") == "refs/heads/stable"
    assert _git(copy, "remote") == ""
    assert (copy / "description").read_text() == "The web app\n"
    assert (copy / "info" / "exclude").read_text() == "*.tmp\n"
    assert not (copy / "FETCH_HEAD").exists()
    assert (site["out"] / "git" / "notes.txt").read_text() == "not a repository\n"
    assert manifest["git"]["repositories"]["empty.git"] == {
        "refs": 0,
        "meta_config": None,
        "head": _git(site["root"] / "git" / "empty.git", "symbolic-ref", "HEAD"),
    }
    assert manifest["git"]["repositories"]["apps/web.git"]["head"] == (
        "refs/heads/stable"
    )
    assert not list(site["out"].glob(".scratch-*"))


@needs_zstd
@needs_zstd
def test_backup_copies_a_repository_without_info(site: dict[str, Any]) -> None:
    web = site["root"] / "git" / "apps" / "web.git"
    shutil.rmtree(web / "info", ignore_errors=True)

    export_ok(site, "backup")
    copy = site["out"] / "git" / "apps" / "web.git"
    assert refs_of(copy) == refs_of(web)
    assert not (copy / "info").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any directory")
def test_backup_refuses_an_unreadable_info_directory(site: dict[str, Any]) -> None:
    hidden = site["root"] / "git" / "apps" / "web.git" / "info" / "hidden"
    hidden.mkdir(parents=True)
    (hidden / "attributes").write_text("* -diff\n")
    hidden.chmod(0)
    try:
        export_fails(site, f"cannot read {hidden}", "backup")
    finally:
        hidden.chmod(0o755)
    assert not (site["out"] / "git.tar.zst").exists()
    assert not list(site["out"].glob(".scratch-*"))


def test_backup_survives_refs_packed_during_the_copy(
    site: dict[str, Any], tmp_path: Path
) -> None:
    web = site["root"] / "git" / "apps" / "web.git"
    _git(web, "update-ref", "refs/changes/01/1/1", _git(web, "rev-parse", "master"))
    set_sequence(site["root"] / "git" / "All-Projects.git", "2")
    # Gerrit packs the refs and moves master just as the copy starts.
    moved = (
        'git --git-dir="$repo" pack-refs --all --prune && '
        'next=$(git --git-dir="$repo" commit-tree '
        '"$(git --git-dir="$repo" mktree </dev/null)" -p refs/heads/master -m next) && '
        'git --git-dir="$repo" update-ref refs/heads/master "$next"'
    )
    env = {**GIT_ENV, "PATH": git_shim(tmp_path, before_bundle=moved)}

    manifest = export_ok(site, "backup", env=env)
    copy = site["out"] / "git" / "apps" / "web.git"
    assert (web / "packed-refs").exists()
    assert refs_of(copy) == refs_of(web)
    assert manifest["git"]["repositories"]["apps/web.git"]["refs"] == 4
    _git(copy, "fsck", "--connectivity-only", "--no-progress")


@needs_zstd
def test_backup_survives_a_concurrent_gc(site: dict[str, Any], tmp_path: Path) -> None:
    web = site["root"] / "git" / "apps" / "web.git"
    tree = _git(
        web,
        "mktree",
        stdin="100644 blob "
        + _git(web, "hash-object", "-w", "--stdin", stdin="x")
        + "\tf\n",
    )
    topic = _git(web, "commit-tree", tree, "-p", "refs/heads/stable", "-m", "topic")
    _git(web, "update-ref", "refs/heads/stable", topic)
    # The branch goes, and a gc prunes its objects, just as the copy starts.
    gc = (
        'git --git-dir="$repo" update-ref -d refs/heads/stable && '
        'git --git-dir="$repo" gc -q --prune=now'
    )
    env = {**GIT_ENV, "PATH": git_shim(tmp_path, before_bundle=gc)}

    manifest = export_ok(site, "backup", env=env)
    copy = site["out"] / "git" / "apps" / "web.git"
    assert refs_of(copy) == refs_of(web)
    assert manifest["git"]["repositories"]["apps/web.git"]["refs"] == 2
    _git(copy, "fsck", "--connectivity-only", "--no-progress")


@needs_zstd
def test_backup_never_copies_lock_files(site: dict[str, Any]) -> None:
    web = site["root"] / "git" / "apps" / "web.git"
    for lock in (
        "config.lock",
        "packed-refs.lock",
        "HEAD.lock",
        "refs/heads/master.lock",
        "objects/info/commit-graph.lock",
        "objects/pack/multi-pack-index.lock",
        "info/exclude.lock",
    ):
        (web / lock).parent.mkdir(parents=True, exist_ok=True)
        (web / lock).write_text("stale\n")
    (site["root"] / "git" / "store.lock").write_text("stale\n")

    export_ok(site, "backup")
    assert not list((site["out"] / "git").rglob("*.lock"))
    tar_path = site["out"] / "git.tar"
    subprocess.run(
        ["zstd", "-q", "-d", str(site["out"] / "git.tar.zst"), "-o", str(tar_path)],
        check=True,
    )
    with tarfile.open(tar_path) as tar:
        names = tar.getnames()
    assert "git/apps/web.git/HEAD" in names
    assert not [name for name in names if name.endswith(".lock")]


@pytest.mark.parametrize(
    ("after_fetch", "message"),
    [
        (
            'git --git-dir="$repo" update-ref -d refs/heads/stable',
            "the copy of apps/web.git differs from its bundle's refs",
        ),
        (
            'find "$repo/objects" -type f -delete',
            "connectivity check failed for the copy of apps/web.git",
        ),
    ],
    ids=["refs", "objects"],
)
def test_backup_publishes_nothing_from_a_bad_copy(
    site: dict[str, Any], tmp_path: Path, after_fetch: str, message: str
) -> None:
    env = {"PATH": git_shim(tmp_path, after_fetch=after_fetch)}
    export_fails(site, message, "backup", env=env)
    assert not (site["out"] / "git.tar.zst").exists()
    assert not list(site["out"].glob(".scratch-*"))


def test_backup_refuses_a_copy_missing_a_blob(
    site: dict[str, Any], tmp_path: Path
) -> None:
    web = site["root"] / "git" / "apps" / "web.git"
    blob = _git(web, "hash-object", "-w", "--stdin", stdin="content")
    tree = _git(web, "mktree", stdin=f"100644 blob {blob}\tf\n")
    topic = _git(web, "commit-tree", tree, "-p", "refs/heads/stable", "-m", "topic")
    _git(web, "update-ref", "refs/heads/stable", topic)
    # Unpack the fetched pack into loose objects, then lose the one blob.
    lose_blob = (
        'mv "$repo"/objects/pack/*.pack "$repo/all.pack" && '
        'rm -f "$repo"/objects/pack/* && '
        'git --git-dir="$repo" unpack-objects -q <"$repo/all.pack" && '
        f'rm "$repo/all.pack" "$repo/objects/{blob[:2]}/{blob[2:]}"'
    )
    env = {"PATH": git_shim(tmp_path, after_fetch=lose_blob)}
    export_fails(
        site,
        "connectivity check failed for the copy of apps/web.git",
        "backup",
        env=env,
    )
    assert not (site["out"] / "git.tar.zst").exists()


@needs_zstd
def test_backup_accepts_head_outside_refs_heads(site: dict[str, Any]) -> None:
    """Gerrit points All-Projects' HEAD at refs/meta/config; fsck rejects it."""
    all_projects = site["root"] / "git" / "All-Projects.git"
    _git(all_projects, "symbolic-ref", "HEAD", "refs/meta/config")
    plain = site["root"] / "git" / "plain.git"
    _git(plain, "symbolic-ref", "HEAD", "refs/heads/unborn")

    export_ok(site, "backup")
    out = site["out"] / "git"
    assert _git(out / "All-Projects.git", "symbolic-ref", "HEAD") == "refs/meta/config"
    assert _git(out / "plain.git", "symbolic-ref", "HEAD") == "refs/heads/unborn"
    assert refs_of(out / "All-Projects.git") == refs_of(all_projects)
    assert refs_of(out / "plain.git") == refs_of(plain)


@needs_zstd
def test_backup_keeps_a_detached_head(site: dict[str, Any]) -> None:
    """A detached HEAD whose commit no ref reaches keeps that commit."""
    web = site["root"] / "git" / "apps" / "web.git"
    tree = _git(web, "mktree", stdin="")
    master = _git(web, "rev-parse", "refs/heads/master")
    lone = _git(web, "commit-tree", tree, "-p", master, "-m", "Only from HEAD")
    _git(web, "update-ref", "--no-deref", "HEAD", lone)
    plain = site["root"] / "git" / "plain.git"
    main = _git(plain, "rev-parse", "refs/heads/main")
    _git(plain, "update-ref", "--no-deref", "HEAD", main)

    manifest = export_ok(site, "backup")
    out = site["out"] / "git"
    copy = out / "apps" / "web.git"
    assert _git(copy, "rev-parse", "HEAD") == lone
    assert (
        subprocess.run(
            ["git", "--git-dir", str(copy), "symbolic-ref", "-q", "HEAD"],
            capture_output=True,
            check=False,
        ).returncode
        == 1
    )
    assert _git(copy, "cat-file", "-t", lone) == "commit"
    assert refs_of(copy) == refs_of(web)
    assert not (copy / "FETCH_HEAD").exists()
    _git(copy, "fsck", "--no-progress", "--no-dangling")
    assert _git(out / "plain.git", "rev-parse", "HEAD") == main
    repositories = manifest["git"]["repositories"]
    assert repositories["apps/web.git"]["head"] == lone
    assert repositories["plain.git"]["head"] == main
    assert repositories["All-Projects.git"]["head"] == _git(
        site["root"] / "git" / "All-Projects.git", "symbolic-ref", "HEAD"
    )


def test_backup_refuses_a_detached_head_moved_during_the_copy(
    site: dict[str, Any], tmp_path: Path
) -> None:
    web = site["root"] / "git" / "apps" / "web.git"
    _git(web, "update-ref", "--no-deref", "HEAD", "refs/heads/master")
    moved = 'git --git-dir="$repo" update-ref --no-deref HEAD refs/meta/config'
    env = {"PATH": git_shim(tmp_path, after_bundle=moved)}
    export_fails(
        site, "HEAD of apps/web.git changed during the copy", "backup", env=env
    )
    assert not (site["out"] / "git.tar.zst").exists()


def test_backup_refuses_a_head_changed_during_the_copy(
    site: dict[str, Any], tmp_path: Path
) -> None:
    # Gerrit makes a branch the bundle lacks the default one, just after bundling.
    new_head = (
        'git --git-dir="$repo" update-ref refs/heads/next refs/heads/master && '
        'git --git-dir="$repo" symbolic-ref HEAD refs/heads/next'
    )
    env = {"PATH": git_shim(tmp_path, after_bundle=new_head)}
    export_fails(
        site, "HEAD of apps/web.git changed during the copy", "backup", env=env
    )
    assert not (site["out"] / "git.tar.zst").exists()


def set_sequence(repo: Path, value: str) -> None:
    """Point repo's refs/sequences/changes at a blob holding value, as Gerrit."""
    blob = _git(repo, "hash-object", "-w", "--stdin", stdin=value)
    _git(repo, "update-ref", "refs/sequences/changes", blob)


def add_change(repo: Path, number: int) -> None:
    """Give repo the meta ref of change number, at its first commit."""
    commit = _git(repo, "rev-list", "--max-count=1", "--all")
    _git(repo, "update-ref", f"refs/changes/{number % 100:02d}/{number}/meta", commit)


def sequence_of(repo: Path) -> str:
    return _git(repo, "cat-file", "blob", "refs/sequences/changes")


def test_backup_copies_the_change_sequence_last(site: dict[str, Any]) -> None:
    set_sequence(site["root"] / "git" / "All-Projects.git", "1")
    result = run_on(site, "backup", "--dry-run")
    assert result.returncode == 0, result.stderr
    bundles = [line for line in result.stdout.splitlines() if " bundle " in line]
    assert len(bundles) == len(site["refs"]), result.stdout
    assert "/All-Projects.git " in bundles[-1], bundles


@needs_zstd
def test_backup_keeps_changes_made_during_the_copy_below_the_sequence(
    site: dict[str, Any], tmp_path: Path
) -> None:
    git_dir = site["root"] / "git"
    set_sequence(git_dir / "All-Projects.git", "21")
    add_change(git_dir / "apps" / "web.git", 20)
    # Gerrit reserves 21 to 40 and creates change 21 in a repository not yet copied.
    reserve = (
        f'blob=$(printf 41 | git --git-dir="{git_dir}/All-Projects.git"'
        " hash-object -w --stdin) && "
        f'git --git-dir="{git_dir}/All-Projects.git"'
        ' update-ref refs/sequences/changes "$blob" && '
        f'git --git-dir="{git_dir}/plain.git" update-ref refs/changes/21/21/meta'
        " refs/heads/main"
    )
    env = {"PATH": git_shim(tmp_path, after_bundle=reserve)}
    export_ok(site, "backup", env=env)
    out = site["out"] / "git"
    assert "refs/changes/21/21/meta" in refs_of(out / "plain.git")
    assert sequence_of(out / "All-Projects.git") == "41"


@pytest.mark.parametrize(
    ("sequence", "message"),
    [
        ("7", "refs/sequences/changes of All-Projects.git is 7, not above change 7"),
        ("x", "refs/sequences/changes of All-Projects.git is not a number"),
        (None, "change 7 in apps/web.git has no refs/sequences/changes"),
    ],
    ids=["behind", "garbled", "missing"],
)
def test_backup_refuses_changes_the_sequence_does_not_cover(
    site: dict[str, Any], sequence: str | None, message: str
) -> None:
    git_dir = site["root"] / "git"
    if sequence is not None:
        set_sequence(git_dir / "All-Projects.git", sequence)
    add_change(git_dir / "apps" / "web.git", 7)
    export_fails(site, message, "backup")
    assert not (site["out"] / "git.tar.zst").exists()


@needs_zstd
def test_backup_accepts_a_site_without_changes(site: dict[str, Any]) -> None:
    set_sequence(site["root"] / "git" / "All-Projects.git", "1")
    export_ok(site, "backup")
    assert sequence_of(site["out"] / "git" / "All-Projects.git") == "1"


@pytest.mark.parametrize(
    "mode", ["seed", "final", pytest.param("backup", marks=needs_zstd)]
)
def test_exports_are_private(site: dict[str, Any], mode: str) -> None:
    """Every path an export writes, archives included, is owner-only."""
    web = site["root"] / "git" / "apps" / "web.git"
    (web / "info").mkdir(exist_ok=True)
    (web / "info" / "exclude").write_text("*.tmp\n")
    for path in [site["root"], *map(Path, all_paths(site["root"]))]:
        path.chmod(0o755 if path.is_dir() else 0o644)
    packed = ["--pack"] if mode == "final" and shutil.which("zstd") else []

    saved = os.umask(0o022)
    try:
        manifest = export_ok(site, mode, *packed)
    finally:
        os.umask(saved)
    paths = list(all_paths(site["out"]))
    assert str(site["out"] / "git" / "apps" / "web.git" / "info" / "exclude") in paths
    assert not [p for p in paths if Path(p).name.startswith(".scratch-")]
    assert [p for p in paths if os.lstat(p).st_mode & 0o077] == []
    for name in manifest["archives"]:
        tar_path = site["out"] / "unpacked.tar"
        subprocess.run(
            ["zstd", "-q", "-f", "-d", str(site["out"] / name), "-o", str(tar_path)],
            check=True,
        )
        with tarfile.open(tar_path) as tar:
            assert [m.name for m in tar if m.mode & 0o077] == [], name
        tar_path.unlink()


@needs_zstd
def test_backup_ignores_a_shared_repository_setting(site: dict[str, Any]) -> None:
    """A source's core.sharedRepository is kept but never widens a copy."""
    for rel in ("All-Projects.git", "apps/web.git"):
        _git(site["root"] / "git" / rel, "config", "core.sharedRepository", "group")
    export_ok(site, "backup")
    copy = site["out"] / "git"
    assert [p for p in all_paths(copy) if os.lstat(p).st_mode & 0o077] == []
    for rel in ("All-Projects.git", "apps/web.git"):
        assert _git(copy / rel, "config", "core.sharedRepository") == "group"


@needs_zstd
def test_backup_bundles_privately(site: dict[str, Any], tmp_path: Path) -> None:
    log = tmp_path / "modes.log"
    record = (
        'for a; do case $a in */repository.bundle) stat -c "%a" "$a" "${a%/*}"'
        f" >> {shlex.quote(str(log))} ;; esac; done"
    )
    env = {"PATH": git_shim(tmp_path, after_bundle=record)}
    saved = os.umask(0o022)
    try:
        export_ok(site, "backup", env=env)
    finally:
        os.umask(saved)
    assert log.read_text().split() == ["600", "700"]
    assert not list(site["out"].glob(".scratch-*"))


@pytest.mark.parametrize(
    "mode", ["seed", "final", pytest.param("backup", marks=needs_zstd)]
)
def test_exports_leave_out_lock_files(site: dict[str, Any], mode: str) -> None:
    etc = site["root"] / "etc"
    locks = ["gerrit.config.new.lock", "secure.config.new.lock", "gerrit.config.lock"]
    for name in locks:
        (etc / name).write_text("stale\n")
    (site["root"] / "git" / "plain.git" / "refs" / "heads" / "main.lock").write_text(
        "stale\n"
    )

    export_ok(site, mode)
    assert (site["out"] / "site" / "etc" / "gerrit.config").is_file()
    assert not list((site["out"] / "site").rglob("*.lock"))
    assert not list((site["out"] / "git").rglob("*.lock"))


@pytest.mark.parametrize(
    "mode", ["seed", "final", pytest.param("backup", marks=needs_zstd)]
)
def test_exports_delete_locks_an_earlier_copy_left(
    site: dict[str, Any], mode: str
) -> None:
    export_ok(site, mode)
    # Left by a seed that predates the lock exclusion.
    stale = [
        site["out"] / "git" / "plain.git" / "refs" / "heads" / "main.lock",
        site["out"] / "git" / "plain.git" / "config.lock",
        site["out"] / "site" / "etc" / "gerrit.config.lock",
    ]
    for lock in stale:
        lock.write_text("stale\n")

    export_ok(site, mode)
    assert not list((site["out"] / "site").rglob("*.lock"))
    assert not list((site["out"] / "git").rglob("*.lock"))


@needs_zstd
def test_backup_follows_deleted_refs(site: dict[str, Any]) -> None:
    export_ok(site, "backup")
    web = site["root"] / "git" / "apps" / "web.git"
    _git(web, "pack-refs", "--all")
    _git(web, "update-ref", "-d", "refs/heads/stable")

    manifest = export_ok(site, "backup")
    assert manifest["git"]["repositories"]["apps/web.git"]["refs"] == 2
    copy = site["out"] / "git" / "apps" / "web.git"
    assert "stable" not in _git(copy, "for-each-ref", "--format=%(refname)")


def test_backup_refuses_a_ref_without_its_object(site: dict[str, Any]) -> None:
    broken = site["root"] / "git" / "plain.git" / "refs" / "heads" / "broken"
    broken.write_text("1234567890123456789012345678901234567890\n")

    result = export_fails(site, "cannot read refs of plain.git", "backup")
    assert "missing object" in result.stderr
    assert "refs/heads/broken" in result.stderr
    assert not (site["out"] / "git.tar.zst").exists()
