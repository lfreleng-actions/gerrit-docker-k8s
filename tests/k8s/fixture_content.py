# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Fill a running Gerrit with the content a site import must carry.

``build_fixture_site.py`` calls ``populate`` against an official
``gerritcodereview/gerrit`` container, whose site runs
DEVELOPMENT_BECOME_ANY_ACCOUNT. The content spans what an import can
lose: accounts with identities, HTTP passwords and SSH keys, groups,
access rules, nested and hidden projects, a project with its own
configuration, branches and tags, and changes with patch sets, votes,
comments, drafts and stars, open, abandoned and merged.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

USERS = {"alice": "Alice Fixture", "bob": "Bob Fixture"}
EMAIL = "{}@example.org"
PROJECTS = ("apps/web", "apps/tools/cli", "plain", "archived/old", "rules")
XSSI = ")]}'"


def quote(name: str) -> str:
    """Encode a project name for one path segment."""
    return urllib.parse.quote(name, safe="")


class FixtureError(Exception):
    """Building the fixture failed; the message says where."""


@dataclass
class Client:
    """REST calls as one account: a session's cookie or basic auth."""

    base: str
    opener: urllib.request.OpenerDirector
    headers: dict[str, str] = field(default_factory=dict)

    def call(
        self, method: str, path: str, body: Any = None, text: str | None = None
    ) -> Any:
        headers = dict(self.headers)
        data = None
        if text is not None:
            data, headers["Content-Type"] = text.encode(), "text/plain"
        elif body is not None:
            data, headers["Content-Type"] = (
                json.dumps(body).encode(),
                "application/json",
            )
        request = urllib.request.Request(
            self.base + path, data=data, method=method, headers=headers
        )
        try:
            with self.opener.open(request, timeout=60) as resp:
                reply = resp.read().decode()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace").strip()[:200]
            raise FixtureError(f"{method} {path}: HTTP {exc.code} {detail}") from exc
        reply = reply.removeprefix(XSSI).strip()
        return json.loads(reply) if reply else None

    def get(self, path: str) -> Any:
        return self.call("GET", path)


def basic(base: str, user: str, password: str) -> Client:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return Client(
        base, urllib.request.build_opener(), {"Authorization": f"Basic {token}"}
    )


def ssh_key(scratch: Path, name: str) -> str:
    key = scratch / f"id_{name}"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(key)],
        check=True,
    )
    return key.with_suffix(".pub").read_text().strip()


def create_users(admin: Client, scratch: Path) -> dict[str, tuple[int, str]]:
    """Create each fixture user; return its id and HTTP password."""
    made = {}
    for name, full in USERS.items():
        password = secrets.token_urlsafe(18)
        info = admin.call(
            "PUT",
            f"/a/accounts/{name}",
            {
                "name": full,
                "email": EMAIL.format(name),
                "ssh_key": ssh_key(scratch, name),
                "http_password": password,
            },
        )
        made[name] = (info["_account_id"], password)
    return made


def create_groups(admin: Client) -> dict[str, str]:
    """Create the groups; return each one's UUID."""
    wanted = {"Developers": ["alice", "bob"], "Reviewers": ["bob"]}
    uuids = {}
    for name, members in wanted.items():
        info = admin.call(
            "PUT", f"/a/groups/{name}", {"description": f"Fixture {name}"}
        )
        admin.call("POST", f"/a/groups/{info['id']}/members.add", {"members": members})
        uuids[name] = info["id"]
    return uuids


def grant(admin: Client, groups: dict[str, str]) -> None:
    """Add rules for the fixture groups to All-Projects."""
    developers, reviewers = groups["Developers"], groups["Reviewers"]
    sections = {
        "refs/heads/*": {
            "permissions": {
                "label-Code-Review": {
                    "label": "Code-Review",
                    "rules": {developers: {"action": "ALLOW", "min": -2, "max": 2}},
                },
                "submit": {"rules": {developers: {"action": "ALLOW"}}},
            }
        },
        "refs/heads/stable": {
            "permissions": {
                "push": {
                    "exclusive": True,
                    "rules": {reviewers: {"action": "ALLOW", "force": True}},
                }
            }
        },
    }
    admin.call("POST", "/a/projects/All-Projects/access", {"add": sections})


