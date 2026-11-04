# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for the chart's site-import init container script.

``K8S/charts/gerrit/files/scripts/site-import.py`` decides from the
state file, mode, force token and source whether to import, then
fetches, checks and unpacks an export written by
``scripts/gerrit-site-export.py final --pack``. The end-to-end tests
export a small fixture site with the real tool and import it with the
real ``site-init.sh`` in prepare mode; a stub stands in for ``java``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "K8S" / "charts" / "gerrit" / "files" / "scripts" / "site-import.py"
SITE_INIT = SCRIPT.with_name("site-init.sh")
EXPORT = ROOT / "scripts" / "gerrit-site-export.py"
SERVER_ID = "4f9b2b2e-0000-4000-8000-000000000001"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "Fixture",
    "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
    "GIT_COMMITTER_NAME": "Fixture",
    "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
}

needs_tools = pytest.mark.skipif(
    not all(shutil.which(t) for t in ("git", "zstd", "rsync", "ssh-keygen")),
    reason="needs git, zstd, rsync and ssh-keygen",
)


def load() -> ModuleType:
    """Load the hyphenated script as a module."""
    spec = importlib.util.spec_from_file_location("site_import", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["site_import"] = module
    spec.loader.exec_module(module)
    return module


def load_archive() -> ModuleType:
    """The module holding the checks site-import.py runs on an archive."""
    load()
    return sys.modules["site_import_archive"]


def git(repo: Path, *args: str, stdin: str | None = None) -> str:
    return subprocess.run(
        ["git", "--git-dir", str(repo), *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **GIT_ENV},
    ).stdout.strip()


def bare_repo(path: Path, branches: list[str], project_config: str | None) -> None:
    """Create a bare repository with one commit per branch and a config."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "--bare", "-q", str(path)], check=True)
    tree = git(path, "mktree", stdin="")
    commit = git(path, "commit-tree", tree, "-m", "Initial")
    for branch in branches:
        git(path, "update-ref", f"refs/heads/{branch}", commit)
    if project_config is None:
        return
    blob = git(path, "hash-object", "-w", "--stdin", stdin=project_config)
    meta_tree = git(path, "mktree", stdin=f"100644 blob {blob}\tproject.config\n")
    meta = git(path, "commit-tree", meta_tree, "-m", "Project config")
    git(path, "update-ref", "refs/meta/config", meta)


def write_war(path: Path, version: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as war:
        war.writestr(
            "META-INF/MANIFEST.MF",
            f"Manifest-Version: 1.0\r\nImplementation-Version: {version}\r\n",
        )


@pytest.fixture()
def source(tmp_path: Path) -> Path:
    """A Gerrit site to export, as a 3.10 server would leave it."""
    root = tmp_path / "source"
    etc = root / "etc"
    etc.mkdir(parents=True)
    (etc / "gerrit.config").write_text(
        f"[gerrit]\n\tbasePath = git\n\tserverId = {SERVER_ID}\n"
        "[auth]\n\ttype = DEVELOPMENT_BECOME_ANY_ACCOUNT\n\tcookieSecure = true\n"
    )
    (etc / "secure.config").write_text(
        "[auth]\n\tregisterEmailPrivateKey = old-key\n[ldap]\n\tpassword = old-ldap\n"
    )
    subprocess.run(
        [
            "ssh-keygen",
            "-q",
            "-t",
            "ed25519",
            "-N",
            "",
            "-f",
            str(etc / "ssh_host_ed25519_key"),
        ],
        check=True,
    )
    (etc / "samlKeystore.jks").write_bytes(b"source keystore")
    (root / "plugins").mkdir()
    (root / "plugins" / "replication.jar").write_bytes(b"old plugin")
    (root / "index").mkdir()
    (root / "index" / "stale").write_text("stale index\n")
    write_war(root / "bin" / "gerrit.war", "3.10.1")
    bare_repo(
        root / "git" / "All-Projects.git",
        [],
        '[commentlink "bug"]\n\tmatch = x\n\thtml = <b>x</b>\n',
    )
    bare_repo(root / "git" / "All-Users.git", [], "[project]\n")
    bare_repo(root / "git" / "apps" / "web.git", ["master", "stable"], "[project]\n")
    web = root / "git" / "apps" / "web.git"
    blob = git(web, "hash-object", "-w", "--stdin", stdin="submit_rule(ok).\n")
    tree = git(web, "mktree", stdin=f"100644 blob {blob}\trules.pl\n")
    parent = git(web, "rev-parse", "refs/meta/config")
    commit = git(web, "commit-tree", tree, "-p", parent, "-m", "Prolog")
    git(web, "update-ref", "refs/meta/config", commit)
    return root


def export_site(source: Path, out: Path) -> Path:
    """Export a site with the real tool, packed."""
    result = subprocess.run(
        [
            sys.executable,
            str(EXPORT),
            "final",
            "--pack",
            "--i-stopped-writes",
            "--site",
            str(source),
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return out


@pytest.fixture()
def export(source: Path, tmp_path: Path) -> Path:
    """Export the source site with the real tool, packed."""
    return export_site(source, tmp_path / "export")


@pytest.fixture()
def pod(tmp_path: Path) -> dict[str, Any]:
    """The init container's world: an empty volume, the render and the image."""
    site = tmp_path / "volume"
    site.mkdir()
    rendered = tmp_path / "config"
    rendered.mkdir()
    (rendered / "gerrit.config").write_text(
        "[gerrit]\n\tbasePath = git\n[auth]\n\ttype = DEVELOPMENT_BECOME_ANY_ACCOUNT\n"
    )
    for name in ("jgit.config", "notedb.config", "log4j.properties"):
        (rendered / name).write_text(f"# rendered {name}\n")
    image_bin = tmp_path / "image-bin"
    write_war(image_bin / "gerrit.war", "3.13.4")
    (image_bin / "gerrit.sh").write_text("#!/bin/sh\n")
    catalogue = tmp_path / "catalogue"
    (catalogue / "plugins").mkdir(parents=True)
    (catalogue / "plugins" / "healthcheck.jar").write_bytes(b"new plugin")
    log = tmp_path / "java.log"
    java = tmp_path / "java"
    # Records each run's gerrit.war command and the plugins it would load
    java.write_text(
        "#!/bin/sh\n"
        f'echo "$* | $(ls "$GERRIT_SITE/plugins" | tr "\\n" " ")" >> {log}\n'
        'case " $* " in *" init "*) [ -n "$JAVA_FAIL_INIT" ] && exit 3;; esac\n'
        "exit 0\n"
    )
    java.chmod(0o755)
    keystore = tmp_path / "saml" / "samlKeystore.jks"
    keystore.parent.mkdir()
    keystore.write_bytes(b"source keystore")
    env = {
        "GERRIT_SITE": str(site),
        "GERRIT_RENDERED_CONFIG": str(rendered),
        "GERRIT_IMAGE_BIN": str(image_bin),
        "GERRIT_CATALOGUE": str(catalogue),
        "GERRIT_PLUGINS": "healthcheck",
        "GERRIT_LIBS": "",
        "GERRIT_SITE_DIRS": "cache db etc git index",
        "SITE_IMPORT_SCRATCH": str(tmp_path / "scratch"),
        "SITE_INIT_SCRIPT": str(SITE_INIT),
        "SITE_IMPORT_JAVA": str(java),
        "SITE_IMPORT_SECURE_KEYS": "auth.registerEmailPrivateKey sendemail.smtpPass",
        "SITE_IMPORT_SAML_KEYSTORE": str(keystore),
    }
    return {"site": site, "env": env, "log": log, "keystore": keystore, "tmp": tmp_path}


def import_env(pod: dict[str, Any], export: Path, **extra: str) -> dict[str, str]:
    return {
        **pod["env"],
        "SITE_IMPORT_SOURCE_TYPE": "pvc",
        "SITE_IMPORT_SOURCE_URL": str(export),
        **extra,
    }


def run_main(
    env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> tuple[ModuleType, int]:
    module = load()
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return module, module.main(env)


def state(site: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((site / ".chart" / "site-state.json").read_text())
    return data


def java_runs(pod: dict[str, Any]) -> list[str]:
    log: Path = pod["log"]
    return log.read_text().splitlines() if log.exists() else []


# --- decision -------------------------------------------------------------


def complete(token: str | None = None) -> dict[str, Any]:
    return {"state": "complete", "forceToken": token}


@pytest.mark.parametrize(
    ("current", "mode", "token", "source", "holds", "expected"),
    [
        (None, "auto", "", "none", False, "fresh"),
        (None, "auto", "", "pvc", False, "import"),
        (complete(), "auto", "", "pvc", True, "nothing"),
        (complete(), "auto", "", "none", True, "nothing"),
        ({"state": "failed"}, "auto", "", "pvc", True, "import"),
        ({"state": "in-progress"}, "auto", "", "https", True, "import"),
        (complete(), "force", "1", "pvc", True, "import"),
        (complete("1"), "force", "1", "pvc", True, "nothing"),
        (complete("1"), "force", "2", "pvc", True, "import"),
        ({"state": "failed", "forceToken": "2"}, "force", "2", "pvc", True, "import"),
        (None, "force", "1", "pvc", True, "import"),
        (None, "skip", "", "pvc", True, "nothing"),
        ({"state": "failed"}, "skip", "", "none", True, "nothing"),
    ],
)
def test_decide(
    current: dict[str, Any] | None,
    mode: str,
    token: str,
    source: str,
    holds: bool,
    expected: str,
) -> None:
    assert load().decide(current, mode, token, source, holds) == expected


@pytest.mark.parametrize(
    ("current", "mode", "token", "source", "holds", "message"),
    [
        (None, "auto", "", "none", True, "no state file accounts for"),
        (None, "auto", "", "pvc", True, "no state file accounts for"),
        ({"state": "failed"}, "auto", "", "none", True, "left state failed"),
        ({"state": "in-progress"}, "auto", "", "none", True, "left state in-progress"),
        (complete(), "force", "", "pvc", True, "needs a siteImport.forceToken"),
        (complete(), "force", "1", "none", True, "needs a siteImport.source"),
    ],
)
def test_decide_refuses(
    current: dict[str, Any] | None,
    mode: str,
    token: str,
    source: str,
    holds: bool,
    message: str,
) -> None:
    module = load()
    with pytest.raises(module.SiteImportError, match=message):
        module.decide(current, mode, token, source, holds)


@pytest.mark.parametrize(
    ("mode", "source", "chart_site", "expected"),
    [
        ("auto", "none", True, "adopt"),
        ("skip", "none", True, "nothing"),
        ("skip", "pvc", False, "nothing"),
    ],
)
def test_decide_adopts_a_chart_site(
    mode: str, source: str, chart_site: bool, expected: str
) -> None:
    assert load().decide(None, mode, "", source, True, chart_site) == expected


@pytest.mark.parametrize(
    ("current", "mode", "token", "source"),
    [
        (None, "auto", "", "pvc"),
        (None, "auto", "", "https"),
        ({"state": "failed"}, "auto", "", "none"),
    ],
)
def test_decide_adopts_only_with_auto_none_and_no_state(
    current: dict[str, Any] | None, mode: str, token: str, source: str
) -> None:
    module = load()
    with pytest.raises(module.SiteImportError):
        module.decide(current, mode, token, source, True, True)


def test_occupied_ignores_state_and_lost_found(tmp_path: Path) -> None:
    module = load()
    (tmp_path / ".chart").mkdir()
    (tmp_path / "lost+found").mkdir()
    assert not module.occupied(tmp_path)
    (tmp_path / "etc").mkdir()
    assert module.occupied(tmp_path)


# --- state file -----------------------------------------------------------


def test_write_state_replaces_atomically(tmp_path: Path) -> None:
    module = load()
    module.write_state(tmp_path, {"state": "in-progress", "source": "pvc:/x"})
    module.write_state(tmp_path, {"state": "complete", "source": "pvc:/x"})
    assert sorted(p.name for p in (tmp_path / ".chart").iterdir()) == [
        "site-state.json"
    ]
    data = module.read_state(tmp_path)
    assert data["schema"] == 1
    assert data["state"] == "complete"
    assert data["updated"].endswith("Z")


@pytest.mark.parametrize("text", ["{", '{"state": "done"}', "[]"])
def test_unreadable_state_is_refused(tmp_path: Path, text: str) -> None:
    module = load()
    (tmp_path / ".chart").mkdir()
    (tmp_path / ".chart" / "site-state.json").write_text(text)
    with pytest.raises(module.SiteImportError, match="repair or remove it"):
        module.read_state(tmp_path)


@pytest.mark.parametrize(
    ("value", "threads"),
    [("", None), ("2", 2), ("1500m", 2), ("0.5", 1), ("250m", 1), (" 4 ", 4)],
)
def test_cpu_limit_sets_reindex_threads(
    pod: dict[str, Any], value: str, threads: int | None
) -> None:
    env = {**pod["env"], "SITE_IMPORT_CPU_LIMIT": value}
    assert load().Settings.from_env(env).reindex_threads == threads


@pytest.mark.parametrize("value", ["two", "0", "-1", "m", "nan"])
def test_bad_cpu_limit_is_refused(pod: dict[str, Any], value: str) -> None:
    module = load()
    env = {**pod["env"], "SITE_IMPORT_CPU_LIMIT": value}
    with pytest.raises(module.SiteImportError, match="not a CPU limit"):
        module.Settings.from_env(env)


def test_settings_refuse_bad_values(pod: dict[str, Any]) -> None:
    module = load()
    for extra, message in [
        ({"SITE_IMPORT_MODE": "always"}, "siteImport.mode"),
        ({"SITE_IMPORT_SOURCE_TYPE": "s3"}, "siteImport.source.type"),
        ({"SITE_IMPORT_SOURCE_TYPE": "pvc"}, "needs a url"),
        ({"SITE_IMPORT_REINDEX": "maybe"}, "not a boolean"),
    ]:
        with pytest.raises(module.SiteImportError, match=message):
            module.Settings.from_env({**pod["env"], **extra})


# --- manifest and archives ------------------------------------------------


def manifest_with(path: Path, **changes: Any) -> Path:
    data = {
        "schema": 1,
        "mode": "final",
        "archives": {"git.tar.zst": "0" * 64, "site.tar.zst": "0" * 64},
        "git": {"repositories": {}},
        "ssh_host_keys": {},
        **changes,
    }
    path.write_text(json.dumps(data))
    return path


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"mode": "seed"}, "'seed' export"),
        ({"schema": 2}, "not a schema 1 manifest"),
        ({"archives": {}}, "export with --pack"),
        (
            {"archives": {"git.tar.zst.age": "0", "site.tar.zst.age": "0"}},
            "encrypted archives are not supported yet",
        ),
        ({"ssh_host_keys": None}, "lacks git.repositories or ssh_host_keys"),
    ],
)
def test_manifest_is_refused(
    tmp_path: Path, changes: dict[str, Any], message: str
) -> None:
    module = load()
    with pytest.raises(module.SiteImportError, match=message):
        module.load_manifest(manifest_with(tmp_path / "m.json", **changes), "")


