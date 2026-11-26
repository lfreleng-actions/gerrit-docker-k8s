# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for verify-admin.py, against a stand-in ssh and a stub Gerrit."""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

SCRIPT = (
    Path(__file__).parent.parent
    / "K8S"
    / "charts"
    / "gerrit"
    / "files"
    / "scripts"
    / "verify-admin.py"
)
_spec = importlib.util.spec_from_file_location("verify_admin", SCRIPT)
assert _spec is not None and _spec.loader is not None
verify_admin = importlib.util.module_from_spec(_spec)
sys.modules["verify_admin"] = verify_admin
_spec.loader.exec_module(verify_admin)
verify_admin_ssh = sys.modules["verify_admin_ssh"]

USER = "admin"
PASSWORD = "pw-never-shown-4711"

# Run through the test's own interpreter; replies come from a JSON file
# keyed by the remote command, a list of stdouts answering successive
# calls in turn, and every argv is logged
FAKE_SSH = """\
import json, os, sys
argv = sys.argv[1:]
with open(os.environ["FAKE_SSH_LOG"], "a") as log:
    log.write(json.dumps(argv) + "\\n")
with open(os.environ["FAKE_SSH_LOG"]) as log:
    seen = [json.loads(line) for line in log]
command = " ".join(argv[argv.index("--") + 2 :])
with open(os.environ["FAKE_SSH_REPLIES"]) as replies:
    reply = json.load(replies).get(command)
if reply is None:
    sys.stderr.write("fatal: " + command + ": not found\\n")
    sys.exit(1)
stdout = reply.get("stdout", "")
if isinstance(stdout, list):
    stdout = stdout[min(seen.count(argv), len(stdout)) - 1]
sys.stdout.write(stdout)
sys.stderr.write(reply.get("stderr", ""))
sys.exit(reply.get("code", 0))
"""

QUEUE = """\
Task     State        StartTime         Command
------------------------------------------------------------------------------
1a2b3c4d              11:58:02.120      git-upload-pack '/All-Projects' (admin)
2b3c4d5e waiting .... 11:59:30.004      git-receive-pack '/demo' (alice)
3c4d5e6f 03:00:00.000 Nov-25 03:00      Log File Compressor
------------------------------------------------------------------------------
  3 tasks
"""

CACHES = """\
Gerrit Code Review        3.10.1                    now    12:00:10   UTC
                                                 uptime    2 days 3 hrs

  Name                          |Entries              |  AvgGet |Hit Ratio|
--------------------------------+---------------------+---------+---------+
  accounts                      |     2               |   3.1ms | 98%     |

SSH:      2  users, oldest session started   4 min ago
Tasks:    3  total =    1 running +      1 ready +    1 sleeping
Mem: 1.85g total = 1.34g used + 509.11m free + 0.00k buffers
     3.00g max
        1024 open files

Threads: 4 CPUs available, 371 threads
"""


def _table(*rows: str, count: int | None = None) -> str:
    """A show-queue table holding the rows, with their count or another."""
    rule = "-" * 78
    body = "".join(f"{row}\n" for row in rows)
    total = len(rows) if count is None else count
    return (
        f"Task     State        StartTime         Command\n{rule}\n"
        f"{body}{rule}\n  {total} tasks\n"
    )


INDEXES = {
    "accounts": {
        "name": "accounts",
        "versions": {"13": {"is_write": True, "is_search": True}},
    },
    "changes": {
        "name": "changes",
        "versions": {
            "84": {"is_write": True, "is_search": True},
            "85": {"is_write": True, "is_search": False},
        },
    },
}

PLUGINS = {
    "replication": {"id": "replication", "version": "v3.10.1"},
    "healthcheck": {"id": "healthcheck", "version": "v3.10.0"},
    "gitiles": {"id": "gitiles", "version": "v3.10.1", "disabled": True},
}


