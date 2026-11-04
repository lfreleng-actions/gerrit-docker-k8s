# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for verify-import.py, its site probe and its REST snapshot."""

from __future__ import annotations

import copy
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

SCRIPTS = (
    Path(__file__).parent.parent / "K8S" / "charts" / "gerrit" / "files" / "scripts"
)

pytestmark = pytest.mark.xfail(
    strict=True, reason="verify-import.py does not exist yet"
)

needs_tools = pytest.mark.skipif(
    not all(shutil.which(t) for t in ("git", "ssh-keygen")),
    reason="needs git and ssh-keygen",
)


def _load(name: str, filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def verify() -> ModuleType:
    return _load("verify_import", "verify-import.py")


@pytest.fixture(scope="module")
def snapshot() -> ModuleType:
    return _load("verify_import_snapshot_test", "verify_import_snapshot.py")


@pytest.fixture(scope="module")
def probe() -> ModuleType:
    return _load("verify_import_probe_test", "verify_import_probe.py")


# --- REST snapshot ----------------------------------------------------------

DEVELOPERS = "1a2b"
REST: dict[str, Any] = {
    "/a/accounts/?q=is:active+OR+is:inactive&n=500": [
        {"_account_id": i} for i in (1, 2, 3)
    ],
    "/a/accounts/alice": {"_account_id": 2},
    "/a/accounts/2/external.ids": [
        {"identity": "username:alice"},
        {"identity": "mailto:alice@example.org"},
    ],
    "/a/accounts/2/sshkeys": [{"ssh_public_key": "ssh-ed25519 AAAA alice"}],
    "/a/groups/": {"Developers": {"id": DEVELOPERS}},
    f"/a/groups/{DEVELOPERS}/members/": [{"_account_id": 3}, {"_account_id": 2}],
    "/a/projects/?all": {
        "All-Projects": {"state": "ACTIVE"},
        "old": {"state": "HIDDEN"},
    },
    "/a/changes/?q=status:open&n=500": [{"_number": 1}],
    "/a/changes/web~1?o=ALL_REVISIONS&o=DETAILED_LABELS": {
        "status": "NEW",
        "revisions": {"a": {}, "b": {}},
        "labels": {
            "Code-Review": {
                "all": [{"_account_id": 2, "value": -1}, {"_account_id": 3, "value": 0}]
            },
            "Verified": {"all": [{"_account_id": 3}]},
        },
    },
    "/a/changes/web~1/comments": {"README": [{}, {}], "web.txt": [{}]},
    "/a/changes/web~1/drafts": {"README": [{}]},
    "/a/changes/?q=is:starred&n=500": [{"project": "web", "_number": 1}],
    "/a/projects/All-Projects/access": {
        "local": {
            "refs/heads/*": {
                "permissions": {
                    "submit": {"rules": {DEVELOPERS: {"action": "ALLOW"}}},
                    "label-Code-Review": {
                        "label": "Code-Review",
                        "exclusive": True,
                        "rules": {DEVELOPERS: {"action": "ALLOW", "min": -2, "max": 2}},
                    },
                }
            }
        }
    },
}

SNAPSHOT: dict[str, Any] = {
    "accounts": 3,
    "users": {
        "alice": {
            "id": 2,
            "identities": ["mailto:alice@example.org", "username:alice"],
            "sshkeys": ["ssh-ed25519 AAAA alice"],
        }
    },
    "groups": {"Developers": [2, 3]},
    "projects": {"All-Projects": "ACTIVE", "old": "HIDDEN"},
    "open_changes": 1,
    "changes": {
        "web~1": {
            "status": "NEW",
            "patchsets": 2,
            "votes": {"Code-Review": {"2": -1}},
            "comments": 3,
            "drafts": 1,
        }
    },
    "starred": ["web~1"],
    "access": {
        "refs/heads/*": {
            "submit": {
                "exclusive": False,
                "rules": {DEVELOPERS: {"action": "ALLOW", "force": False}},
            },
            "label-Code-Review": {
                "exclusive": True,
                "rules": {
                    DEVELOPERS: {"action": "ALLOW", "force": False, "min": -2, "max": 2}
                },
            },
        }
    },
}


def test_snapshot_takes_what_the_site_serves(snapshot: ModuleType) -> None:
    assert snapshot.take(REST.__getitem__, ["alice"], ["web~1"]) == SNAPSHOT


def test_snapshot_compare_accepts_an_equal_site(snapshot: ModuleType) -> None:
    assert snapshot.compare(SNAPSHOT, copy.deepcopy(SNAPSHOT)) == []


def test_snapshot_compare_lets_a_newer_release_add(snapshot: ModuleType) -> None:
    actual = copy.deepcopy(SNAPSHOT)
    actual["groups"]["Service Users"] = []
    actual["projects"]["New"] = "ACTIVE"
    actual["access"]["refs/heads/*"]["submit"]["rules"]["global:Owners"] = {}
    actual["access"]["refs/tags/*"] = {"create": {"exclusive": False, "rules": {}}}
    assert snapshot.compare(SNAPSHOT, actual) == []


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (["accounts"], 4, "accounts: 4, expected 3"),
        (["users", "alice", "sshkeys"], [], "user alice: "),
        (["groups", "Developers"], [2], "group Developers: [2], expected [2, 3]"),
        (["projects", "old"], "ACTIVE", "project old: 'ACTIVE', expected 'HIDDEN'"),
        (["open_changes"], 0, "open changes: 0, expected 1"),
        (["changes", "web~1", "drafts"], 0, "change web~1: "),
        (["starred"], [], "starred changes: [], expected ['web~1']"),
    ],
)
def test_snapshot_compare_names_each_difference(
    snapshot: ModuleType, path: list[str], value: Any, message: str
) -> None:
    actual = copy.deepcopy(SNAPSHOT)
    target = actual
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert [p for p in snapshot.compare(SNAPSHOT, actual) if p.startswith(message)]