def test_manifest_pin(tmp_path: Path) -> None:
    module = load()
    path = manifest_with(tmp_path / "m.json")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert module.load_manifest(path, digest)[1] == digest
    with pytest.raises(module.SiteImportError, match="not the expected"):
        module.load_manifest(path, "f" * 64)


def tar_zst(path: Path, members: dict[str, bytes]) -> Path:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    subprocess.run(["zstd", "-q", "-o", str(path)], input=buffer.getvalue(), check=True)
    return path


def link(name: str, target: str, hard: bool = False) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = tarfile.LNKTYPE if hard else tarfile.SYMTYPE
    info.linkname = target
    return info


def tar_zst_members(path: Path, members: list[tuple[tarfile.TarInfo, bytes]]) -> Path:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for info, data in members:
            if info.isfile():
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            else:
                tar.addfile(info)
    subprocess.run(["zstd", "-q", "-o", str(path)], input=buffer.getvalue(), check=True)
    return path


def regular(name: str, data: bytes = b"x") -> tuple[tarfile.TarInfo, bytes]:
    return tarfile.TarInfo(name), data


def directory(name: str) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    info.mode = 0o755
    return info, b""


@needs_tools
@pytest.mark.parametrize(
    ("members", "message"),
    [
        (
            [
                directory("site/etc"),
                (link("site/etc/jgit.config", "../.chart/site-state.json"), b""),
            ],
            "symlink outside site/ or into a skipped path",
        ),
        ([(link("site/passwd", "/etc/passwd"), b"")], "symlink to an absolute path"),
        ([(link("site/up", "../../escape"), b"")], "symlink outside site/"),
        ([(link("site/repos", "../git"), b"")], "symlink outside site/"),
        (
            # A directory link, then a member written through it
            [
                directory("site/etc"),
                (link("site/etc/up", ".."), b""),
                regular("site/etc/up/.chart/site-state.json", b"{}"),
            ],
            "lies outside site/ or in a skipped path",
        ),
        (
            # Harmless when checked, retargeted by a later directory link
            [
                directory("site/etc"),
                (link("site/etc/state", "up/.chart/site-state.json"), b""),
                (link("site/etc/up", ".."), b""),
            ],
            "symlinks resolve outside their tree or into a skipped path: "
            "site/etc/state",
        ),
        (
            [
                regular("git/x.git/config"),
                (link("site/x", "git/x.git/config", True), b""),
            ],
            "hard link outside site/",
        ),
        (
            [(link("site/state", "site/.chart/site-state.json", True), b"")],
            "hard link outside site/ or into a skipped path",
        ),
    ],
)
def test_unpack_refuses_links_leaving_their_tree(
    tmp_path: Path, members: list[tuple[tarfile.TarInfo, bytes]], message: str
) -> None:
    module = load()
    archive = tar_zst_members(tmp_path / "site.tar.zst", members)
    with pytest.raises(module.SiteImportError, match=message):
        module.unpack(archive, tmp_path / "out")
    assert not (tmp_path / "out" / "site" / ".chart").exists()


