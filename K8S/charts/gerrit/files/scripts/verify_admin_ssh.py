# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""The SSH checks of ``verify-admin.py``, and the results both halves share.

Each check runs ``gerrit`` commands through the operator's own ``ssh``
client and key, reads nothing but their output, and raises
``CheckError`` on anything it cannot parse rather than passing.
"""

from __future__ import annotations

import collections
import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo

SSH_TIMEOUT = 120
DAY = 24 * 60 * 60

# Gerrit prints a time of day for a task started within the last day,
# and a date with hour and minute for an older one
TIME = r"\d{2}:\d{2}:\d{2}\.\d{3}|[A-Z][a-z]{2}-\d{2} \d{2}:\d{2}"
TASK_ROW = re.compile(
    r"^\s*(?P<id>[0-9a-f]{8})\s+"
    rf"(?:(?P<state>\.+ [a-z]+|[a-z]+ \.+|{TIME})\s+)?"
    rf"(?P<start>{TIME})\s+(?P<command>\S.*)$"
)
QUEUE_HEADER = re.compile(r"^Task\s+State\s+StartTime\s+Command$")
QUEUE_FOOTER = re.compile(r"^(\d+) tasks?\b")
ACTIVE_STATES = ("running", "starting", "ready")
SERVER_NOW = re.compile(r"\bnow\s+(\d{2}):(\d{2}):(\d{2})\b(?:[ \t]+(\S+))?")
# Names Java gives the zone of a clock that reads UTC
UTC_NAMES = ("UTC", "GMT", "Z", "UCT")
SUMMARY_LINES = {
    "Mem": re.compile(
        r"^\s*Mem:\s+(?P<total>\S+) total = (?P<used>\S+) used \+ "
        r"(?P<free>\S+) free",
        re.MULTILINE,
    ),
    "Threads": re.compile(
        r"^\s*Threads:\s+(?P<cpus>\d+) CPUs available, (?P<threads>\d+) threads",
        re.MULTILINE,
    ),
    "SSH": re.compile(r"^\s*SSH:\s+(?P<users>\d+)\s+users", re.MULTILINE),
}
MEM_MAX = re.compile(r"^\s*(?P<max>\S+) max\s*$", re.MULTILINE)


class CheckError(Exception):
    """A check failed; the message is the one-line reason."""

    def __init__(self, reason: str, lines: list[str] | None = None) -> None:
        super().__init__(reason)
        self.lines = lines or []


class Unavailable(Exception):
    """A check this Gerrit cannot answer; the message says why."""


@dataclass
class Outcome:
    """A passed check: a short detail and any lines to list under it."""

    detail: str
    lines: list[str] = field(default_factory=list)


def run_check(failures: list[str], name: str, func: Callable[[], Outcome]) -> bool:
    """Run one check, print its result, and record it if it failed."""
    try:
        outcome = func()
    except Unavailable as exc:
        print(f"skip {name}: unavailable: {exc}", flush=True)
        return True
    except CheckError as exc:
        failures.append(name)
        print(f"FAIL {name}: {exc}", flush=True)
        passed, lines = False, exc.lines
    else:
        print(f"ok   {name} ({outcome.detail})", flush=True)
        passed, lines = True, outcome.lines
    for line in lines:
        print(f"     {line}", flush=True)
    return passed


@dataclass(frozen=True)
class Ssh:
    """Runs ``gerrit`` commands through the operator's ssh client."""

    host: str
    port: int
    user: str | None

    def run(self, *command: str) -> str:
        """Run one gerrit command and return its standard output."""
        name = " ".join(("gerrit", *command))
        cmd = ["ssh", "-o", "BatchMode=yes", "-p", str(self.port)]
        if self.user:
            cmd += ["-l", self.user]
        cmd += ["--", self.host, "gerrit", *command]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=SSH_TIMEOUT, check=False
            )
        except FileNotFoundError as exc:
            raise CheckError("no ssh client on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise CheckError(f"{name} timed out after {SSH_TIMEOUT}s") from exc
        if proc.returncode != 0:
            detail = proc.stderr.strip().splitlines()
            reason = detail[-1] if detail else f"exit code {proc.returncode}"
            raise CheckError(f"{name}: {reason}")
        return proc.stdout


def check_version(ssh: Ssh, expected: str | None) -> Outcome:
    """The version Gerrit reports, equal to the expected one if given."""
    out = ssh.run("version").strip()
    match = re.fullmatch(r"gerrit version (\S+)", out)
    if not match:
        raise CheckError(f"gerrit version: cannot parse {out[:80]!r}")
    version = match.group(1)
    if expected is not None and version != expected:
        raise CheckError(f"version {version}, expected {expected}")
    return Outcome(version)


def check_plugins(ssh: Ssh, expected: list[str]) -> Outcome:
    """Every expected plugin is loaded and enabled; list them all."""
    out = ssh.run("plugin", "ls", "--all", "--format", "JSON")
    try:
        plugins = json.loads(out)
    except json.JSONDecodeError as exc:
        raise CheckError("gerrit plugin ls: output is not JSON") from exc
    if not isinstance(plugins, dict) or not all(
        isinstance(info, dict) for info in plugins.values()
    ):
        raise CheckError("gerrit plugin ls: output is not a map of plugins")
    status = {
        name: "disabled" if info.get("disabled") else "enabled"
        for name, info in plugins.items()
    }
    lines = []
    for name in sorted(status):
        version = plugins[name].get("version") or "no version"
        note = "" if not expected or name in expected else ", not expected"
        lines.append(f"{name} {version}: {status[name]}{note}")
    problems = [
        f"{name} {'is disabled' if name in status else 'is absent'}"
        for name in sorted(set(expected))
        if status.get(name) != "enabled"
    ]
    if problems:
        raise CheckError("; ".join(problems), lines)
    enabled = sum(1 for s in status.values() if s == "enabled")
    return Outcome(f"{len(status)} loaded, {enabled} enabled", lines)


@dataclass(frozen=True)
class Task:
    """One row of ``gerrit show-queue``."""

    id: str
    state: str
    start: str
    command: str

    def ages(self, before: datetime, now: datetime, zone: ZoneInfo) -> list[int] | None:
        """Every age in seconds at ``now`` the start time can mean.

        Gerrit prints the time of day in ``zone`` for a task started
        within a day of printing the queue, at some instant between
        ``before`` and ``now``. Every instant from a day before
        ``before`` to ``now`` that reads that time counts: in a gap the
        clocks skip there may be none, and in an hour they repeat, or
        when the queue was printed well after ``before``, more than
        one. None when Gerrit prints a date, for a task over a day old.
        """
        match = re.fullmatch(r"(\d{2}):(\d{2}):(\d{2})\.\d{3}", self.start)
        if not match:
            return None
        hours, minutes, seconds = (int(g) for g in match.groups())
        clock = time(hours, minutes, seconds)
        # show-caches drops the fraction of its second, show-queue does not
        latest = now + timedelta(seconds=1)
        earliest = before - timedelta(seconds=DAY)
        today = now.astimezone(zone).date()
        ages = []
        for back in (2, 1, 0):
            for instant in wall_instants(today - timedelta(days=back), clock, zone):
                if earliest <= instant <= latest:
                    ages.append(max(0, int((now - instant).total_seconds())))
        return ages


def wall_instants(day: date, clock: time, zone: tzinfo) -> list[datetime]:
    """The instants, in UTC, at which ``zone`` reads ``clock`` on ``day``."""
    wall = datetime.combine(day, clock)
    instants: list[datetime] = []
    for fold in (0, 1):
        instant = wall.replace(tzinfo=zone, fold=fold).astimezone(UTC)
        if (
            instant.astimezone(zone).replace(tzinfo=None, fold=0) == wall
            and instant not in instants
        ):
            instants.append(instant)
    return instants


def task_state(column: str | None) -> str:
    """Name the state show-queue prints in its State column."""
    if column is None:
        return "running"
    if column == "waiting ....":
        return "ready"
    if column == "starting ...":
        return "starting"
    if column.startswith("."):
        return column.split()[-1]
    # A time in the State column is when a sleeping task runs next
    return "sleeping"


def _is_rule(line: str) -> bool:
    return set(line.strip()) == {"-"}


def parse_queue(text: str) -> list[Task]:
    """Parse ``gerrit show-queue --wide``: the whole table, or fail.

    The table is a header, a rule, a row per task, a rule and the task
    count, which must match the rows read.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines or not QUEUE_HEADER.match(lines[0].strip()):
        raise CheckError("gerrit show-queue: no task table header")
    if len(lines) < 4 or not _is_rule(lines[1]) or not _is_rule(lines[-2]):
        raise CheckError("gerrit show-queue: task table is incomplete")
    footer = QUEUE_FOOTER.match(lines[-1].strip())
    if not footer:
        raise CheckError("gerrit show-queue: no task count after the table")
    tasks = []
    for line in lines[2:-2]:
        match = TASK_ROW.match(line)
        if not match:
            raise CheckError(f"gerrit show-queue: cannot parse {line.strip()[:80]!r}")
        tasks.append(
            Task(
                match["id"],
                task_state(match["state"]),
                match["start"],
                match["command"].strip(),
            )
        )
    if int(footer[1]) != len(tasks):
        raise CheckError(
            f"gerrit show-queue: {len(tasks)} rows, but a count of {footer[1]}"
        )
    return tasks


def server_now(text: str, zone: ZoneInfo, today: datetime | None = None) -> datetime:
    """The server's clock, as the instant ``gerrit show-caches`` shows.

    show-caches gives a time of day and names its zone. A clock in UTC
    is read as UTC. A clock in another zone is the JVM's, and must be a
    name ``zone``, the zone the operator says the JVM runs in, uses at
    that instant. Of the instants that fit, the one nearest ``today``,
    the workstation's time, is the server's.
    """
    match = SERVER_NOW.search(text)
    if not match:
        raise CheckError("gerrit show-caches: no server time in its header")
    hours, minutes, seconds = (int(g) for g in match.groups()[:3])
    clock = time(hours, minutes, seconds)
    label = match[4]
    today = today or datetime.now(UTC)
    in_utc = label is None or label in UTC_NAMES
    clock_zone: tzinfo = UTC if in_utc else zone
    local = today.astimezone(clock_zone).date()
    instants = [
        instant
        for day in (-1, 0, 1)
        for instant in wall_instants(local + timedelta(days=day), clock, clock_zone)
        if in_utc or instant.astimezone(zone).tzname() == label
    ]
    if not instants:
        raise CheckError(
            f"gerrit show-caches: its clock reads {label}, which --server-timezone "
            f"{zone.key} does not use; name the zone Gerrit's JVM runs in"
        )
    return min(instants, key=lambda instant: abs(instant - today))


def check_tasks(
    ssh: Ssh, minutes: int, zone: ZoneInfo, today: datetime | None = None
) -> Outcome:
    """Count the queue by state; fail on a task active for too long.

    show-queue gives a task's StartTime, when Gerrit queued it, and no
    time it began running or became ready, so a task's age counts from
    when it was queued: a recurring task that has just woken can fail.
    The server's clock is read before and after the queue, which
    Gerrit printed somewhere between. A start time whose possible
    instants fall on both sides of the limit, or that names no instant
    in that window, fails too.
    """
    before = server_now(ssh.run("show-caches"), zone, today)
    tasks = parse_queue(ssh.run("show-queue", "--wide"))
    now = server_now(ssh.run("show-caches"), zone, today)
    if now < before:
        raise CheckError("gerrit show-caches: the server's clock went back")
    counts = collections.Counter(task.state for task in tasks)
    detail = f"{len(tasks)} tasks"
    if counts:
        detail += ": " + ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))
    limit = minutes * 60
    stale, unplaced = [], []
    for task in tasks:
        if task.state not in ACTIVE_STATES:
            continue
        ages = task.ages(before, now, zone)
        row = f"{task.id} {task.state} since {task.start}: {task.command}"
        if ages is None or (ages and min(ages) > limit):
            stale.append(row)
        elif not ages or max(ages) > limit:
            unplaced.append(f"{row} (start time not placeable in {zone.key})")
    reasons = []
    if stale:
        reasons.append(
            f"{len(stale)} task(s) running or ready, queued over {minutes} minutes ago"
        )
    if unplaced:
        reasons.append(
            f"{len(unplaced)} task(s) running or ready, queued at a time that "
            f"cannot be placed against {minutes} minutes"
        )
    if reasons:
        raise CheckError("; ".join(reasons), stale + unplaced)
    return Outcome(detail)


def check_summary(ssh: Ssh) -> Outcome:
    """Memory, threads and SSH users, as ``gerrit show-caches`` reports."""
    out = ssh.run("show-caches")
    found = {}
    for label, pattern in SUMMARY_LINES.items():
        match = pattern.search(out)
        if not match:
            raise CheckError(f"gerrit show-caches: no {label} line")
        found[label] = match
    mem, threads, users = found["Mem"], found["Threads"], found["SSH"]
    memory = f"memory {mem['used']} used, {mem['free']} free of {mem['total']}"
    if limit := MEM_MAX.search(out):
        memory += f", {limit['max']} max"
    return Outcome(
        f"{memory}; {threads['threads']} threads on {threads['cpus']} CPUs; "
        f"{users['users']} SSH users"
    )
