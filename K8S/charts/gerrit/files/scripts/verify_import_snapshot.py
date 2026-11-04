# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""What a Gerrit site serves over REST, as a comparable snapshot.

``tests/k8s/build_fixture_site.py`` takes a snapshot of the source
site before it exports it; ``verify-import.py`` takes one of the
imported release and compares the two. Both pass ``get``, which
fetches a REST path as the site's administrator and returns the
parsed reply, so this module needs neither a session nor a server.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Callable
from typing import Any

Get = Callable[[str], Any]

# Up to the default query limit of an administrator, ample for a fixture
QUERY_LIMIT = 500


def quote(name: str) -> str:
    """Encode a project, account or group name for one path segment."""
    return urllib.parse.quote(name, safe="")


def user(get: Get, name: str) -> dict[str, Any]:
    """An account's id, external identities and SSH keys."""
    info = get(f"/a/accounts/{quote(name)}")
    ids = get(f"/a/accounts/{info['_account_id']}/external.ids")
    keys = get(f"/a/accounts/{info['_account_id']}/sshkeys")
    return {
        "id": info["_account_id"],
        "identities": sorted(e["identity"] for e in ids),
        "sshkeys": sorted(k["ssh_public_key"] for k in keys),
    }


def groups(get: Get) -> dict[str, list[int]]:
    """Every group the administrator sees, with its direct members."""
    found = {}
    for name, info in get("/a/groups/").items():
        members = get(f"/a/groups/{info['id']}/members/")
        found[name] = sorted(m["_account_id"] for m in members)
    return found


def votes(labels: dict[str, Any]) -> dict[str, dict[str, int]]:
    """The non-zero votes on each label, by account id."""
    found = {}
    for label, detail in labels.items():
        cast = {
            str(v["_account_id"]): v["value"]
            for v in detail.get("all", [])
            if v.get("value")
        }
        if cast:
            found[label] = cast
    return found


def change(get: Get, ident: str) -> dict[str, Any]:
    """One change's status, patch sets, votes and comments."""
    info = get(f"/a/changes/{ident}?o=ALL_REVISIONS&o=DETAILED_LABELS")
    comments = get(f"/a/changes/{ident}/comments")
    drafts = get(f"/a/changes/{ident}/drafts")
    return {
        "status": info["status"],
        "patchsets": len(info.get("revisions", {})),
        "votes": votes(info.get("labels", {})),
        "comments": sum(len(c) for c in comments.values()),
        "drafts": sum(len(d) for d in drafts.values()),
    }


def rule(found: dict[str, Any]) -> dict[str, Any]:
    """A permission rule without the fields its defaults leave out."""
    kept = {
        "action": found.get("action", "ALLOW"),
        "force": bool(found.get("force")),
        "min": found.get("min"),
        "max": found.get("max"),
    }
    return {k: v for k, v in kept.items() if v is not None}


def access(local: dict[str, Any]) -> dict[str, Any]:
    """A project's own access sections, normalised across API versions."""
    sections: dict[str, Any] = {}
    for ref, section in local.items():
        for name, perm in section.get("permissions", {}).items():
            sections.setdefault(ref, {})[name] = {
                "exclusive": bool(perm.get("exclusive")),
                "rules": {g: rule(r) for g, r in perm.get("rules", {}).items()},
            }
    return sections


def take(get: Get, users: list[str], changes: list[str]) -> dict[str, Any]:
    """Snapshot the site: the named users and changes, and site totals."""
    everyone = get(f"/a/accounts/?q=is:active+OR+is:inactive&n={QUERY_LIMIT}")
    projects = get("/a/projects/?all")
    starred = get(f"/a/changes/?q=is:starred&n={QUERY_LIMIT}")
    return {
        "accounts": len(everyone),
        "users": {name: user(get, name) for name in users},
        "groups": groups(get),
        "projects": {n: p.get("state", "ACTIVE") for n, p in projects.items()},
        "open_changes": len(get(f"/a/changes/?q=status:open&n={QUERY_LIMIT}")),
        "changes": {ident: change(get, ident) for ident in changes},
        "starred": sorted(f"{c['project']}~{c['_number']}" for c in starred),
        "access": access(get("/a/projects/All-Projects/access").get("local", {})),
    }


def differ(name: str, expected: Any, actual: Any) -> list[str]:
    return [] if expected == actual else [f"{name}: {actual!r}, expected {expected!r}"]


def missing_or_differ(
    name: str, expected: dict[str, Any], actual: dict[str, Any]
) -> list[str]:
    """Compare each expected entry; entries only actual has may stay."""
    found = []
    for key, value in sorted(expected.items()):
        if key not in actual:
            found.append(f"{name} {key}: missing")
        else:
            found.extend(differ(f"{name} {key}", value, actual[key]))
    return found


def access_differ(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    """The access sections must be equal: no rule missing, changed or added."""
    found: list[str] = []
    for ref in sorted(set(expected) | set(actual)):
        want, have = expected.get(ref, {}), actual.get(ref, {})
        for perm in sorted(set(want) | set(have)):
            where = f"access {ref} {perm}"
            if perm not in have:
                found.append(f"{where}: missing")
            elif perm not in want:
                found.append(f"{where}: added, {have[perm]!r}")
            else:
                found.extend(
                    differ(
                        f"{where} exclusive",
                        want[perm]["exclusive"],
                        have[perm]["exclusive"],
                    )
                )
                found.extend(
                    rules_differ(where, want[perm]["rules"], have[perm]["rules"])
                )
    return found


def rules_differ(
    name: str, expected: dict[str, Any], actual: dict[str, Any]
) -> list[str]:
    """Compare one permission's rules both ways, by group."""
    found = missing_or_differ(name, expected, actual)
    found.extend(
        f"{name} {group}: added, {actual[group]!r}"
        for group in sorted(set(actual) - set(expected))
    )
    return found


def compare(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    """Name every way the imported site differs from the exported one."""
    return [
        *differ("accounts", expected["accounts"], actual["accounts"]),
        *missing_or_differ("user", expected["users"], actual["users"]),
        *missing_or_differ("group", expected["groups"], actual["groups"]),
        *missing_or_differ("project", expected["projects"], actual["projects"]),
        *differ("open changes", expected["open_changes"], actual["open_changes"]),
        *missing_or_differ("change", expected["changes"], actual["changes"]),
        *differ("starred changes", expected["starred"], actual["starred"]),
        *access_differ(expected["access"], actual["access"]),
    ]