def _replies(
    overrides: dict[str, dict[str, object]] | None = None,
) -> dict[str, dict[str, object]]:
    """Each command's reply, keyed without the leading ``gerrit``."""
    replies: dict[str, dict[str, object]] = {
        "gerrit version": {"stdout": "gerrit version 3.10.1\n"},
        "gerrit plugin ls --all --format JSON": {"stdout": json.dumps(PLUGINS)},
        "gerrit show-queue --wide": {"stdout": QUEUE},
        "gerrit show-caches": {"stdout": CACHES},
    }
    for key, value in (overrides or {}).items():
        replies[f"gerrit {key}"] = value
    return replies


@dataclass
class FakeSsh:
    """The stand-in ssh: what it answers and the calls it saw."""

    replies_file: Path
    log_file: Path

    def answer(self, replies: dict[str, dict[str, object]]) -> None:
        self.replies_file.write_text(json.dumps(replies))

    def calls(self) -> list[list[str]]:
        if not self.log_file.exists():
            return []
        return [json.loads(line) for line in self.log_file.read_text().splitlines()]


@pytest.fixture
def fake_ssh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeSsh:
    """Put a stand-in ssh first on PATH, created here as git cannot carry it."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    ssh = bindir / "ssh"
    ssh.write_text(f"#!{sys.executable}\n{FAKE_SSH}")
    ssh.chmod(0o755)
    fake = FakeSsh(tmp_path / "replies.json", tmp_path / "ssh.log")
    fake.answer(_replies())
    monkeypatch.setenv("PATH", f"{bindir}:{sys.exec_prefix}/bin:/usr/bin:/bin")
    monkeypatch.setenv("FAKE_SSH_REPLIES", str(fake.replies_file))
    monkeypatch.setenv("FAKE_SSH_LOG", str(fake.log_file))
    return fake


@dataclass
class StubGerrit:
    """What the stub REST API serves, and the requests it saw."""

    capabilities: dict[str, object] = field(
        default_factory=lambda: {"administrateServer": True, "viewCaches": True}
    )
    consistency: dict[str, object] = field(
        default_factory=lambda: {
            "check_accounts_result": {},
            "check_account_external_ids_result": {"problems": []},
            "check_groups_result": {},
        }
    )
    indexes: object = field(default_factory=lambda: json.loads(json.dumps(INDEXES)))
    requests: list[tuple[str, str, object]] = field(default_factory=list)


def _handler(stub: StubGerrit) -> type[BaseHTTPRequestHandler]:
    expected = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def _reply(self, code: int, payload: object = None) -> None:
            body = b"" if payload is None else b")]}'\n" + json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _route(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            stub.requests.append((method, self.path, body))
            if self.headers.get("Authorization") != expected:
                self._reply(401)
            elif method == "GET" and self.path == "/r/a/accounts/self/capabilities":
                self._reply(200, stub.capabilities)
            elif (
                method == "POST" and self.path == "/r/a/config/server/check.consistency"
            ):
                if stub.capabilities.get("administrateServer") is not True:
                    self._reply(403)
                else:
                    self._reply(200, stub.consistency)
            elif method == "GET" and self.path == "/r/a/config/server/indexes/":
                if stub.indexes is None:
                    self._reply(404)
                else:
                    self._reply(200, stub.indexes)
            else:
                self._reply(404)

        def do_GET(self) -> None:
            self._route("GET")

        def do_POST(self) -> None:
            self._route("POST")

    return Handler


@pytest.fixture
def gerrit() -> Iterator[tuple[StubGerrit, str]]:
    """Serve the stub on loopback; yield it with its base URL."""
    stub = StubGerrit()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(stub))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield stub, f"http://127.0.0.1:{server.server_port}/r"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def netrc_file(tmp_path: Path) -> Path:
    """A netrc entry stored under the site's public name, not the URL's."""
    path = tmp_path / "netrc"
    path.write_text(
        f"machine gerrit.example.org login {USER} password {PASSWORD}\n"
        "machine other.example.org login someone password other-pw\n"
    )
    path.chmod(0o600)
    return path


SSH_ARGS = ["--ssh-host", "gerrit.example.org", "--ssh-user", USER]


def _run(capsys: pytest.CaptureFixture[str], *args: str) -> tuple[int, str]:
    code = verify_admin.main([*SSH_ARGS, *args])
    out, err = capsys.readouterr()
    return code, out + err


def _rest(base: str, netrc_file: Path) -> list[str]:
    return [
        "--url",
        base,
        "--netrc-machine",
        "gerrit.example.org",
        "--netrc-file",
        str(netrc_file),
    ]


def test_clean_instance_passes(
    fake_ssh: FakeSsh,
    gerrit: tuple[StubGerrit, str],
    netrc_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stub, base = gerrit
    code, out = _run(
        capsys,
        "--expect-version",
        "3.10.1",
        "--expect-plugin",
        "replication",
        "--expect-plugin",
        "healthcheck",
        *_rest(base, netrc_file),
    )
    assert code == 0, out
    assert "ok   version (3.10.1)" in out
    assert "ok   plugins (3 loaded, 2 enabled)" in out
    assert "gitiles v3.10.1: disabled, not expected" in out
    assert "ok   task queue (3 tasks: 1 ready, 1 running, 1 sleeping)" in out
    assert (
        "ok   summary (memory 1.34g used, 509.11m free of 1.85g, 3.00g max; "
        "371 threads on 4 CPUs; 2 SSH users)"
    ) in out
    assert "ok   administrateServer (granted)" in out
    assert (
        "ok   index versions (2 indexes; Gerrit reports no readiness beyond "
        "search and write)"
    ) in out
    assert "     changes 84: search, write" in out
    assert "     changes 85: write" in out
    assert "All admin checks passed" in out
    assert fake_ssh.calls()[0] == [
        "-o",
        "BatchMode=yes",
        "-p",
        "29418",
        "-l",
        USER,
        "--",
        "gerrit.example.org",
        "gerrit",
        "version",
    ]
    assert [(m, p) for m, p, _ in stub.requests] == [
        ("GET", "/r/a/accounts/self/capabilities"),
        ("POST", "/r/a/config/server/check.consistency"),
        ("GET", "/r/a/config/server/indexes/"),
    ]
    assert stub.requests[1][2] == {
        "check_accounts": {},
        "check_account_external_ids": {},
        "check_groups": {},
    }


@pytest.mark.parametrize(
    ("plugin", "reason"),
    [("gitiles", "gitiles is disabled"), ("webhooks", "webhooks is absent")],
)
def test_expected_plugin_disabled_or_absent_fails(
    fake_ssh: FakeSsh,
    capsys: pytest.CaptureFixture[str],
    plugin: str,
    reason: str,
) -> None:
    code, out = _run(
        capsys, "--expect-plugin", "replication", "--expect-plugin", plugin
    )
    assert code == 1
    assert f"FAIL plugins: {reason}" in out


def test_other_version_fails(
    fake_ssh: FakeSsh, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = _run(capsys, "--expect-version", "3.11.0")
    assert code == 1
    assert "FAIL version: version 3.10.1, expected 3.11.0" in out


@pytest.mark.parametrize(
    ("status", "code"), [("ERROR", 1), ("FATAL", 1), ("WARNING", 0)]
)
def test_consistency_fails_on_errors_only(
    fake_ssh: FakeSsh,
    gerrit: tuple[StubGerrit, str],
    netrc_file: Path,
    capsys: pytest.CaptureFixture[str],
    status: str,
    code: int,
) -> None:
    stub, base = gerrit
    stub.consistency["check_groups_result"] = {
        "problems": [{"status": status, "message": "group 7 has no owner"}]
    }
    result, out = _run(capsys, *_rest(base, netrc_file))
    assert result == code, out
    assert f"     {status} groups: group 7 has no owner" in out
    if code:
        assert "FAIL consistency of accounts, external IDs and groups: " in out


def test_failing_ssh_fails(
    fake_ssh: FakeSsh, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_ssh.answer(
        _replies(
            {"version": {"code": 255, "stderr": "Permission denied (publickey).\n"}}
        )
    )
    code, out = _run(capsys)
    assert code == 1
    assert "FAIL version: gerrit version: Permission denied (publickey)." in out


@pytest.mark.parametrize(
    ("command", "stdout", "reason"),
    [
        ("version", "Gerrit 3\n", "gerrit version: cannot parse"),
        ("plugin ls --all --format JSON", "Name Version\n", "output is not JSON"),
        ("show-queue --wide", _table("1a2b3c4d garbled"), "show-queue: cannot parse"),
        ("show-caches", "nothing here\n", "gerrit show-caches: no "),
    ],
)
def test_unparsable_output_fails(
    fake_ssh: FakeSsh,
    capsys: pytest.CaptureFixture[str],
    command: str,
    stdout: str,
    reason: str,
) -> None:
    fake_ssh.answer(_replies({command: {"stdout": stdout}}))
    code, out = _run(capsys)
    assert code == 1
    assert reason in out
    assert "All admin checks passed" not in out


def test_task_over_threshold_fails(
    fake_ssh: FakeSsh, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = _run(capsys, "--task-minutes", "1")
    assert code == 1
    assert (
        "FAIL task queue: 1 task(s) running or ready, queued over 1 minutes ago" in out
    )
    assert "1a2b3c4d running since 11:58:02.120" in out


def test_task_ages_across_midnight_and_days(
    fake_ssh: FakeSsh, capsys: pytest.CaptureFixture[str]
) -> None:
    queue = _table(
        "1a2b3c4d              23:55:00.000      gc demo",
        "2b3c4d5e              00:00:05.400      git-upload-pack '/demo'",
    )
    caches = CACHES.replace("12:00:10", "00:00:06")
    fake_ssh.answer(
        _replies(
            {"show-caches": {"stdout": caches}, "show-queue --wide": {"stdout": queue}}
        )
    )
    code, out = _run(capsys)
    assert code == 0, out
    old = queue.replace("23:55:00.000", "Nov-24 23:55")
    fake_ssh.answer(
        _replies(
            {"show-caches": {"stdout": caches}, "show-queue --wide": {"stdout": old}}
        )
    )
    code, out = _run(capsys)
    assert code == 1
    assert "1a2b3c4d running since Nov-24 23:55: gc demo" in out


def test_task_just_under_a_day_old_fails(
    fake_ssh: FakeSsh, capsys: pytest.CaptureFixture[str]
) -> None:
    # 12:00:30 yesterday, read at 12:00:10 today: 23h59m40s old
    queue = _table("1a2b3c4d              12:00:30.000      gc demo")
    fake_ssh.answer(_replies({"show-queue --wide": {"stdout": queue}}))
    code, out = _run(capsys, "--task-minutes", "1")
    assert code == 1
    assert "1a2b3c4d running since 12:00:30.000: gc demo" in out


@pytest.mark.parametrize(("start", "code"), [("12:00:30.000", 1), ("11:59:00.000", 0)])
def test_queue_printed_between_clock_reads(
    fake_ssh: FakeSsh, capsys: pytest.CaptureFixture[str], start: str, code: int
) -> None:
    # The clock reads 12:00:00 before show-queue and 12:01:10 after it.
    # 12:00:30 may be yesterday's, printed time-only at 12:00:00 and now
    # over a day old, or today's, 40 seconds old: it cannot be placed
    queue = _table(f"1a2b3c4d              {start}      gc demo")
    clocks = [CACHES.replace("12:00:10", t) for t in ("12:00:00", "12:01:10")]
    fake_ssh.answer(
        _replies(
            {"show-caches": {"stdout": clocks}, "show-queue --wide": {"stdout": queue}}
        )
    )
    result, out = _run(capsys)
    assert result == code, out
    if code:
        assert "queued at a time that cannot be placed against 30 minutes" in out
        assert "1a2b3c4d running since 12:00:30.000: gc demo" in out


def _check_tasks(
    fake_ssh: FakeSsh, start: str, clock: str, today: str, minutes: int, zone: str
) -> str:
    """Run the queue check on one running task; its detail or FAIL reason."""
    queue = _table(f"1a2b3c4d              {start}      gc demo")
    caches = CACHES.replace("12:00:10   UTC", clock)
    fake_ssh.answer(
        _replies(
            {"show-caches": {"stdout": caches}, "show-queue --wide": {"stdout": queue}}
        )
    )
    ssh = verify_admin_ssh.Ssh("gerrit.example.org", 29418, USER)
    instant = datetime.fromisoformat(today).replace(tzinfo=UTC)
    try:
        outcome = verify_admin_ssh.check_tasks(ssh, minutes, ZoneInfo(zone), instant)
        return str(outcome.detail)
    except verify_admin_ssh.CheckError as exc:
        return f"FAIL {exc}"


@pytest.mark.parametrize(
    ("start", "clock", "today", "minutes", "expected"),
    [
        # Europe/Berlin leaves CEST at 01:00 UTC on 2026-10-25: queued at
        # 01:45 CEST and read at 02:10 CET, the task is 85 minutes old
        ("01:45:00.000", "01:10:00   UTC", "2026-10-25T01:10:30", 30, "queued over"),
        ("01:45:00.000", "02:10:00   CET", "2026-10-25T01:10:30", 30, "queued over"),
        ("01:45:00.000", "02:10:00   CET", "2026-10-25T01:10:30", 90, "1 tasks"),
        # 02:30 happens twice that night, 70 or 130 minutes before 02:40 UTC
        ("02:30:00.000", "02:40:00   UTC", "2026-10-25T02:40:00", 30, "queued over"),
        ("02:30:00.000", "02:40:00   UTC", "2026-10-25T02:40:00", 90, "cannot be"),
        ("02:30:00.000", "02:40:00   UTC", "2026-10-25T02:40:00", 180, "1 tasks"),
        # and it enters CEST at 01:00 UTC on 2026-03-29: queued at 01:50
        # CET and read at 03:10 CEST, the task is 20 minutes old
        ("01:50:00.000", "01:10:00   UTC", "2026-03-29T01:10:30", 30, "1 tasks"),
        ("01:50:00.000", "03:10:00   CEST", "2026-03-29T01:10:30", 30, "1 tasks"),
        ("01:50:00.000", "03:10:00   CEST", "2026-03-29T01:10:30", 10, "queued over"),
        # 02:30 never happens that night, nor less than a day earlier
        ("02:30:00.000", "01:40:00   UTC", "2026-03-29T01:40:00", 30, "cannot be"),
    ],
)
def test_task_ages_across_daylight_saving_changes(
    fake_ssh: FakeSsh, start: str, clock: str, today: str, minutes: int, expected: str
) -> None:
    result = _check_tasks(fake_ssh, start, clock, today, minutes, "Europe/Berlin")
    assert expected in result
    assert result.startswith("FAIL") == (expected != "1 tasks")


@pytest.mark.parametrize(
    ("clock", "expected"),
    [
        ("02:30:00   CEST", "2026-10-25T00:30:00"),
        ("02:30:00   CET", "2026-10-25T01:30:00"),
    ],
)
def test_clock_zone_name_picks_the_repeated_hour(clock: str, expected: str) -> None:
    caches = CACHES.replace("12:00:10   UTC", clock)
    today = datetime(2026, 10, 25, 1, 0, tzinfo=UTC)
    now = verify_admin_ssh.server_now(caches, ZoneInfo("Europe/Berlin"), today)
    assert now == datetime.fromisoformat(expected).replace(tzinfo=UTC)


@pytest.mark.parametrize(
    ("stdout", "reason"),
    [
        ("", "no task table header"),
        (_table().split("\n", 1)[0] + "\n", "task table is incomplete"),
        (
            _table("1a2b3c4d              11:58:02.120      gc").rsplit("-" * 78, 1)[0],
            "task table is incomplete",
        ),
        (_table("1a2b3c4d              11:58:02.120      gc", count=2), "1 rows, but"),
        (_table().replace("  0 tasks", "  done"), "no task count after the table"),
    ],
)
def test_missing_or_truncated_queue_fails(
    fake_ssh: FakeSsh, capsys: pytest.CaptureFixture[str], stdout: str, reason: str
) -> None:
    fake_ssh.answer(_replies({"show-queue --wide": {"stdout": stdout}}))
    code, out = _run(capsys)
    assert code == 1
    assert f"FAIL task queue: gerrit show-queue: {reason}" in out


def test_empty_queue_passes(
    fake_ssh: FakeSsh, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_ssh.answer(_replies({"show-queue --wide": {"stdout": _table()}}))
    code, out = _run(capsys)
    assert code == 0, out
    assert "ok   task queue (0 tasks)" in out


@pytest.mark.parametrize(
    ("start", "code"),
    [("13:58:02.120", 0), ("11:55:00.000", 1), ("09:50:00.000", 1)],
)
def test_queue_read_in_server_timezone_against_utc_clock(
    fake_ssh: FakeSsh, capsys: pytest.CaptureFixture[str], start: str, code: int
) -> None:
    # show-caches reads 12:00:10 UTC; the JVM, and show-queue, run at UTC+2
    queue = _table(f"1a2b3c4d              {start}      gc demo")
    fake_ssh.answer(_replies({"show-queue --wide": {"stdout": queue}}))
    result, out = _run(capsys, "--server-timezone", "Etc/GMT-2")
    assert result == code, out


@pytest.mark.parametrize(
    ("zone", "today", "expected"),
    [
        ("Europe/Berlin", "2026-07-01T12:00:30", "1 tasks"),
        ("Etc/GMT+5", "2026-07-01T12:00:30", "CEST, which --server-timezone Etc/GMT+5"),
        ("Europe/Berlin", "2026-11-26T13:00:30", "CEST, which --server-timezone"),
    ],
)
def test_clock_in_jvm_zone_must_be_server_timezone(
    fake_ssh: FakeSsh, zone: str, today: str, expected: str
) -> None:
    # A clock in a zone other than UTC is the JVM's, so it must be one
    # --server-timezone uses at that instant
    result = _check_tasks(fake_ssh, "13:58:02.120", "14:00:10   CEST", today, 30, zone)
    assert expected in result


@pytest.mark.parametrize("problems", [{}, "", False, 0, None])
def test_malformed_consistency_problems_fail(
    fake_ssh: FakeSsh,
    gerrit: tuple[StubGerrit, str],
    netrc_file: Path,
    capsys: pytest.CaptureFixture[str],
    problems: object,
) -> None:
    stub, base = gerrit
    stub.consistency["check_groups_result"] = {"problems": problems}
    code, out = _run(capsys, *_rest(base, netrc_file))
    assert code == 1
    assert (
        "FAIL consistency of accounts, external IDs and groups: "
        "check.consistency: no readable check_groups_result"
    ) in out


def test_index_listing_absent_is_unavailable(
    fake_ssh: FakeSsh,
    gerrit: tuple[StubGerrit, str],
    netrc_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stub, base = gerrit
    stub.indexes = None
    code, out = _run(capsys, *_rest(base, netrc_file))
    assert code == 0, out
    assert (
        "skip index versions: unavailable: this Gerrit has no /a/config/server/indexes/"
    ) in out


@pytest.mark.parametrize(
    "indexes",
    [
        [],
        {"changes": {"name": "changes"}},
        {"changes": {"versions": {"84": {}}}},
        {"changes": {"name": "changes", "versions": {"84": {"is_search": "yes"}}}},
        # The flags under another name than Gerrit's
        {"changes": {"name": "changes", "versions": {"84": {"search": True}}}},
    ],
)
def test_unreadable_index_listing_fails(
    fake_ssh: FakeSsh,
    gerrit: tuple[StubGerrit, str],
    netrc_file: Path,
    capsys: pytest.CaptureFixture[str],
    indexes: object,
) -> None:
    stub, base = gerrit
    stub.indexes = indexes
    code, out = _run(capsys, *_rest(base, netrc_file))
    assert code == 1
    assert "FAIL index versions: GET /a/config/server/indexes/: " in out


def test_index_versions_skipped_without_credential(
    fake_ssh: FakeSsh, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = _run(capsys)
    assert code == 0, out
    assert "skip index versions: needs --url and a credential" in out


def test_http_401_fails(
    fake_ssh: FakeSsh,
    gerrit: tuple[StubGerrit, str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, base = gerrit
    wrong = tmp_path / "password"
    wrong.write_text("not-the-password\n")
    code, out = _run(
        capsys, "--url", base, "--password-file", str(wrong), "--http-user", USER
    )
    assert code == 1
    assert "FAIL administrateServer: GET /a/accounts/self/capabilities: HTTP 401" in out
    assert "consistency" not in out


def test_password_file_passes(
    fake_ssh: FakeSsh,
    gerrit: tuple[StubGerrit, str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, base = gerrit
    secret = tmp_path / "password"
    secret.write_text(f"{PASSWORD}\n")
    code, out = _run(
        capsys, "--url", base, "--password-file", str(secret), "--http-user", USER
    )
    assert code == 0, out


def test_account_without_administrate_server_fails(
    fake_ssh: FakeSsh,
    gerrit: tuple[StubGerrit, str],
    netrc_file: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stub, base = gerrit
    stub.capabilities = {"viewCaches": True}
    code, out = _run(capsys, *_rest(base, netrc_file))
    assert code == 1
    assert (
        "FAIL administrateServer: the credential's account lacks "
        "administrateServer, so the consistency check was not run"
    ) in out
    assert [p for _, p, _ in stub.requests] == ["/r/a/accounts/self/capabilities"]


def test_credential_never_printed(
    fake_ssh: FakeSsh,
    gerrit: tuple[StubGerrit, str],
    netrc_file: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stub, base = gerrit
    outputs = []
    _, out = _run(capsys, *_rest(base, netrc_file))
    outputs.append(out)
    stub.capabilities = {}
    _, out = _run(capsys, *_rest(base, netrc_file))
    outputs.append(out)
    stub.capabilities = {"administrateServer": True}
    stub.consistency = {"check_accounts_result": "garbled"}
    _, out = _run(capsys, *_rest(base, netrc_file))
    outputs.append(out)
    # A malformed entry, where the parser would name the password token
    broken = tmp_path / "broken-netrc"
    broken.write_text(f"machine gerrit.example.org login {USER} {PASSWORD}\n")
    _, out = _run(
        capsys,
        "--url",
        base,
        "--netrc-machine",
        "gerrit.example.org",
        "--netrc-file",
        str(broken),
    )
    outputs.append(out)
    assert "FAIL administrateServer: cannot parse" in out
    # A password refused by the server
    wrong_user = tmp_path / "password"
    wrong_user.write_text(PASSWORD)
    _, out = _run(
        capsys, "--url", base, "--password-file", str(wrong_user), "--http-user", "x"
    )
    outputs.append(out)
    assert "HTTP 401" in out
    for out in outputs:
        assert PASSWORD not in out
        assert base64.b64encode(PASSWORD.encode()).decode() not in out


@pytest.mark.parametrize(
    "url",
    ["http://gerrit.example.org/r", "http://10.0.0.5:8080/r", "http://[::2]/r"],
)
def test_plain_http_to_other_hosts_refused_before_any_request(
    fake_ssh: FakeSsh,
    netrc_file: Path,
    capsys: pytest.CaptureFixture[str],
    url: str,
) -> None:
    with pytest.raises(SystemExit) as exc:
        verify_admin.main([*SSH_ARGS, *_rest(url, netrc_file)])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "refusing to send a credential over plain HTTP" in err
    assert fake_ssh.calls() == []


@pytest.mark.parametrize(
    "url",
    [
        "https://gerrit.example.org/r",
        "http://127.0.0.1:8080/r",
        "http://localhost:8080",
        "http://[::1]:8080/r",
    ],
)
def test_https_and_loopback_urls_accepted(url: str) -> None:
    assert verify_admin.url_problem(url) is None


@pytest.mark.parametrize(
    "args",
    [
        ["--url", "https://gerrit.example.org"],
        ["--netrc-machine", "gerrit.example.org"],
        ["--url", "https://gerrit.example.org", "--password-file", "pw"],
        # Built from parts, so the source holds no URL with inline credentials
        [
            "--url",
            "https://u" + ":" + "p" + "@gerrit.example.org",
            "--netrc-machine",
            "m",
        ],
        ["--task-minutes", "1440"],
        ["--server-timezone", "Not/AZone"],
        ["--url", "http://[::1", "--netrc-machine", "m"],
        ["--url", "https://gerrit.example.org:port", "--netrc-machine", "m"],
    ],
)
def test_parse_args_rejects_incomplete_options(args: list[str]) -> None:
    with pytest.raises(SystemExit):
        verify_admin.parse_args([*SSH_ARGS, *args])
