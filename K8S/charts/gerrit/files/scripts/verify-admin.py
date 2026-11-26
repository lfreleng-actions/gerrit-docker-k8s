# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Report the administrative status of a running Gerrit, read-only.

Over SSH, as an administrator, through the operator's own ``ssh``
client and key, it checks:

- the version, against ``--expect-version`` when given;
- the plugins and their status: each ``--expect-plugin`` must be
  loaded and enabled, and any other loaded plugin is reported;
- the task queue: totals by state, failing on any task running or
  ready and queued more than ``--task-minutes`` ago, as Gerrit gives
  no time a task became active. Start times are read in
  ``--server-timezone``, the JVM's zone, as instants against the clock
  ``gerrit show-caches`` gives before and after the queue, across
  daylight saving changes; one that could fall on either side of the
  limit fails;
- the summary Gerrit reports for memory, threads and SSH users.

Over REST, only with a credential from a netrc entry
(``--netrc-machine``) or a ``--password-file``, it confirms the
account holds ``administrateServer`` and then runs Gerrit's
consistency check of accounts, account external IDs and groups,
printing each problem and failing on any ``ERROR`` or ``FATAL``. It
then lists each index's versions and which of them Gerrit searches and
writes, from ``GET /a/config/server/indexes/``, and says the check is
unavailable where that endpoint is absent. Gerrit reports no readiness
beyond those flags, and the script infers none.

Nothing here changes the site: the consistency check is the only
request that is not a ``GET``. The credential is never printed, and is
sent over plain HTTP only to a loopback address, as a
``kubectl port-forward`` gives. Standard library only. Each failed
check prints one ``FAIL`` line with its reason, and the script then
exits non-zero. ``verify_admin_ssh.py``, beside it, holds the SSH
checks.

    python3 K8S/charts/gerrit/files/scripts/verify-admin.py \\
        --ssh-host gerrit.example.org --ssh-user admin \\
        --expect-plugin replication \\
        --url http://127.0.0.1:8080/r --netrc-machine gerrit.example.org
"""

from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import netrc
import sys
import urllib.error
import urllib.parse
import urllib.request
import zoneinfo
from collections.abc import Callable
from pathlib import Path
from typing import Any

from verify_admin_ssh import (
    DAY,
    CheckError,
    Outcome,
    Ssh,
    Unavailable,
    check_plugins,
    check_summary,
    check_tasks,
    check_version,
    run_check,
)

XSSI_PREFIX = ")]}'"
HTTP_TIMEOUT = 60
CONSISTENCY_RESULTS = (
    ("accounts", "check_accounts_result"),
    ("account external IDs", "check_account_external_ids_result"),
    ("groups", "check_groups_result"),
)
FAILING_STATUSES = ("ERROR", "FATAL")
INDEXES_PATH = "/a/config/server/indexes/"
# Gerrit reports each index version's is_search and is_write
INDEX_FLAGS = ("search", "write")


class HttpError(CheckError):
    """A REST call answered with an HTTP error status."""

    def __init__(self, reason: str, code: int) -> None:
        super().__init__(reason)
        self.code = code


def is_loopback(host: str) -> bool:
    """Whether a URL's host is a loopback address."""
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def url_problem(url: str) -> str | None:
    """Why a REST base URL may not take the credential, or None."""
    try:
        parts = urllib.parse.urlsplit(url)
        _ = parts.port  # raises on a port that is not a number
    except ValueError:
        return "--url is not a valid URL"
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "--url must be an http or https URL"
    if parts.username is not None or parts.password is not None:
        return "--url must not carry a user or password"
    if parts.query or parts.fragment:
        return "--url must not carry a query or fragment"
    if parts.scheme == "http" and not is_loopback(parts.hostname):
        return (
            f"refusing to send a credential over plain HTTP to {parts.hostname}; "
            "use https, or a loopback address such as kubectl port-forward gives"
        )
    return None


def netrc_credential(path: Path, machine: str) -> tuple[str, str]:
    """The login and password of one netrc entry, and no default entry."""
    try:
        entries = netrc.netrc(str(path))
    except FileNotFoundError as exc:
        raise CheckError(f"no netrc file at {path}") from exc
    except netrc.NetrcParseError as exc:
        # The parser's message can quote a token, which may be a password
        raise CheckError(f"cannot parse {path} near line {exc.lineno}") from None
    except OSError as exc:
        raise CheckError(f"cannot read {path}: {exc.strerror}") from exc
    if machine not in entries.hosts:
        raise CheckError(f"no machine {machine} in {path}")
    login, _account, password = entries.hosts[machine]
    if not login or not password:
        raise CheckError(f"machine {machine} in {path} lacks a login or password")
    return login, password


