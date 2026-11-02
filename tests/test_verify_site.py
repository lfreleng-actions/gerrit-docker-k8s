# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for verify-site.py and its companion scripts, against a stub Gerrit."""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import urllib.parse
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = (
    Path(__file__).parent.parent
    / "K8S"
    / "charts"
    / "gerrit"
    / "files"
    / "scripts"
    / "verify-site.py"
)
_spec = importlib.util.spec_from_file_location("verify_site", SCRIPT)
assert _spec is not None and _spec.loader is not None
verify_site = importlib.util.module_from_spec(_spec)
sys.modules["verify_site"] = verify_site
_spec.loader.exec_module(verify_site)

XSRF = "xsrf-token"


@dataclass
class StubGerrit:
    """What the stub serves, and the requests it saw."""

    prefix: str
    accounts: set[int] = field(default_factory=set)
    dev_auth: bool = True
    base_visited: bool = False
    server_info: dict[str, object] = field(
        default_factory=lambda: {
            "auth": {"auth_type": "DEVELOPMENT_BECOME_ANY_ACCOUNT"},
            "download": {"schemes": {"http": {}, "ssh": {}}, "archives": []},
        }
    )
    requests: list[tuple[str, str, str, str]] = field(default_factory=list)
    projects: dict[str, dict[str, object]] = field(default_factory=dict)
    changes: list[dict[str, object]] = field(default_factory=list)
    # Status and report of the healthcheck plugin; None when not loaded
    healthcheck: tuple[int, dict[str, object]] | None = None