def create_projects(admin: Client) -> None:
    for name in PROJECTS:
        admin.call(
            "PUT",
            f"/a/projects/{quote(name)}",
            {"create_empty_commit": True, "branches": ["master"]},
        )
    admin.call(
        "PUT", f"/a/projects/{quote('archived/old')}/config", {"state": "HIDDEN"}
    )
    admin.call(
        "PUT",
        "/a/projects/plain/config",
        {
            "description": "A project with its own settings",
            "max_object_size_limit": "10m",
        },
    )
    web = quote("apps/web")
    admin.call("PUT", f"/a/projects/{web}/branches/stable", {"revision": "master"})
    admin.call(
        "PUT",
        f"/a/projects/{web}/tags/v1.0",
        {"revision": "master", "message": "Release 1.0"},
    )


def http_env(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {token}",
        "GIT_AUTHOR_NAME": USERS[user],
        "GIT_AUTHOR_EMAIL": EMAIL.format(user),
        "GIT_COMMITTER_NAME": USERS[user],
        "GIT_COMMITTER_EMAIL": EMAIL.format(user),
    }


def push_change(base: str, scratch: Path, user: str, password: str) -> None:
    """Push a change to apps/web over HTTP as user, then a second patch set."""
    work = scratch / "push"
    env = http_env(user, password)
    url = f"{base}/a/apps/web"

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(work), *args], env=env, check=True)

    subprocess.run(["git", "clone", "--quiet", url, str(work)], env=env, check=True)
    change_id = "I" + secrets.token_hex(20)
    for round_ in (1, 2):
        (work / "web.txt").write_text(f"revision {round_}\n")
        git("add", "web.txt")
        message = f"Add the web page\n\nChange-Id: {change_id}\n"
        git("commit", "--quiet", *(["--amend"] if round_ == 2 else []), "-m", message)
        git("push", "--quiet", url, "HEAD:refs/for/master")


def rest_change(admin: Client, project: str, subject: str) -> str:
    """Create a change over REST with two patch sets; return its id."""
    info = admin.call(
        "POST",
        "/a/changes/",
        {"project": project, "branch": "master", "subject": subject},
    )
    ident = f"{quote(project)}~{info['_number']}"
    admin.call("PUT", f"/a/changes/{ident}/edit/README", text=f"{subject}\n")
    admin.call("POST", f"/a/changes/{ident}/edit:publish", {})
    return ident


def review(
    client: Client, ident: str, label: int, comment: str, path: str = "README"
) -> None:
    client.call(
        "POST",
        f"/a/changes/{ident}/revisions/current/review",
        {
            "message": comment,
            "labels": {"Code-Review": label},
            "comments": {path: [{"line": 1, "message": comment}]},
        },
    )


def create_changes(
    base: str, admin: Client, users: dict[str, tuple[int, str]], scratch: Path
) -> list[str]:
    """Create changes in every state; return their ids."""
    alice, bob = (basic(base, n, users[n][1]) for n in ("alice", "bob"))
    push_change(base, scratch, "alice", users["alice"][1])
    pushed = admin.get(f"/a/changes/?q=project:apps/web+owner:{users['alice'][0]}")
    web = f"{quote('apps/web')}~{pushed[0]['_number']}"
    tool = rest_change(admin, "apps/tools/cli", "Add the tool")
    gone = rest_change(admin, "apps/web", "Try a dead end")
    merged = rest_change(admin, "plain", "Describe the project")
    review(bob, tool, 2, "Fine by me")
    review(alice, tool, -1, "Needs a usage line")
    review(bob, web, 1, "Looks right", "web.txt")
    admin.call(
        "PUT",
        f"/a/changes/{tool}/revisions/current/drafts",
        {"path": "README", "line": 1, "message": "Unsent thought"},
    )
    admin.call("PUT", f"/a/accounts/self/starred.changes/{web}")
    admin.call("POST", f"/a/changes/{gone}/abandon", {})
    review(admin, merged, 2, "Ship it")
    admin.call("POST", f"/a/changes/{merged}/submit", {})
    return [web, tool, gone, merged]


def populate(base: str, admin: Client, scratch: Path) -> dict[str, Any]:
    """Fill the site; return what the verification needs to know."""
    users = create_users(admin, scratch)
    grant(admin, create_groups(admin))
    create_projects(admin)
    changes = create_changes(base, admin, users, scratch)
    return {
        "users": sorted(USERS),
        "changes": changes,
        "push": {"user": "alice", "email": EMAIL.format("alice"), "change": changes[0]},
        "password": users["alice"][1],
    }