def file_credential(path: Path, user: str) -> tuple[str, str]:
    """The user and the password a file holds."""
    try:
        password = path.read_text(encoding="utf-8").rstrip("\r\n")
    except OSError as exc:
        raise CheckError(f"cannot read {path}: {exc.strerror}") from exc
    except UnicodeDecodeError:
        raise CheckError(f"{path} is not UTF-8 text") from None
    if not password:
        raise CheckError(f"{path} is empty")
    return user, password


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects, so the credential goes to the URL given and no other."""

    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        """Decline every redirect; the opener then raises HTTPError."""
        return None


class Rest:
    """Authenticated REST calls; the credential is read on the first call."""

    def __init__(self, base: str, credential: Callable[[], tuple[str, str]]) -> None:
        self.base = base.rstrip("/")
        self._credential = credential
        self._auth: str | None = None
        handlers: list[urllib.request.BaseHandler] = [_NoRedirect()]
        if urllib.parse.urlsplit(base).scheme == "http":
            # Plain HTTP stays on the loopback address, never via a proxy
            handlers.append(urllib.request.ProxyHandler({}))
        self._opener = urllib.request.build_opener(*handlers)

    def call(self, method: str, path: str, body: Any = None) -> Any:
        """Send one request and decode Gerrit's JSON reply."""
        if self._auth is None:
            user, password = self._credential()
            token = base64.b64encode(f"{user}:{password}".encode()).decode()
            self._auth = f"Basic {token}"
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json; charset=UTF-8")
        req.add_unredirected_header("Authorization", self._auth)
        try:
            with self._opener.open(req, timeout=HTTP_TIMEOUT) as resp:
                reply: bytes = resp.read()
        except urllib.error.HTTPError as exc:
            exc.close()
            hint = " (the credential was refused)" if exc.code == 401 else ""
            raise HttpError(
                f"{method} {path}: HTTP {exc.code}{hint}", exc.code
            ) from None
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise CheckError(f"{method} {path}: {reason}") from None
        text = reply.decode("utf-8", errors="replace")
        if text.startswith(XSSI_PREFIX):
            text = text[len(XSSI_PREFIX) :]
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise CheckError(f"{method} {path}: reply is not JSON") from None


def check_admin(rest: Rest) -> Outcome:
    """The credential's account holds ``administrateServer``."""
    caps = rest.call("GET", "/a/accounts/self/capabilities")
    if not isinstance(caps, dict):
        raise CheckError("GET /a/accounts/self/capabilities: not a map")
    if caps.get("administrateServer") is not True:
        raise CheckError(
            "the credential's account lacks administrateServer, "
            "so the consistency check was not run"
        )
    return Outcome("granted")


def check_consistency(rest: Rest) -> Outcome:
    """Gerrit's own consistency check; fail on an ERROR or FATAL problem."""
    reply = rest.call(
        "POST",
        "/a/config/server/check.consistency",
        {"check_accounts": {}, "check_account_external_ids": {}, "check_groups": {}},
    )
    if not isinstance(reply, dict):
        raise CheckError("check.consistency: reply is not a map")
    lines, failing = [], 0
    for label, key in CONSISTENCY_RESULTS:
        result = reply.get(key)
        # Gerrit leaves out an empty list, so only absence means none
        problems = result.get("problems", []) if isinstance(result, dict) else None
        if not isinstance(problems, list):
            raise CheckError(f"check.consistency: no readable {key}")
        for problem in problems:
            status = problem.get("status") if isinstance(problem, dict) else None
            if not isinstance(status, str):
                raise CheckError(f"check.consistency: unreadable problem in {key}")
            lines.append(f"{status} {label}: {problem.get('message', '')}")
            failing += status in FAILING_STATUSES
    if failing:
        raise CheckError(f"{failing} of {len(lines)} problems are errors", lines)
    return Outcome(f"{len(lines)} problems, none an error", lines)