@needs_tools
def test_unpack_keeps_links_inside_their_tree(tmp_path: Path) -> None:
    module = load()
    archive = tar_zst_members(
        tmp_path / "site.tar.zst",
        [
            directory("site/etc"),
            regular("site/etc/gerrit.config", b"[gerrit]\n"),
            (link("site/etc/linked.config", "gerrit.config"), b""),
            (link("site/etc/copy.config", "site/etc/gerrit.config", True), b""),
        ],
    )
    out = tmp_path / "out"
    module.unpack(archive, out)
    assert (out / "site/etc/linked.config").read_text() == "[gerrit]\n"
    assert (out / "site/etc/copy.config").read_text() == "[gerrit]\n"


@needs_tools
def test_unpack_skips_state_index_and_cache(tmp_path: Path) -> None:
    module = load()
    archive = tar_zst(
        tmp_path / "site.tar.zst",
        {
            "site/etc/gerrit.config": b"[gerrit]\n",
            "site/.chart/site-state.json": b"{}",
            "site/index/x": b"x",
            "site/cache/y": b"y",
        },
    )
    module.unpack(archive, tmp_path / "out")
    found = sorted(
        str(p.relative_to(tmp_path / "out")) for p in (tmp_path / "out").rglob("*")
    )
    assert found == ["site", "site/etc", "site/etc/gerrit.config"]