def test_snapshot_compare_names_lost_access(snapshot: ModuleType) -> None:
    actual = copy.deepcopy(SNAPSHOT)
    perms = actual["access"]["refs/heads/*"]
    del perms["submit"]
    perms["label-Code-Review"]["rules"][DEVELOPERS]["max"] = 1
    perms["label-Code-Review"]["exclusive"] = False
    assert snapshot.compare(SNAPSHOT, actual) == [
        "access refs/heads/* label-Code-Review exclusive: False, expected True",
        f"access refs/heads/* label-Code-Review {DEVELOPERS}: "
        f"{{'action': 'ALLOW', 'force': False, 'min': -2, 'max': 1}}, "
        f"expected {{'action': 'ALLOW', 'force': False, 'min': -2, 'max': 2}}",
        "access refs/heads/* submit: missing",
    ]


def test_snapshot_compare_names_a_missing_user_and_change(snapshot: ModuleType) -> None:
    actual = copy.deepcopy(SNAPSHOT)
    actual["users"] = {}
    actual["changes"] = {}
    assert snapshot.compare(SNAPSHOT, actual) == [
        "user alice: missing",
        "change web~1: missing",
    ]


# --- checks on the probe's report -------------------------------------------


def test_state_check(verify: ModuleType) -> None:
    state = {
        "state": "complete",
        "manifestSha256": "abc",
        "warnings": ["1 projects carry rules.pl, and Gerrit 3.12 ..."],
    }
    assert verify.check_state(state, "abc", ["carry rules.pl"]) == "1 warnings"
    with pytest.raises(verify.CheckError) as err:
        verify.check_state(
            {**state, "state": "failed"}, "def", ["rejectImplicitMerges"]
        )
    assert str(err.value) == (
        "state is 'failed', not 'complete'; manifestSha256 is abc, not def; "
        "no warning names 'rejectImplicitMerges'"
    )
    with pytest.raises(verify.CheckError, match="no readable"):
        verify.check_state(None, "abc", [])


def test_host_key_check(verify: ModuleType) -> None:
    expected = {"ssh_host_ed25519_key": "SHA256:a", "ssh_host_rsa_key": "SHA256:b"}
    found = {**expected, "ssh_host_ecdsa_key": "SHA256:c"}
    assert verify.check_host_keys(expected, found) == "2 keys"
    found = {"ssh_host_ed25519_key": "SHA256:x"}
    with pytest.raises(verify.CheckError) as err:
        verify.check_host_keys(expected, found)
    assert str(err.value) == (
        "ssh_host_ed25519_key is SHA256:x, not SHA256:a; "
        "ssh_host_rsa_key is missing, not SHA256:b"
    )


def test_repository_check(verify: ModuleType) -> None:
    expected = {
        "All-Projects.git": {"refs": 3, "meta_config": "a"},
        "All-Users.git": {"refs": 14, "meta_config": "b"},
        "apps/web.git": {"refs": 10, "meta_config": "c"},
    }
    found = {
        "All-Projects.git": {"refs": 3, "meta_config": "new"},
        "All-Users.git": {"refs": 16, "meta_config": "newer"},
        "apps/web.git": {"refs": 10, "meta_config": "c"},
    }
    assert verify.check_repositories(expected, found) == "3 repositories"
    found["All-Users.git"]["refs"] = 13
    found["apps/web.git"]["refs"] = 11
    del found["All-Projects.git"]
    with pytest.raises(verify.CheckError) as err:
        verify.check_repositories(expected, found)
    assert str(err.value) == (
        "All-Projects.git missing; All-Users.git has 13 refs, fewer than 14; "
        "apps/web.git is {'refs': 11, 'meta_config': 'c'}, "
        "the manifest {'refs': 10, 'meta_config': 'c'}"
    )


def test_sequence_check(verify: ModuleType) -> None:
    assert verify.check_sequence(41, 41) == "next change 41"
    assert verify.check_sequence(41, 61) == "next change 61"
    for found in (40, None):
        with pytest.raises(verify.CheckError, match="before 41"):
            verify.check_sequence(41, found)