def check_indexes(rest: Rest) -> Outcome:
    """Each index's versions, and which Gerrit searches and writes."""
    try:
        reply = rest.call("GET", INDEXES_PATH)
    except HttpError as exc:
        if exc.code == 404:
            raise Unavailable(
                f"this Gerrit has no {INDEXES_PATH}; "
                "read index/gerrit_index.config on the site"
            ) from None
        raise
    indexes = list(reply.values()) if isinstance(reply, dict) else reply
    if not isinstance(indexes, list) or not indexes:
        raise CheckError(f"GET {INDEXES_PATH}: no list of indexes")
    lines = []
    for index in indexes:
        name = index.get("name") if isinstance(index, dict) else None
        versions = index.get("versions") if isinstance(index, dict) else None
        if not isinstance(name, str) or not isinstance(versions, dict) or not versions:
            raise CheckError(f"GET {INDEXES_PATH}: unreadable index entry")
        for version, flags in sorted(versions.items(), key=lambda v: (len(v[0]), v[0])):
            if not isinstance(flags, dict) or not all(
                isinstance(flags.get(f"is_{f}"), bool) for f in INDEX_FLAGS
            ):
                raise CheckError(f"GET {INDEXES_PATH}: unreadable version of {name}")
            uses = [f for f in INDEX_FLAGS if flags[f"is_{f}"]]
            lines.append(
                f"{name} {version}: {', '.join(uses) or 'neither searched nor written'}"
            )
    return Outcome(
        f"{len(indexes)} indexes; Gerrit reports no readiness beyond search and write",
        lines,
    )


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse and cross-check the command line."""
    parser = argparse.ArgumentParser(
        description="Report the administrative status of a running Gerrit."
    )
    ssh = parser.add_argument_group("SSH, as an administrator")
    ssh.add_argument("--ssh-host", required=True)
    ssh.add_argument("--ssh-port", type=int, default=29418)
    ssh.add_argument("--ssh-user", help="default: what the ssh client chooses")
    ssh.add_argument("--expect-version", help="the version Gerrit must report")
    ssh.add_argument(
        "--expect-plugin",
        action="append",
        default=[],
        help="a plugin that must be loaded and enabled; repeatable",
    )
    ssh.add_argument(
        "--task-minutes",
        type=int,
        default=30,
        help="fail on a task running or ready and queued longer ago, under a day "
        "(default: 30)",
    )
    ssh.add_argument(
        "--server-timezone",
        default="UTC",
        help="the time zone Gerrit's JVM runs in, used to read show-queue's "
        "times; a show-caches clock not in UTC must be in it (default: UTC)",
    )
    rest = parser.add_argument_group("REST, only with a credential")
    rest.add_argument("--url", help="base URL, with the URL prefix")
    source = rest.add_mutually_exclusive_group()
    source.add_argument(
        "--netrc-machine", help="the netrc entry that holds the credential"
    )
    source.add_argument(
        "--password-file", type=Path, help="a file holding the HTTP password"
    )
    rest.add_argument("--netrc-file", type=Path, help="default: ~/.netrc")
    rest.add_argument("--http-user", help="the user --password-file belongs to")
    args = parser.parse_args(argv)
    if args.ssh_host.startswith("-"):
        parser.error("--ssh-host must not start with '-'")
    if not 0 < args.task_minutes < DAY // 60:
        parser.error("--task-minutes must be between 1 and 1439")
    try:
        args.server_timezone = zoneinfo.ZoneInfo(args.server_timezone)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        parser.error(f"--server-timezone: no time zone {args.server_timezone}")
    has_credential = bool(args.netrc_machine or args.password_file)
    if args.url is None:
        if has_credential or args.http_user or args.netrc_file:
            parser.error("the REST options need --url")
        return args
    if not has_credential:
        parser.error("--url needs --netrc-machine or --password-file")
    if bool(args.password_file) != bool(args.http_user):
        parser.error("--password-file and --http-user go together")
    if args.netrc_file and not args.netrc_machine:
        parser.error("--netrc-file needs --netrc-machine")
    if problem := url_problem(args.url):
        parser.error(problem)
    return args


def main(argv: list[str] | None = None) -> int:
    """Run every check and return the process exit code."""
    args = parse_args(argv)
    ssh = Ssh(args.ssh_host, args.ssh_port, args.ssh_user)
    failures: list[str] = []

    run_check(failures, "version", lambda: check_version(ssh, args.expect_version))
    run_check(failures, "plugins", lambda: check_plugins(ssh, args.expect_plugin))
    run_check(
        failures,
        "task queue",
        lambda: check_tasks(ssh, args.task_minutes, args.server_timezone),
    )
    run_check(failures, "summary", lambda: check_summary(ssh))

    if args.url is None:
        print("skip index versions: needs --url and a credential", flush=True)
    else:

        def credential() -> tuple[str, str]:
            if args.netrc_machine:
                path = args.netrc_file or Path.home() / ".netrc"
                return netrc_credential(path, args.netrc_machine)
            return file_credential(args.password_file, args.http_user)

        rest = Rest(args.url, credential)
        if run_check(failures, "administrateServer", lambda: check_admin(rest)):
            run_check(
                failures,
                "consistency of accounts, external IDs and groups",
                lambda: check_consistency(rest),
            )
            run_check(failures, "index versions", lambda: check_indexes(rest))

    if failures:
        print(f"{len(failures)} check(s) failed", file=sys.stderr)
        return 1
    print("All admin checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