@needs_tools
@pytest.mark.parametrize("name", ["../escape", "site/../../escape", "other/file"])
def test_unpack_refuses_members_outside(tmp_path: Path, name: str) -> None:
    module = load()
    archive = tar_zst(tmp_path / "site.tar.zst", {name: b"x"})
    with pytest.raises(module.SiteImportError, match="outside site/ and git/"):
        module.unpack(archive, tmp_path / "out")
    assert not (tmp_path / "escape").exists()


@needs_tools
def test_unpack_stops_decompressing_a_refused_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused member ends decompression; the rest is never buffered."""
    module = load()
    drained: list[int] = []

    class Spy(subprocess.Popen):
        def communicate(self, *args: Any, **kwargs: Any) -> Any:
            out, err = super().communicate(*args, **kwargs)
            drained.append(len(out or b""))
            return out, err

    rest = 16 * 1024 * 1024
    archive = tar_zst(
        tmp_path / "site.tar.zst",
        {"other/file": b"x", "site/big": bytes(rest)},
    )
    monkeypatch.setattr(subprocess, "Popen", Spy)
    with pytest.raises(module.SiteImportError, match="outside site/ and git/"):
        module.unpack(archive, tmp_path / "out")
    assert drained
    assert drained[0] < rest // 4


G2P_HOOK_LINKS = {
    "patchset-created": "/opt/gerrit-tools/bin/patchset-created",
    "change-merged": "/opt/gerrit-tools/bin/change-merged",
}
G2P_REPLICATION = (
    "site/.config/gerrit_to_platform/replication.config",
    "/var/gerrit/etc/replication.config",
)


def g2p_site(hooks_path: str = "/var/gerrit/hooks", hooks_dir: str = "hooks") -> list:
    """Members of a chart site with gerrit-to-platform hooks enabled."""
    return [
        directory("site/etc"),
        regular("site/etc/gerrit.config", f"[hooks]\n\tpath = {hooks_path}\n".encode()),
        directory(f"site/{hooks_dir}"),
        *[
            (link(f"site/{hooks_dir}/{name}", target), b"")
            for name, target in G2P_HOOK_LINKS.items()
        ],
        directory("site/.config/gerrit_to_platform"),
        (link(*G2P_REPLICATION), b""),
    ]


def unpack_and_skip(tmp_path: Path, members: list) -> Path:
    module = load()
    archive = tar_zst_members(tmp_path / "site.tar.zst", members)
    out = tmp_path / "out"
    absolute: list[tuple[str, str]] = []
    module.unpack(archive, out, absolute)
    module.skip_regenerated(
        absolute, out / "site/etc/gerrit.config", Path("/opt/gerrit-tools/bin")
    )
    return out


@needs_tools
@pytest.mark.parametrize(
    ("hooks_path", "hooks_dir"),
    [("/var/gerrit/hooks", "hooks"), ("custom/hooks", "custom/hooks"), ("", "hooks")],
)
def test_unpack_leaves_out_links_site_init_recreates(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    hooks_path: str,
    hooks_dir: str,
) -> None:
    members = g2p_site(hooks_path, hooks_dir)
    if not hooks_path:
        members[1] = regular("site/etc/gerrit.config", b"[gerrit]\n")
    out = unpack_and_skip(tmp_path, members)
    assert (out / "site" / hooks_dir).is_dir()
    assert not any((out / "site" / hooks_dir).iterdir())
    assert not os.path.lexists(out / G2P_REPLICATION[0])
    logged = capsys.readouterr().out
    for name, target in G2P_HOOK_LINKS.items():
        assert f"skipping site/{hooks_dir}/{name} -> {target}" in logged
    assert f"skipping {G2P_REPLICATION[0]} -> {G2P_REPLICATION[1]}" in logged


@needs_tools
@pytest.mark.parametrize(
    ("member", "hooks_path"),
    [
        (link("site/hooks/patchset-created", "/usr/bin/patchset-created"), None),
        (link("site/hooks/patchset-created", "/opt/gerrit-tools/bin/sh"), None),
        (link("site/hooks/patchset-created", "/opt/gerrit-tools/bin/../x"), None),
        (
            link("site/bin/patchset-created", "/opt/gerrit-tools/bin/patchset-created"),
            None,
        ),
        (
            link(
                "site/hooks/patchset-created", "/opt/gerrit-tools/bin/patchset-created"
            ),
            "custom",
        ),
        (link(G2P_REPLICATION[0], "/etc/passwd"), None),
        (link("site/.config/gerrit_to_platform/other", G2P_REPLICATION[1]), None),
    ],
)
def test_unpack_refuses_other_absolute_links(
    tmp_path: Path, member: tarfile.TarInfo, hooks_path: str | None
) -> None:
    module = load()
    members = g2p_site(hooks_path or "/var/gerrit/hooks", hooks_path or "hooks")
    members.append((member, b""))
    with pytest.raises(module.SiteImportError, match="symlinks to absolute paths"):
        unpack_and_skip(tmp_path, members)


def test_keystore_must_match_the_secret(tmp_path: Path) -> None:
    module = load_archive()
    imported, mounted = tmp_path / "a.jks", tmp_path / "b.jks"
    imported.write_bytes(b"one")
    mounted.write_bytes(b"one")
    module.check_keystore(imported, mounted)
    module.check_keystore(imported, None)
    mounted.write_bytes(b"two")
    with pytest.raises(module.SiteImportError, match="differs from the SAML Secret"):
        module.check_keystore(imported, mounted)
    module.check_keystore(None, mounted)


def test_system_repositories_must_match_the_render(tmp_path: Path) -> None:
    module = load_archive()
    imported, rendered = tmp_path / "imported.config", tmp_path / "rendered.config"
    rendered.write_text("[gerrit]\n\tbasePath = git\n")
    imported.write_text("[gerrit]\n\tallProjects = All-Projects\n")
    module.check_system_repositories(imported, rendered)
    imported.write_text("[gerrit]\n\tallProjects = Root\n\tallUsers = Users\n")
    with pytest.raises(module.SiteImportError) as raised:
        module.check_system_repositories(imported, rendered)
    message = str(raised.value)
    assert "gerrit.allProjects is Root, the chart's All-Projects" in message
    assert "gerrit.allUsers is Users, the chart's All-Users" in message
    assert message.endswith("set the archive's names in gerrit.extraConfig")
    rendered.write_text("[gerrit]\n\tallProjects = Root\n\tallUsers = Users\n")
    module.check_system_repositories(imported, rendered)


def keystore_site(tmp_path: Path, keystore_path: str | None) -> Path:
    site = tmp_path / "site"
    (site / "etc").mkdir(parents=True)
    saml = f"[saml]\n\tkeystorePath = {keystore_path}\n" if keystore_path else ""
    (site / "etc" / "gerrit.config").write_text("[gerrit]\n\tbasePath = git\n" + saml)
    return site


@pytest.mark.parametrize(
    ("keystore_path", "file", "expected"),
    [
        (None, None, None),
        (None, "etc/samlKeystore.jks", "etc/samlKeystore.jks"),
        ("etc/samlKeystore.jks", None, None),
        (
            "etc/source-identity.jks",
            "etc/source-identity.jks",
            "etc/source-identity.jks",
        ),
        ("/srv/gerrit/data/id.jks", "data/id.jks", "data/id.jks"),
        ("/var/gerrit/etc/id.jks", "etc/id.jks", "etc/id.jks"),
    ],
)
def test_imported_keystore_follows_the_configured_path(
    tmp_path: Path, keystore_path: str | None, file: str | None, expected: str | None
) -> None:
    module = load_archive()
    site = keystore_site(tmp_path, keystore_path)
    if file:
        (site / file).parent.mkdir(parents=True, exist_ok=True)
        (site / file).write_bytes(b"keystore")
    found = module.imported_keystore(site, "/srv/gerrit")
    assert found == (site / expected if expected else None)


@pytest.mark.parametrize(
    ("keystore_path", "message"),
    [
        ("etc/source-identity.jks", "names a keystore the archive does not carry"),
        ("/etc/ssl/id.jks", "lies outside its site"),
        ("../outside.jks", "leads outside its site"),
    ],
)
def test_imported_keystore_refuses_what_it_cannot_compare(
    tmp_path: Path, keystore_path: str, message: str
) -> None:
    module = load_archive()
    site = keystore_site(tmp_path, keystore_path)
    (tmp_path / "outside.jks").write_bytes(b"keystore")
    with pytest.raises(module.SiteImportError, match=message):
        module.imported_keystore(site, "/srv/gerrit")


def test_secure_config_report_names_dropped_entries() -> None:
    module = load()
    warnings = module.secure_config_report(
        ["auth.registeremailprivatekey", "ldap.password"],
        ["auth.registerEmailPrivateKey", "sendemail.smtpPass"],
    )
    assert warnings[0].endswith("dropped at start: ldap.password")
    assert "email verification links" in warnings[1]
    assert (
        module.secure_config_report(["sendemail.smtppass"], ["sendemail.smtpPass"])
        == []
    )


def test_removed_keys_report() -> None:
    module = load()
    assert module.removed_keys_report(["auth.cookiesecure", "auth.type"]) == [
        "gerrit.config sets auth.cookiesecure, which Gerrit 3.13 removed"
    ]


def test_gerrit_version_reads_the_release_war(tmp_path: Path) -> None:
    module = load()
    nested = io.BytesIO()
    with zipfile.ZipFile(nested, "w") as jar:
        jar.writestr("com/google/gerrit/common/Version", "v3.13.10\n")
    war = tmp_path / "gerrit.war"
    with zipfile.ZipFile(war, "w") as archive:
        archive.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\r\n")
        archive.writestr(
            "WEB-INF/lib/com_google_gerrit_common_libversion.jar", nested.getvalue()
        )
    assert module.gerrit_version(war) == "3.13.10"
    write_war(war, "3.12.1")
    assert module.gerrit_version(war) == "3.12.1"
    war.write_bytes(b"not a war")
    assert module.gerrit_version(war) is None


# --- end to end -----------------------------------------------------------


@needs_tools
def test_fresh_site_is_recorded_once(
    pod: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, code = run_main(pod["env"], monkeypatch)
    assert code == 0
    recorded = state(pod["site"])
    assert recorded["state"] == "complete"
    assert recorded["source"] == "none"
    assert recorded["manifestSha256"] is None
    assert recorded["gerritVersion"] == "3.13.4"
    assert java_runs(pod) == []
    (pod["site"] / "git").mkdir()
    _, code = run_main(pod["env"], monkeypatch)
    assert code == 0
    assert state(pod["site"])["updated"] == recorded["updated"]


@needs_tools
def test_occupied_volume_without_state_is_refused(
    pod: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (pod["site"] / "etc").mkdir()
    _, code = run_main(pod["env"], monkeypatch)
    assert code == 1
    assert "no state file accounts for" in capsys.readouterr().err
    assert not (pod["site"] / ".chart").exists()


def chart_site(site: Path, all_projects: str = "") -> None:
    """Lay out a site as chart 0.6 left it: config and All-Projects."""
    (site / "etc").mkdir()
    named = f"\tallProjects = {all_projects}\n" if all_projects else ""
    (site / "etc" / "gerrit.config").write_text(f"[gerrit]\n{named}")
    (site / "git" / f"{all_projects or 'All-Projects'}.git").mkdir(parents=True)


@needs_tools
@pytest.mark.parametrize("all_projects", ["", "Root-Project"])
def test_chart_site_without_state_is_adopted(
    pod: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    all_projects: str,
) -> None:
    site: Path = pod["site"]
    chart_site(site, all_projects)
    _, code = run_main(pod["env"], monkeypatch)
    assert code == 0
    assert (
        "adopted the site an earlier chart version created" in capsys.readouterr().out
    )
    recorded = state(site)
    assert recorded["state"] == "complete"
    assert recorded["adopted"] is True
    assert recorded["manifestSha256"] is None
    assert recorded["source"] == "none"
    assert java_runs(pod) == []
    assert (site / "etc" / "gerrit.config").is_file()
    # The next start finds the state complete
    _, code = run_main(pod["env"], monkeypatch)
    assert code == 0
    assert state(site)["updated"] == recorded["updated"]


@needs_tools
@pytest.mark.parametrize(
    "layout",
    ["config only", "repository only", "other All-Projects", "pvc source"],
)
def test_other_unaccounted_content_is_still_refused(
    pod: dict[str, Any],
    export: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    layout: str,
) -> None:
    site: Path = pod["site"]
    env = pod["env"]
    if layout == "config only":
        (site / "etc").mkdir()
        (site / "etc" / "gerrit.config").write_text("[gerrit]\n")
    elif layout == "repository only":
        (site / "git" / "All-Projects.git").mkdir(parents=True)
    elif layout == "other All-Projects":
        chart_site(site)
        (site / "etc" / "gerrit.config").write_text(
            "[gerrit]\n\tallProjects = Root-Project\n"
        )
    else:
        chart_site(site)
        env = import_env(pod, export)
    _, code = run_main(env, monkeypatch)
    assert code == 1
    assert "no state file accounts for" in capsys.readouterr().err
    assert not (site / ".chart").exists()


@needs_tools
def test_init_and_reindex_get_the_heap_and_threads(
    pod: dict[str, Any], export: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = import_env(
        pod,
        export,
        SITE_IMPORT_JAVA_HEAP="-XX:MaxRAMPercentage=70.0",
        SITE_IMPORT_CPU_LIMIT="1500m",
    )
    assert run_main(env, monkeypatch)[1] == 0
    init, reindex = java_runs(pod)
    for run in (init, reindex):
        assert "-XX:MaxRAMPercentage=70.0 -jar " in run
    assert "--threads" not in init
    assert " reindex --threads 2 -d " in reindex


@needs_tools
def test_reindex_keeps_gerrits_default_threads_without_a_cpu_limit(
    pod: dict[str, Any], export: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert run_main(import_env(pod, export), monkeypatch)[1] == 0
    assert all("--threads" not in run for run in java_runs(pod))
    assert all("MaxRAMPercentage" not in run for run in java_runs(pod))


@needs_tools
def test_import_adopts_the_export(
    pod: dict[str, Any],
    export: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    site: Path = pod["site"]
    _, code = run_main(import_env(pod, export), monkeypatch)
    out = capsys.readouterr()
    assert code == 0, out.err

    recorded = state(site)
    digest = hashlib.sha256((export / "manifest.json").read_bytes()).hexdigest()
    assert recorded["state"] == "complete"
    assert recorded["manifestSha256"] == digest
    assert recorded["source"] == f"pvc:{export}"
    assert recorded["gerritVersion"] == "3.13.4"

    for repo in ("All-Projects.git", "All-Users.git", "apps/web.git"):
        assert (site / "git" / repo / "HEAD").is_file()
    # The chart's plugins and configuration replace the source's
    assert sorted(p.name for p in (site / "plugins").iterdir()) == ["healthcheck.jar"]
    config = (site / "etc" / "gerrit.config").read_text()
    assert f"serverId = {SERVER_ID}" in config
    assert "cookieSecure" not in config
    assert (site / "etc" / "jgit.config").read_text() == "# rendered jgit.config\n"
    assert not (site / "index" / "stale").exists()
    assert (site / "etc" / "ssh_host_ed25519_key").is_file()

    runs = java_runs(pod)
    assert [r.split(" -jar ")[1].split()[1] for r in runs] == ["init", "reindex"]
    assert all(r.endswith("| healthcheck.jar ") for r in runs)
    assert "--batch --no-auto-start" in runs[0]

    warnings = recorded["warnings"]
    assert any("rejectImplicitMerges" in w for w in warnings)
    assert any("commentlink html" in w for w in warnings)
    assert any("rules.pl" in w and "apps/web.git" in w for w in warnings)
    assert any("auth.cookiesecure" in w for w in warnings)
    assert any("ldap.password" in w for w in warnings)
    # Names only: no secret value reaches the log or the state
    assert "old-ldap" not in out.out + out.err
    assert "old-ldap" not in json.dumps(recorded)

    # A restart of the pod finds the import complete and does nothing
    _, code = run_main(import_env(pod, export), monkeypatch)
    assert code == 0
    assert len(java_runs(pod)) == 2


def add_g2p_links(source: Path, target_dir: str = "/opt/gerrit-tools/bin") -> None:
    """Give the source site the links a chart site with g2p hooks holds."""
    with open(source / "etc" / "gerrit.config", "a", encoding="utf-8") as config:
        config.write("[hooks]\n\tpath = /var/gerrit/hooks\n")
    (source / "hooks").mkdir()
    for name in G2P_HOOK_LINKS:
        (source / "hooks" / name).symlink_to(f"{target_dir}/{name}")
    g2p = source / ".config" / "gerrit_to_platform"
    g2p.mkdir(parents=True)
    (g2p / "replication.config").symlink_to(
        "/var/gerrit/etc/replication.config"
        if target_dir == "/opt/gerrit-tools/bin"
        else "/etc/passwd"
    )


@needs_tools
def test_import_of_a_g2p_chart_site_leaves_out_its_links(
    pod: dict[str, Any],
    source: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    add_g2p_links(source)
    out = export_site(source, tmp_path / "export")
    _, code = run_main(import_env(pod, out), monkeypatch)
    logged = capsys.readouterr()
    assert code == 0, logged.err
    site: Path = pod["site"]
    assert state(site)["state"] == "complete"
    assert "skipping site/hooks/patchset-created -> " in logged.out
    assert not any(p.is_symlink() for p in site.rglob("*"))


@needs_tools
def test_import_refuses_the_same_names_linking_elsewhere(
    pod: dict[str, Any],
    source: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    add_g2p_links(source, "/usr/local/bin")
    out = export_site(source, tmp_path / "export")
    _, code = run_main(import_env(pod, out), monkeypatch)
    assert code != 0
    assert "symlinks to absolute paths" in capsys.readouterr().err
    site: Path = pod["site"]
    assert state(site)["state"] == "failed"
    assert not (site / "etc").exists()


@needs_tools
def test_corrupt_archive_fails_closed_then_retries(
    pod: dict[str, Any],
    export: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    site: Path = pod["site"]
    good = (export / "git.tar.zst").read_bytes()
    (export / "git.tar.zst").write_bytes(good[:-8] + b"corrupt!")
    _, code = run_main(import_env(pod, export), monkeypatch)
    assert code == 1
    assert "git.tar.zst has sha256" in capsys.readouterr().err
    recorded = state(site)
    assert recorded["state"] == "failed"
    assert "git.tar.zst has sha256" in recorded["reason"]
    assert sorted(p.name for p in site.iterdir()) == [".chart"]
    assert java_runs(pod) == []

    # Dropping the source cannot start Gerrit on the failed site
    _, code = run_main(pod["env"], monkeypatch)
    assert code == 1
    assert "left state failed" in capsys.readouterr().err

    (export / "git.tar.zst").write_bytes(good)
    _, code = run_main(import_env(pod, export), monkeypatch)
    assert code == 0
    assert state(site)["state"] == "complete"


@needs_tools
def test_failed_init_records_failed(
    pod: dict[str, Any], export: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, code = run_main(import_env(pod, export, JAVA_FAIL_INIT="1"), monkeypatch)
    assert code == 1
    recorded = state(pod["site"])
    assert recorded["state"] == "failed"
    assert recorded["reason"] == "gerrit.war init failed with exit status 3"


@needs_tools
@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"SITE_IMPORT_EXPECTED_MANIFEST_SHA256": "0" * 64}, "not the expected"),
    ],
)
def test_pin_mismatch_fails(
    pod: dict[str, Any],
    export: Path,
    monkeypatch: pytest.MonkeyPatch,
    extra: dict[str, str],
    message: str,
) -> None:
    _, code = run_main(import_env(pod, export, **extra), monkeypatch)
    assert code == 1
    assert message in state(pod["site"])["reason"]


@needs_tools
def test_server_id_mismatch_fails_before_the_site_changes(
    pod: dict[str, Any], export: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rendered = Path(pod["env"]["GERRIT_RENDERED_CONFIG"]) / "gerrit.config"
    rendered.write_text("[gerrit]\n\tbasePath = git\n\tserverId = other\n")
    _, code = run_main(import_env(pod, export), monkeypatch)
    assert code == 1
    assert "does not match the archive's" in state(pod["site"])["reason"]
    assert sorted(p.name for p in pod["site"].iterdir()) == [".chart"]


@needs_tools
def test_keystore_mismatch_fails(
    pod: dict[str, Any], export: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pod["keystore"].write_bytes(b"another keystore")
    _, code = run_main(import_env(pod, export), monkeypatch)
    assert code == 1
    assert "differs from the SAML Secret" in state(pod["site"])["reason"]


@needs_tools
def test_configured_keystore_is_compared(
    pod: dict[str, Any],
    source: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (source / "etc" / "samlKeystore.jks").unlink()
    (source / "etc" / "source-identity.jks").write_bytes(b"source identity")
    with open(source / "etc" / "gerrit.config", "a") as config:
        config.write("[saml]\n\tkeystorePath = etc/source-identity.jks\n")
    export = export_site(source, tmp_path / "saml-export")
    _, code = run_main(import_env(pod, export), monkeypatch)
    assert code == 1
    assert "differs from the SAML Secret" in state(pod["site"])["reason"]
    assert sorted(p.name for p in pod["site"].iterdir()) == [".chart"]
    pod["keystore"].write_bytes(b"source identity")
    assert run_main(import_env(pod, export), monkeypatch)[1] == 0


@needs_tools
def test_system_repository_names_must_match_before_the_site_changes(
    pod: dict[str, Any],
    source: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git_dir = source / "git"
    (git_dir / "All-Users.git").rename(git_dir / "Users.git")
    with open(source / "etc" / "gerrit.config", "a") as config:
        config.write("[gerrit]\n\tallUsers = Users\n")
    export = export_site(source, tmp_path / "named-export")
    _, code = run_main(import_env(pod, export), monkeypatch)
    assert code == 1
    reason = state(pod["site"])["reason"]
    assert "the archive's gerrit.allUsers is Users, the chart's All-Users" in reason
    assert sorted(p.name for p in pod["site"].iterdir()) == [".chart"]
    rendered = Path(pod["env"]["GERRIT_RENDERED_CONFIG"]) / "gerrit.config"
    with open(rendered, "a") as config:
        config.write("[gerrit]\n\tallUsers = Users\n")
    assert run_main(import_env(pod, export), monkeypatch)[1] == 0


@needs_tools
def test_tampered_manifest_fails_verification(
    pod: dict[str, Any], export: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = json.loads((export / "manifest.json").read_text())
    manifest["git"]["repositories"]["gone.git"] = {"refs": 1, "meta_config": None}
    (export / "manifest.json").write_text(json.dumps(manifest))
    _, code = run_main(import_env(pod, export), monkeypatch)
    assert code == 1
    assert "missing repositories: gone.git" in state(pod["site"])["reason"]
    _, code = run_main(import_env(pod, export, SITE_IMPORT_VERIFY="false"), monkeypatch)
    assert code == 0


@needs_tools
def test_force_reimports_once_per_token(
    pod: dict[str, Any], export: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site: Path = pod["site"]
    assert run_main(import_env(pod, export), monkeypatch)[1] == 0
    (site / "etc" / "local-change").write_text("made after the import\n")
    forced = import_env(
        pod, export, SITE_IMPORT_MODE="force", SITE_IMPORT_FORCE_TOKEN="1"
    )
    assert run_main(forced, monkeypatch)[1] == 0
    assert not (site / "etc" / "local-change").exists()
    assert state(site)["forceToken"] == "1"
    assert len(java_runs(pod)) == 4
    assert run_main(forced, monkeypatch)[1] == 0
    assert len(java_runs(pod)) == 4
    # Back in auto mode the honoured token stays on record
    assert run_main(import_env(pod, export), monkeypatch)[1] == 0
    assert state(site)["forceToken"] == "1"


@needs_tools
def test_https_passes_urls_on_stdin(
    pod: dict[str, Any],
    export: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tmp: Path = pod["tmp"]
    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    argv_log = tmp / "curl.argv"
    # Serves https://exports.example/<name> from the export directory
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> {argv_log}\n'
        'while [ "$1" != --output ]; do shift; done\n'
        "url=$(sed -n 's/^url = \"\\(.*\\)\"$/\\1/p')\n"
        "url=${url%%\\?*}\n"
        f'cp "{export}/${{url##*/}}" "$2"\n'
    )
    curl.chmod(0o755)
    urls = tmp / "urls"
    urls.mkdir()
    (urls / "git.tar.zst").write_text(
        "https://signed.example/git.tar.zst?X-Sig=secret\n"
    )
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    env = {
        **pod["env"],
        "SITE_IMPORT_SOURCE_TYPE": "https",
        "SITE_IMPORT_SOURCE_URL": "https://exports.example/final",
        "SITE_IMPORT_URLS": str(urls),
    }
    _, code = run_main(env, monkeypatch)
    out = capsys.readouterr().out
    assert code == 0
    # TLS for the first request and for every redirect
    assert all(
        "--proto =https --proto-redir =https" in line
        for line in argv_log.read_text().splitlines()
    )
    assert "X-Sig" not in argv_log.read_text()
    assert "X-Sig" not in out
    assert "downloading git.tar.zst from the URL from the Secret" in out
    assert "https://exports.example/final/site.tar.zst" in out
    assert not (
        Path(pod["env"]["SITE_IMPORT_SCRATCH"]) / "download" / "git.tar.zst"
    ).exists()


def test_https_refuses_plain_http(pod: dict[str, Any]) -> None:
    module = load()
    settings = module.Settings.from_env(
        {
            **pod["env"],
            "SITE_IMPORT_SOURCE_TYPE": "https",
            "SITE_IMPORT_SOURCE_URL": "http://exports.example",
        }
    )
    with pytest.raises(module.SiteImportError, match="only https://"):
        module.fetch(settings, "manifest.json")