def _handler(stub: StubGerrit) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def _session(self) -> int | None:
            for part in self.headers.get("Cookie", "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == "GerritAccount" and value.startswith("sess-"):
                    return int(value.removeprefix("sess-"))
            return None

        def _reply(
            self, code: int, body: bytes = b"", headers: dict[str, str] | None = None
        ) -> None:
            self.send_response(code)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _sign_in(self, account: int) -> None:
            self._reply(
                302,
                headers={
                    "Location": f"{stub.prefix}/",
                    "Set-Cookie": (
                        f"GerritAccount=sess-{account}; Path={stub.prefix}/; HttpOnly"
                    ),
                },
            )

        def _route(self, method: str) -> None:
            url = urllib.parse.urlsplit(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode() if length else ""
            ctype = self.headers.get("Content-Type", "")
            stub.requests.append((method, self.path, ctype, body))
            p = stub.prefix
            if method == "GET" and url.path == f"{p}/":
                stub.base_visited = True
                headers = {}
                if self._session() is not None:
                    headers["Set-Cookie"] = f"XSRF_TOKEN={XSRF}; Path={p}/"
                self._reply(200, b"<html></html>", headers)
            elif url.path == f"{p}/login/":
                if not stub.base_visited:
                    # Gerrit's first-time redirect, ahead of the login servlet
                    self._reply(302, headers={"Location": f"{p}/intro.html"})
                    return
                if not stub.dev_auth:
                    self._reply(404)
                    return
                query = urllib.parse.parse_qs(url.query)
                form = urllib.parse.parse_qs(body)
                if method == "GET" and query.get("account_id"):
                    account = int(query["account_id"][0])
                    if account in stub.accounts:
                        self._sign_in(account)
                    else:
                        self._reply(404)
                elif (
                    method == "POST"
                    and ctype == "application/x-www-form-urlencoded"
                    and form.get("action") == ["create_account"]
                ):
                    account = 1000000 + len(stub.accounts)
                    stub.accounts.add(account)
                    self._sign_in(account)
                else:
                    # A GET with action=create_account only shows the form
                    self._reply(200, b"<html>login</html>")
            elif method == "GET" and url.path == f"{p}/config/server/info":
                payload = json.dumps(stub.server_info).encode()
                self._reply(200, b")]}'\n" + payload)
            elif (
                method == "GET"
                and url.path == f"{p}/config/server/healthcheck~status"
                and stub.healthcheck is not None
            ):
                code, report = stub.healthcheck
                self._reply(code, b")]}'\n" + json.dumps(report).encode())
            elif method == "GET" and url.path == f"{p}/a/accounts/self":
                session = self._session()
                if session is None:
                    self._reply(401)
                else:
                    payload = json.dumps({"_account_id": session}).encode()
                    self._reply(200, b")]}'\n" + payload)
            elif url.path.startswith(f"{p}/projects/") or url.path == f"{p}/changes/":
                self._write(method, url.path.removeprefix(p), body)
            else:
                self._reply(404)

        def _write(self, method: str, path: str, body: str) -> None:
            """Create a project or a change as the signed-in account."""
            if self._session() is None or self.headers.get("X-Gerrit-Auth") != XSRF:
                self._reply(403)
                return
            payload = json.loads(body)
            reply: dict[str, object]
            if method == "PUT" and path.startswith("/projects/"):
                name = path.removeprefix("/projects/")
                stub.projects[name] = payload
                reply = {"id": name, "name": name}
            elif method == "POST" and path == "/changes/":
                if payload.get("project") not in stub.projects:
                    self._reply(404)
                    return
                stub.changes.append(payload)
                reply = {"project": payload["project"], "_number": len(stub.changes)}
            else:
                self._reply(405)
                return
            self._reply(201, b")]}'\n" + json.dumps(reply).encode())

        def do_GET(self) -> None:
            self._route("GET")

        def do_POST(self) -> None:
            self._route("POST")

        def do_PUT(self) -> None:
            self._route("PUT")

    return Handler


@pytest.fixture(params=["/r", ""], ids=["prefix", "root"])
def gerrit(request: pytest.FixtureRequest) -> Iterator[tuple[StubGerrit, str]]:
    """Serve a stub Gerrit; yield it with the base URL under its prefix."""
    stub = StubGerrit(prefix=request.param)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(stub))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield stub, f"http://127.0.0.1:{server.server_port}{stub.prefix}"
    finally:
        server.shutdown()
        server.server_close()


def _posts(stub: StubGerrit) -> list[tuple[str, str, str, str]]:
    return [r for r in stub.requests if r[0] == "POST"]


def test_becomes_existing_account(gerrit: tuple[StubGerrit, str]) -> None:
    stub, base = gerrit
    stub.accounts.add(1000000)
    _, xsrf, account = verify_site.start_session(base)
    assert (xsrf, account) == (XSRF, 1000000)
    assert _posts(stub) == []
    assert all(path.startswith(f"{stub.prefix}/") for _, path, _, _ in stub.requests)


def test_creates_first_account_with_form_post(
    gerrit: tuple[StubGerrit, str],
) -> None:
    stub, base = gerrit
    _, xsrf, account = verify_site.start_session(base)
    assert (xsrf, account) == (XSRF, 1000000)
    assert _posts(stub) == [
        (
            "POST",
            f"{stub.prefix}/login/",
            "application/x-www-form-urlencoded",
            "action=create_account",
        )
    ]
    assert stub.accounts == {1000000}


def test_session_reaches_marker_requests(gerrit: tuple[StubGerrit, str]) -> None:
    _, base = gerrit
    opener, _, _ = verify_site.start_session(base)
    with opener.open(f"{base}/a/accounts/self") as resp:
        assert verify_site.parse_gerrit_json(resp.read()) == {"_account_id": 1000000}


def test_fails_without_development_login(gerrit: tuple[StubGerrit, str]) -> None:
    stub, base = gerrit
    stub.dev_auth = False
    with pytest.raises(verify_site.CheckError, match="set no session \\(HTTP 404\\)"):
        verify_site.start_session(base)


def test_server_info_matches(gerrit: tuple[StubGerrit, str]) -> None:
    _, base = gerrit
    detail = verify_site.check_server_info(
        base, "DEVELOPMENT_BECOME_ANY_ACCOUNT", ["ssh", "http"]
    )
    assert detail == "auth DEVELOPMENT_BECOME_ANY_ACCOUNT; download schemes http, ssh"


def test_server_info_reports_without_expectations(
    gerrit: tuple[StubGerrit, str],
) -> None:
    stub, base = gerrit
    stub.server_info = {"auth": {"auth_type": "HTTP"}}
    detail = verify_site.check_server_info(base, None, [])
    assert detail == "auth HTTP; download schemes none"


def test_server_info_wrong_auth_type(gerrit: tuple[StubGerrit, str]) -> None:
    _, base = gerrit
    with pytest.raises(
        verify_site.CheckError,
        match="auth type 'DEVELOPMENT_BECOME_ANY_ACCOUNT', expected 'HTTP'",
    ):
        verify_site.check_server_info(base, "HTTP", [])


def test_server_info_missing_download_scheme(
    gerrit: tuple[StubGerrit, str],
) -> None:
    _, base = gerrit
    with pytest.raises(
        verify_site.CheckError,
        match=r"download schemes \['http', 'ssh'\] lack \['anonymous http'\]",
    ):
        verify_site.check_server_info(base, None, ["ssh", "anonymous http"])


def test_parse_args_server_info_options() -> None:
    args = verify_site.parse_args(
        [
            "--namespace",
            "ns",
            "--auth-type",
            "HTTP",
            "--download-scheme",
            "ssh",
            "--download-scheme",
            "http",
        ]
    )
    assert (args.auth_type, args.download_scheme) == ("HTTP", ["ssh", "http"])
    defaults = verify_site.parse_args(["--namespace", "ns"])
    assert (defaults.auth_type, defaults.download_scheme) == (None, [])


def test_marker_project_is_created(gerrit: tuple[StubGerrit, str]) -> None:
    stub, _ = gerrit
    name = verify_site.create_marker(gerrit[1])
    assert name.startswith("verify-site-")
    assert stub.projects == {name: {"description": "verify-site.py persistence marker"}}


@pytest.fixture(scope="module")
def verify_hook() -> ModuleType:
    """Load verify-hook.py, which loads its own copy of verify-site.py."""
    spec = importlib.util.spec_from_file_location(
        "verify_hook", SCRIPT.with_name("verify-hook.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_test_change_is_created_in_a_new_project(
    gerrit: tuple[StubGerrit, str], verify_hook: ModuleType
) -> None:
    stub, base = gerrit
    name = verify_hook.create_test_change(base)
    assert name.startswith("verify-hook-")
    assert stub.projects[name] == {
        "description": "verify-hook.py hook check",
        "create_empty_commit": True,
        "branches": ["main"],
    }
    assert stub.changes == [
        {"project": name, "branch": "main", "subject": "verify-hook.py check"}
    ]


def _fake_kubectl(
    verify_hook: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    log: Callable[[int], str | None],
) -> list[tuple[str, ...]]:
    """Serve ``log(call number)`` as the hook log, None failing; return calls."""
    calls: list[tuple[str, ...]] = []

    def fake_kubectl(namespace: str, *args: str, timeout: float = 120) -> str:
        calls.append((namespace, *args))
        reply = log(len(calls))
        if reply is None:
            raise verify_hook.CheckError("no such file")
        return reply

    monkeypatch.setattr(verify_hook.site, "kubectl", fake_kubectl)
    monkeypatch.setattr(verify_hook.time, "sleep", lambda _: None)
    return calls


def test_hook_check_finds_the_project(
    gerrit: tuple[StubGerrit, str],
    verify_hook: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub, base = gerrit

    def log(call: int) -> str | None:
        if call == 1:
            return None
        project = next(iter(stub.projects))
        return f"--change I1 --project {project} --branch main\n"

    calls = _fake_kubectl(verify_hook, monkeypatch, log)
    project = verify_hook.check_hook(base, "ns", "gerrit-0", "/var/hook.log", 30)
    assert project == next(iter(stub.projects))
    assert len(calls) == 2
    assert calls[0] == (
        "ns",
        "exec",
        "gerrit-0",
        "-c",
        "gerrit",
        "--",
        "cat",
        "/var/hook.log",
    )


def test_hook_check_ignores_other_projects(
    gerrit: tuple[StubGerrit, str],
    verify_hook: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub, base = gerrit

    def log(_: int) -> str:
        project = next(iter(stub.projects))
        return f"--project {project}-other --branch main\n"

    _fake_kubectl(verify_hook, monkeypatch, log)
    with pytest.raises(verify_hook.CheckError, match="no hook call in /var/hook.log"):
        verify_hook.check_hook(base, "ns", "gerrit-0", "/var/hook.log", 0)


def test_hook_parse_args(verify_hook: ModuleType) -> None:
    args = verify_hook.parse_args(["--namespace", "ns", "--hook-log", "/var/h.log"])
    assert (args.hook_log, args.fullname, args.timeout) == ("/var/h.log", "gerrit", 120)
    with pytest.raises(SystemExit):
        verify_hook.parse_args(["--namespace", "ns"])


@pytest.fixture(scope="module")
def verify_health() -> ModuleType:
    """Load verify-health.py, which loads its own copy of verify-site.py."""
    spec = importlib.util.spec_from_file_location(
        "verify_health", SCRIPT.with_name("verify-health.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _check(result: str) -> dict[str, object]:
    return {"result": result, "ts": 1, "elapsed": 2}


@pytest.mark.xfail(strict=True, reason="observability lands in #244")
def test_healthcheck_passes(
    gerrit: tuple[StubGerrit, str], verify_health: ModuleType
) -> None:
    stub, base = gerrit
    stub.healthcheck = (
        200,
        {
            "ts": 1,
            "elapsed": 9,
            "querychanges": _check("passed"),
            "jgit": _check("passed"),
            "auth": _check("disabled"),
        },
    )
    assert verify_health.check_healthcheck(base) == "passed: jgit, querychanges"


@pytest.mark.xfail(strict=True, reason="observability lands in #244")
def test_healthcheck_names_the_failing_checks(
    gerrit: tuple[StubGerrit, str],
    verify_health: ModuleType,
) -> None:
    stub, base = gerrit
    stub.healthcheck = (
        500,
        {
            "jgit": _check("passed"),
            "querychanges": _check("timeout"),
            "projectslist": _check("failed"),
        },
    )
    with pytest.raises(
        verify_health.CheckError,
        match="HTTP 500, failing checks: projectslist failed, querychanges timeout",
    ):
        verify_health.check_healthcheck(base)


@pytest.mark.xfail(strict=True, reason="observability lands in #244")
def test_healthcheck_fails_on_an_error_status_with_passing_checks(
    gerrit: tuple[StubGerrit, str],
    verify_health: ModuleType,
) -> None:
    stub, base = gerrit
    stub.healthcheck = (500, {"jgit": _check("passed")})
    with pytest.raises(verify_health.CheckError, match="HTTP 500$"):
        verify_health.check_healthcheck(base)


@pytest.mark.xfail(strict=True, reason="observability lands in #244")
def test_healthcheck_fails_without_the_plugin(
    gerrit: tuple[StubGerrit, str],
    verify_health: ModuleType,
) -> None:
    _, base = gerrit
    with pytest.raises(verify_health.CheckError, match="HTTP 404"):
        verify_health.check_healthcheck(base)


@pytest.mark.xfail(strict=True, reason="observability lands in #244")
def test_healthcheck_fails_on_a_report_without_checks(
    gerrit: tuple[StubGerrit, str],
    verify_health: ModuleType,
) -> None:
    stub, base = gerrit
    stub.healthcheck = (200, {"ts": 1, "elapsed": 2})
    with pytest.raises(verify_health.CheckError, match="reported no checks"):
        verify_health.check_healthcheck(base)


@pytest.mark.xfail(strict=True, reason="observability lands in #244")
def test_parse_args_healthcheck(verify_health: ModuleType) -> None:
    args = verify_health.parse_args(["--namespace", "ns"])
    assert (args.fullname, args.url_prefix, args.local_port) == ("gerrit", "/r", 18080)