# --- the probe --------------------------------------------------------------


def git(repo: Path, *args: str, stdin: str | None = None) -> str:
    return subprocess.run(
        ["git", f"--git-dir={repo}", *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def commit(repo: Path, ref: str, path: str, text: str) -> str:
    blob = git(repo, "hash-object", "-w", "--stdin", stdin=text)
    tree = git(repo, "mktree", stdin=f"100644 blob {blob}\t{path}\n")
    sha = git(
        repo,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit-tree",
        tree,
        "-m",
        "Fixture",
    )
    git(repo, "update-ref", ref, sha)
    return sha


@needs_tools
def test_probe_reports_the_site(probe: ModuleType, tmp_path: Path) -> None:
    site = tmp_path / "site"
    (site / "etc").mkdir(parents=True)
    (site / "etc" / "gerrit.config").write_text("[gerrit]\n\tbasePath = repos\n")
    key = site / "etc" / "ssh_host_ed25519_key"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True
    )
    listing = subprocess.run(
        ["ssh-keygen", "-l", "-E", "sha256", "-f", str(key)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    (site / ".chart").mkdir()
    (site / ".chart" / "site-state.json").write_text('{"state": "complete"}')
    store = site / "repos"
    for name in ("All-Projects.git", "apps/web.git"):
        subprocess.run(["git", "init", "-q", "--bare", str(store / name)], check=True)
    commit(store / "All-Projects.git", "refs/heads/master", "x", "x\n")
    meta = commit(store / "apps/web.git", "refs/meta/config", "project.config", "[x]\n")
    commit(store / "apps/web.git", "refs/heads/master", "README", "hello\n")

    assert probe.probe(site) == {
        "state": {"state": "complete"},
        "ssh_host_keys": {key.name: listing.split()[1]},
        "repositories": {
            "All-Projects.git": {"refs": 1, "meta_config": None},
            "apps/web.git": {"refs": 2, "meta_config": meta},
        },
        "change_sequence": None,
    }


@needs_tools
def test_probe_reads_the_change_sequence(probe: ModuleType, tmp_path: Path) -> None:
    store = tmp_path / "git"
    repo = store / "All-Projects.git"
    subprocess.run(["git", "init", "-q", "--bare", str(repo)], check=True)
    blob = git(repo, "hash-object", "-w", "--stdin", stdin="41")
    git(repo, "update-ref", "refs/sequences/changes", blob)
    assert probe.change_sequence(store, "All-Projects") == 41
    assert probe.state(tmp_path) is None


# --- the push ---------------------------------------------------------------


def test_git_env_keeps_the_password_out_of_the_command(verify: ModuleType) -> None:
    env = verify.git_env("alice", "alice@example.org", "not-a-real-password")
    assert env["GIT_CONFIG_KEY_0"] == "http.extraHeader"
    assert env["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")
    assert "not-a-real-password" not in json.dumps(env)
    assert env["GIT_COMMITTER_EMAIL"] == "alice@example.org"


@needs_tools
@pytest.mark.parametrize("lands", [True, False])
def test_push_patchset(verify: ModuleType, tmp_path: Path, lands: bool) -> None:
    """Push over a file URL standing in for Gerrit's /a/<project>."""
    repo = tmp_path / "a" / "web"
    subprocess.run(["git", "init", "-q", "--bare", str(repo)], check=True)
    commit(repo, "refs/changes/01/1/1", "README", "first\n")
    revisions = {"r1": {"ref": "refs/changes/01/1/1"}}

    def get(path: str) -> Any:
        assert path == "/a/changes/web~1?o=ALL_REVISIONS"
        pushed = git(repo, "for-each-ref", "refs/for/")
        found = dict(revisions, r2={}) if pushed and lands else revisions
        return {
            "project": "web",
            "branch": "main",
            "current_revision": "r1",
            "revisions": found,
        }

    push = {"user": "alice", "email": "alice@example.org", "change": "web~1"}
    base = tmp_path.as_uri()
    if lands:
        assert verify.push_patchset(base, get, push, "pw") == "web~1 patch set 2"
        log = git(repo, "log", "-1", "--format=%ae %ce", "refs/for/main")
        assert log == "fixture@example.invalid alice@example.org"
    else:
        with pytest.raises(
            verify.CheckError, match="1 patch sets after the push, not 2"
        ):
            verify.push_patchset(base, get, push, "pw")


def test_parse_args(verify: ModuleType) -> None:
    args = verify.parse_args(
        [
            "--namespace",
            "ns",
            "--manifest",
            "m.json",
            "--expected",
            "e.json",
            "--password-file",
            "pw",
            "--expect-warning",
            "a",
            "--expect-warning",
            "b",
        ]
    )
    assert (args.fullname, args.pod, args.container, args.site) == (
        "gerrit",
        "gerrit-0",
        "gerrit",
        "/var/gerrit",
    )
    assert args.expect_warning == ["a", "b"]
    assert args.report is None
