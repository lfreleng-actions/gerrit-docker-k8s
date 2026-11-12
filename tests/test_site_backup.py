# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for the chart's backup CronJob script, ``site-backup.py``.

The script runs the real export tool's ``backup`` mode and uploads the
result with rclone. The tests run it against a fake ``rclone`` that
serves a directory as the bucket, so a backup can be restored with
site-import from ``s3://<bucket>/<prefix>/latest``.
"""

from __future__ import annotations

import datetime
import fcntl
import importlib.util
import json
import os
import signal
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import site_backup_retention as retention
import test_site_import
from test_site_import import EXPORT, ROOT, needs_tools, run_main, state

SCRIPT = ROOT / "K8S" / "charts" / "gerrit" / "files" / "scripts" / "site-backup.py"
URL = "s3://gerrit-backups/gerrit"
SECRET = "placeholder-secret"

# Hourly from 5 January 00:00 to 6 January 06:00, as (day, hour)
HOURLY = [(5, h) for h in range(24)] + [(6, h) for h in range(7)]

source = test_site_import.source
pod = test_site_import.pod

# rclone, as the scripts call it, on a bucket directory: <remote>:<path>
# names $FAKE_BUCKET/<path>. $FAKE_FAIL fails the subcommand it names.
FAKE_RCLONE = """\
import filecmp, os, shutil, sys
from pathlib import Path

bucket = Path(os.environ["FAKE_BUCKET"])
with open(os.environ["FAKE_LOG"], "a") as log:
    print(" ".join(sys.argv[1:]), file=log)
    print(" ".join(sorted(k for k in os.environ if k.startswith("RCLONE_"))), file=log)
    print("AWS_SECRET_ACCESS_KEY" in os.environ, file=log)
command, args, files_from = sys.argv[1], [], None
rest = iter(sys.argv[2:])
for arg in rest:
    if arg in ("--stats", "--retries", "--retries-sleep", "--low-level-retries",
               "--max-depth"):
        next(rest)
    elif arg == "--files-from":
        files_from = next(rest)
    elif not arg.startswith("--"):
        args.append(arg)
if command == os.environ.get("FAKE_FAIL"):
    sys.exit(3)
def path(arg):
    remote, sep, rel = arg.partition(":")
    return bucket / rel if sep and not remote.startswith("/") else Path(arg)
if command == "copyto":
    dest = path(args[1])
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path(args[0]), dest)
elif command == "check":
    names = Path(files_from).read_text().split()
    if not all(filecmp.cmp(path(args[0]) / n, path(args[1]) / n, False)
               for n in names):
        sys.exit(1)
elif command == "lsf":
    top = path(args[0])
    for entry in sorted(top.iterdir()):
        print(entry.name + ("/" if entry.is_dir() else ""))
        for inner in sorted(entry.iterdir()) if entry.is_dir() else []:
            print(f"{entry.name}/{inner.name}")
elif command == "cat":
    print(path(args[0]).read_text(), end="")
elif command == "purge":
    shutil.rmtree(path(args[0]))
else:
    sys.exit(f"fake rclone: unexpected {command}")
"""


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("site_backup", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["site_backup"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def world(tmp_path: Path, source: Path) -> dict[str, Any]:
    """A backup pod: the site, scratch, a bucket and a fake rclone."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = tmp_path / "fake_rclone.py"
    fake.write_text(FAKE_RCLONE)
    rclone = bin_dir / "rclone"
    rclone.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "$@"\n')
    rclone.chmod(0o755)
    bucket = tmp_path / "bucket"
    bucket.mkdir()
    set_state(source, "complete")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    log = tmp_path / "rclone.log"
    lock = source / ".chart" / "backup-lock" / "lock"
    lock.parent.mkdir(mode=0o700)
    lock.touch(mode=0o600)
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "FAKE_BUCKET": str(bucket),
        "FAKE_LOG": str(log),
        "SITE_BACKUP_URL": URL,
        "SITE_BACKUP_S3_PROVIDER": "Minio",
        "SITE_BACKUP_S3_ENDPOINT": "http://store.example:9000",
        "SITE_BACKUP_KEEP_DAILY": "2",
        "SITE_BACKUP_KEEP_WEEKLY": "2",
        "SITE_BACKUP_MIN_AGE_HOURS": "1",
        "SITE_BACKUP_MAX_RUN_HOURS": "6",
        "SITE_BACKUP_SITE": str(source),
        "SITE_BACKUP_SCRATCH": str(scratch),
        "SITE_BACKUP_EXPORTER": str(EXPORT),
        "AWS_ACCESS_KEY_ID": "AKIDEXAMPLE",
        "AWS_SECRET_ACCESS_KEY": SECRET,
        "RCLONE_HEADER": "X: y",
    }
    return {
        "env": env,
        "bucket": bucket,
        "log": log,
        "prefix": bucket / URL[5:],
        "lock": lock,
    }


def set_state(site: Path, value: str) -> None:
    """Record a site-import state on the site, as site-import does."""
    (site / ".chart").mkdir(mode=0o700, exist_ok=True)
    record = {"schema": 1, "state": value, "updated": "2026-01-05T00:00:00Z"}
    (site / ".chart" / "site-state.json").write_text(json.dumps(record))


def taken(stamp: str) -> datetime.datetime:
    moment = datetime.datetime.strptime(stamp[:16], "%Y%m%dT%H%M%SZ")
    return moment.replace(tzinfo=datetime.UTC)


def marker(moment: datetime.datetime) -> str:
    return json.dumps({"publishedAt": moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")}) + "\n"


def store(prefix: Path, stamp: str, *, published: bool = True) -> None:
    """A backup in the bucket; published, as soon as it was taken, or
    left by an interrupted run.
    """
    (prefix / stamp).mkdir(parents=True)
    (prefix / stamp / "manifest.json").write_text("{}\n")
    if published:
        (prefix / stamp / "published").write_text(marker(taken(stamp)))


RUN = "0123abcd"


def backup(
    world: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    stamp: str,
    run: str = RUN,
    argv: tuple[str, ...] = (),
) -> tuple[int, str]:
    """Run a backup whose clock stands still at stamp; return its exit
    status and its prefix.
    """
    module = load()
    monkeypatch.setattr(module, "clock", lambda: taken(stamp))
    monkeypatch.setattr(module, "run_id", lambda: run)
    return module.main(world["env"], argv), f"{stamp}-{run}"


@pytest.mark.parametrize(
    ("stamps", "daily", "weekly", "expected"),
    [
        # Newest per day, for the last two days that hold a backup
        (
            ["20260105T020000Z", "20260105T120000Z", "20260106T020000Z"],
            2,
            0,
            {"20260105T120000Z", "20260106T020000Z"},
        ),
        # A gap of days does not use up the daily count
        (
            ["20260101T020000Z", "20260110T020000Z"],
            2,
            0,
            {"20260101T020000Z", "20260110T020000Z"},
        ),
        # 2026-01-04 is a Sunday, closing ISO week 1
        (
            ["20251229T020000Z", "20260104T020000Z", "20260105T020000Z"],
            1,
            2,
            {"20260104T020000Z", "20260105T020000Z"},
        ),
        (["20260105T020000Z", "20260106T020000Z"], 0, 0, set()),
    ],
)
def test_retention(
    stamps: list[str], daily: int, weekly: int, expected: set[str]
) -> None:
    published = {stamp: taken(stamp) for stamp in stamps}
    assert retention.kept(published, daily, weekly) == expected


def test_retention_orders_by_publication() -> None:
    """The reviewed case: two runs started in the same second publish
    in the opposite order to their run ids. Daily retention keeps the
    one published last on 5 January, as the restore-window rule
    orders them, not the one whose prefix sorts last.
    """
    first, second = "20260105T020000Z-ffffffff", "20260105T020000Z-00000000"
    next_day = "20260106T020000Z-12345678"
    published = {
        first: at(5, 2, 10).replace(tzinfo=datetime.UTC),
        second: at(5, 2, 20).replace(tzinfo=datetime.UTC),
        next_day: at(6, 2, 10).replace(tzinfo=datetime.UTC),
    }
    assert retention.by_publication(published) == [first, second, next_day]
    assert retention.successor(published, first) == second
    assert retention.kept(published, 2, 0) == {second, next_day}
    bucket = listing(published, published_at=dict(published), latest=next_day)
    moment = at(8, 2).replace(tzinfo=datetime.UTC)
    keep = retention.retain(bucket, moment, 2, 0, 24, RUN_HOURS)
    assert keep == {second, next_day}


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("SITE_BACKUP_URL", "https://bucket/x", "SITE_BACKUP_URL must be"),
        ("SITE_BACKUP_URL", "s3://bucket/../x", "SITE_BACKUP_URL must be"),
        ("SITE_BACKUP_S3_PROVIDER", "", "SITE_BACKUP_S3_PROVIDER must name"),
        ("SITE_BACKUP_KEEP_DAILY", "-1", "SITE_BACKUP_KEEP_DAILY must be"),
        ("SITE_BACKUP_KEEP_WEEKLY", "", "SITE_BACKUP_KEEP_WEEKLY must be"),
        ("SITE_BACKUP_MIN_AGE_HOURS", "1.5", "SITE_BACKUP_MIN_AGE_HOURS must be"),
        ("SITE_BACKUP_MIN_AGE_HOURS", "0", "SITE_BACKUP_MIN_AGE_HOURS must be"),
        ("SITE_BACKUP_MAX_RUN_HOURS", "0", "SITE_BACKUP_MAX_RUN_HOURS must be"),
        ("SITE_BACKUP_MAX_RUN_HOURS", "", "SITE_BACKUP_MAX_RUN_HOURS must be"),
    ],
)
def test_settings_refuse_bad_values(
    world: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    name: str,
    value: str,
    message: str,
) -> None:
    world["env"][name] = value
    assert load().main(world["env"]) == 1
    assert message in capsys.readouterr().err
    assert not world["log"].exists()


def test_rclone_environment(world: dict[str, Any]) -> None:
    module = load()
    settings = module.Settings.from_env(world["env"])
    env = module.rclone_env(settings, world["env"])
    assert {k: v for k, v in env.items() if k.startswith("RCLONE_")} == {
        "RCLONE_CONFIG": "/notfound",
        "RCLONE_CONFIG_SITEBACKUP_TYPE": "s3",
        "RCLONE_CONFIG_SITEBACKUP_PROVIDER": "Minio",
        "RCLONE_CONFIG_SITEBACKUP_ENV_AUTH": "true",
        "RCLONE_CONFIG_SITEBACKUP_ENDPOINT": "http://store.example:9000",
    }
    assert env["AWS_SECRET_ACCESS_KEY"] == SECRET


@needs_tools
def test_backup_uploads_verifies_then_moves_latest(
    world: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, stamp = backup(world, monkeypatch, "20260105T020000Z")
    out = capsys.readouterr()
    assert code == 0, out.err
    prefix: Path = world["prefix"]
    assert (prefix / "latest").read_text() == f"{stamp}\n"
    assert sorted(p.name for p in (prefix / stamp).iterdir()) == [
        "git.tar.zst",
        "manifest.json",
        "published",
        "site.tar.zst",
    ]
    assert (prefix / stamp / "published").read_text() == marker(taken(stamp))
    lines = world["log"].read_text().splitlines()
    calls = [line.split()[0] for line in lines[::3]]
    assert calls == ["copyto"] * 3 + [
        "check",
        "lsf",
        "cat",
        "check",
        "copyto",
        "copyto",
        "lsf",
        "cat",
        "cat",
    ]
    assert (
        lines[0].split()[-1] == f"sitebackup:gerrit-backups/gerrit/{stamp}/site.tar.zst"
    )
    assert lines[6].split()[-1].endswith(f"{stamp}/manifest.json")
    # The stored manifest is read back and the archives checked again;
    # then latest moves before the backup is marked published
    assert lines[15].split()[-1].endswith(f"{stamp}/manifest.json")
    assert lines[21].split()[-1] == "sitebackup:gerrit-backups/gerrit/latest"
    assert lines[24].split()[-1].endswith(f"{stamp}/published")
    # rclone sees only the chart's RCLONE_ settings, and the credentials
    # only through the environment
    assert "RCLONE_HEADER" not in lines[1]
    assert "RCLONE_CONFIG_SITEBACKUP_ENV_AUTH" in lines[1]
    assert lines[2] == "True"
    assert SECRET not in world["log"].read_text() + out.out + out.err
    assert f"{URL}/latest names {stamp}" in out.out


@needs_tools
def test_backup_prunes_by_day_and_week(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix: Path = world["prefix"]
    # Older backups: two on Monday 5 January, one each in the two weeks
    # before, and a run that never published its upload, over
    # maxRunHours + 1 hours ago
    for stamp in (
        "20251222T020000Z",
        "20251229T020000Z",
        "20260105T020000Z",
        "20260105T120000Z",
    ):
        store(prefix, stamp)
    store(prefix, "20260105T180000Z", published=False)
    code, stamp = backup(world, monkeypatch, "20260106T020000Z")
    assert code == 0
    # Daily keeps today's and the newest of 5 January; weekly keeps
    # those and the newest of the week before
    assert sorted(p.name for p in prefix.iterdir()) == [
        "20251229T020000Z",
        "20260105T120000Z",
        stamp,
        "latest",
    ]


def stamps(start: datetime.datetime, hours: int) -> list[str]:
    """Timestamps every hour from start, hours of them."""
    step = datetime.timedelta(hours=1)
    return [(start + n * step).strftime("%Y%m%dT%H%M%SZ") for n in range(hours)]


def at(day: int, hour: int, minute: int = 0) -> datetime.datetime:
    return datetime.datetime(2026, 1, day, hour, minute)


def listing(
    published: Iterable[str],
    unpublished: Iterable[str] = (),
    latest: str = "",
    published_at: dict[str, datetime.datetime] | None = None,
) -> Any:
    """A bucket listing; each backup published as soon as it was taken
    unless published_at says otherwise, and latest defaulting to the
    newest published backup.
    """
    published = set(published)
    times: dict[str, datetime.datetime] = {s: taken(s) for s in published}
    times.update(published_at or {})
    return retention.Listing(
        found=published | set(unpublished),
        published=times,
        latest=latest or max(published, default=None),
    )


RUN_HOURS = 6


def retain(bucket: Any, run: str, daily: int, weekly: int, hours: int) -> set[str]:
    """What a run pruning at the time run names keeps."""
    moment = taken(run)
    keep: set[str] = retention.retain(bucket, moment, daily, weekly, hours, RUN_HOURS)
    return keep


def published_run(
    bucket: set[str], run: str, daily: int, weekly: int, hours: int
) -> set[str]:
    """What a successful run at run keeps of bucket, all published."""
    return retain(listing({*bucket, run}), run, daily, weekly, hours)


def test_restore_window_starts_when_superseded() -> None:
    """A backup outlives its successor by minAgeHours, not itself.

    Daily backups at 00:00 until 6 January; a restore resolves latest,
    the 6 January backup, at 23:00. Runs follow at 23:10 and then
    hourly from 00:10. With the default retention, the backup must
    stay until 23:10 on 7 January, minAgeHours after 23:10 replaced it.
    """
    restored = "20260106T000000Z"
    bucket = {f"2026010{day}T000000Z" for day in range(1, 7)}
    for run in stamps(at(6, 23, 10), 26):
        bucket = published_run(bucket, run, 14, 8, 24)
        assert (restored in bucket) == (run < "20260107T231000Z"), run


@pytest.mark.parametrize(
    ("run", "expected"),
    [
        # 1 January was latest until 10 January 00:00
        ("20260110T230000Z", {"20260101T000000Z", "20260110T000000Z"}),
        ("20260111T000000Z", {"20260110T000000Z"}),
    ],
)
def test_restore_window_after_a_long_gap(run: str, expected: set[str]) -> None:
    bucket = {"20260101T000000Z", "20260110T000000Z"}
    assert published_run(bucket, run, 0, 0, 24) == {*expected, run}


@pytest.mark.parametrize("hours", [0, 24])
def test_single_backup_is_kept(hours: int) -> None:
    assert published_run(set(), "20260106T020000Z", 0, 0, hours) == {"20260106T020000Z"}


def test_unpublished_upload_is_no_successor() -> None:
    """The reviewed scenario: latest names 6 January 00:00, an upload
    at 01:00 never published, and the next run succeeds on 7 January
    at 02:00. A restore may have resolved 00:00 just before that run,
    so 00:00 stays; the unpublished upload is a day old and goes.
    """
    run = "20260107T020000Z"
    bucket = listing(
        {"20260105T000000Z", "20260106T000000Z", run},
        {"20260106T010000Z"},
    )
    assert retain(bucket, run, 0, 0, 24) == {"20260106T000000Z", run}


def test_interrupted_before_latest() -> None:
    """A run stopped after its archives, before latest: latest still
    names 5 January 02:00, whose successor is this run's backup, not
    the unpublished upload of 03:00, which is old enough to delete.
    """
    run = "20260106T020000Z"
    bucket = listing({"20260105T020000Z", run}, {"20260105T030000Z"})
    assert retain(bucket, run, 0, 0, 2) == {"20260105T020000Z", run}


@pytest.mark.parametrize(
    ("hours", "expected"),
    [
        # latest names 01:00, which succeeded 5 January 02:00 at 01:00
        (24, {"20260105T020000Z", "20260106T010000Z"}),
        (0, {"20260106T010000Z"}),
    ],
)
def test_interrupted_after_latest(hours: int, expected: set[str]) -> None:
    """A run stopped after moving latest, before its marker: latest
    names that backup, which is published, kept and a successor.
    """
    bucket = listing(
        {"20260105T020000Z"}, {"20260106T010000Z"}, latest="20260106T010000Z"
    )
    assert retain(bucket, "20260106T030000Z", 0, 0, hours) == expected


@pytest.mark.parametrize(
    ("upload", "hours", "kept"),
    [
        # No older than maxRunHours + 1, 7 hours: perhaps a run still
        # uploading, whatever minAgeHours is
        ("20260106T010000Z", 1, True),
        ("20260105T190000Z", 1, True),
        ("20260105T190000Z", 48, True),
        ("20260105T185959Z", 1, False),
        ("20260105T185959Z", 48, False),
    ],
)
def test_unpublished_upload_kept_while_young(
    upload: str, hours: int, kept: bool
) -> None:
    run = "20260106T020000Z"
    bucket = listing({run}, {upload})
    assert retain(bucket, run, 14, 8, hours) == ({run, upload} if kept else {run})


@pytest.mark.parametrize(
    ("run", "kept"),
    [
        # 5 January 01:00 was latest until its successor, started on
        # 6 January at 01:00, was published at 05:00
        ("20260107T045900Z", True),
        ("20260107T050100Z", False),
    ],
)
def test_restore_window_starts_when_successor_published(run: str, kept: bool) -> None:
    """A slow run shortens no restore window: it starts when the
    successor moved latest, not when the successor's run started.
    """
    old, slow, fast = "20260105T010000Z", "20260106T010000Z", "20260107T020000Z"
    bucket = listing(
        {old, slow, fast}, published_at={slow: at(6, 5).replace(tzinfo=datetime.UTC)}
    )
    expected = {old, slow, fast} if kept else {slow, fast}
    assert retain(bucket, run, 0, 0, 24) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            '{"publishedAt": "2026-01-05T02:41:07.123456Z"}',
            datetime.datetime(2026, 1, 5, 2, 41, 7, 123456, tzinfo=datetime.UTC),
        ),
        # One format: a time without microseconds is no valid publishedAt
        ('{"publishedAt": "2026-01-05T02:41:07Z"}', None),
        ("", None),
        ("[]", None),
        ('{"publishedAt": "yesterday"}', None),
    ],
)
def test_published_at(text: str, expected: datetime.datetime | None) -> None:
    assert retention.published_at(text) == expected


def test_marker_without_a_time_leaves_its_prefix_unpublished() -> None:
    """A marker with no valid publishedAt publishes nothing: its prefix
    is a leftover, deleted under the unpublished-prefix rule, and no
    successor of the backup latest named before this run.
    """
    old, run = "20260102T000000Z", "20260111T000000Z"
    bucket = listing({old, run}, {"20260101T000000Z"})
    assert retain(bucket, run, 1, 0, 24) == {old, run}


@needs_tools
@pytest.mark.parametrize(
    ("min_age", "expected"),
    [
        # Every backup whose successor was taken in the 24 hours before
        # this run
        ("24", [f"2026010{d}T{h:02d}0000Z" for d, h in HOURLY[6:-1]]),
        # With an hour's grace, the backup this run replaced alone
        ("1", ["20260106T050000Z"]),
    ],
)
def test_prune_spares_recently_superseded(
    world: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    min_age: str,
    expected: list[str],
) -> None:
    prefix: Path = world["prefix"]
    # Hourly backups from 5 January 00:00 to 6 January 05:00, the last
    # named by latest, as a restore in flight may have resolved it
    for day, hour in HOURLY[:-1]:
        store(prefix, f"2026010{day}T{hour:02d}0000Z")
    (prefix / "latest").write_text("20260106T050000Z\n")
    world["env"]["SITE_BACKUP_KEEP_DAILY"] = "1"
    world["env"]["SITE_BACKUP_KEEP_WEEKLY"] = "0"
    world["env"]["SITE_BACKUP_MIN_AGE_HOURS"] = min_age
    code, stamp = backup(world, monkeypatch, "20260106T060000Z")
    assert code == 0
    assert (prefix / "latest").read_text() == f"{stamp}\n"
    assert sorted(p.name for p in prefix.iterdir()) == [*expected, stamp, "latest"]


@needs_tools
def test_backup_marks_a_backup_latest_names(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run that stopped after moving latest left no marker; the next
    run writes it, so that backup stays in the published sequence.
    """
    prefix: Path = world["prefix"]
    store(prefix, "20260105T020000Z")
    store(prefix, "20260106T010000Z", published=False)
    (prefix / "latest").write_text("20260106T010000Z\n")
    world["env"]["SITE_BACKUP_KEEP_DAILY"] = "0"
    world["env"]["SITE_BACKUP_KEEP_WEEKLY"] = "0"
    world["env"]["SITE_BACKUP_MIN_AGE_HOURS"] = "24"
    code, stamp = backup(world, monkeypatch, "20260106T020000Z")
    assert code == 0
    assert (prefix / "20260106T010000Z" / "published").read_text() == marker(
        taken(stamp)
    )
    assert sorted(p.name for p in prefix.iterdir()) == [
        "20260105T020000Z",
        "20260106T010000Z",
        stamp,
        "latest",
    ]


@needs_tools
def test_marker_without_a_time_is_never_rewritten(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewed scenario: 1 January's marker records no time and
    latest names 2 January. A run on 10 January fails its confirmation
    before publishing, and must not give 1 January a marker that would
    make it 2 January's successor. A run on 11 January then publishes
    with one daily backup and a 24-hour window: 2 January, which latest
    named until that moment, stays for its full restore window, and the
    leftover 1 January goes.
    """
    prefix: Path = world["prefix"]
    old, previous = "20260101T000000Z", "20260102T000000Z"
    store(prefix, old)
    (prefix / old / "published").write_text("")
    store(prefix, previous)
    (prefix / "latest").write_text(f"{previous}\n")
    world["env"]["SITE_BACKUP_KEEP_DAILY"] = "1"
    world["env"]["SITE_BACKUP_KEEP_WEEKLY"] = "0"
    world["env"]["SITE_BACKUP_MIN_AGE_HOURS"] = "24"
    module = load()

    def unconfirmed(*_args: Any) -> None:
        raise module.BackupError("the stored manifest went missing")

    monkeypatch.setattr(module, "confirm", unconfirmed)
    monkeypatch.setattr(module, "clock", lambda: taken("20260110T000000Z"))
    monkeypatch.setattr(module, "run_id", lambda: RUN)
    assert module.main(world["env"]) == 1
    assert (prefix / old / "published").read_text() == ""
    assert (prefix / "latest").read_text() == f"{previous}\n"
    code, stamp = backup(world, monkeypatch, "20260111T000000Z")
    assert code == 0
    assert sorted(p.name for p in prefix.iterdir()) == [previous, stamp, "latest"]


@needs_tools
def test_prune_ignores_an_unpublished_upload(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewed scenario, end to end."""
    prefix: Path = world["prefix"]
    store(prefix, "20260105T000000Z")
    store(prefix, "20260106T000000Z")
    store(prefix, "20260106T010000Z", published=False)
    (prefix / "latest").write_text("20260106T000000Z\n")
    world["env"]["SITE_BACKUP_KEEP_DAILY"] = "0"
    world["env"]["SITE_BACKUP_KEEP_WEEKLY"] = "0"
    world["env"]["SITE_BACKUP_MIN_AGE_HOURS"] = "24"
    code, stamp = backup(world, monkeypatch, "20260107T020000Z")
    assert code == 0
    assert sorted(p.name for p in prefix.iterdir()) == [
        "20260106T000000Z",
        stamp,
        "latest",
    ]


@needs_tools
def test_prune_leaves_a_young_incomplete_backup(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another run's verified upload, started five hours ago and not yet
    published, survives this run's prune with a one-hour minAgeHours:
    only an upload over maxRunHours + 1 hours old is abandoned.
    """
    prefix: Path = world["prefix"]
    for stamp in ("20260105T020000Z", "20260105T210000Z-bbbbbbbb"):
        (prefix / stamp).mkdir(parents=True)
        (prefix / stamp / "site.tar.zst").write_text("partial")
    code, stamp = backup(world, monkeypatch, "20260106T020000Z")
    assert code == 0
    assert sorted(p.name for p in prefix.iterdir()) == [
        "20260105T210000Z-bbbbbbbb",
        stamp,
        "latest",
    ]


@needs_tools
@pytest.mark.parametrize("name", ["manifest.json", "git.tar.zst"])
def test_backup_vanished_before_publishing_leaves_latest(
    world: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
) -> None:
    """A run whose stored manifest or archive went missing after the
    upload's check fails without moving latest.
    """
    prefix: Path = world["prefix"]
    store(prefix, "20260105T020000Z")
    (prefix / "latest").write_text("20260105T020000Z\n")
    module = load()
    reconcile = module.reconcile

    def purged(settings: Any, env: Any) -> None:
        reconcile(settings, env)
        (prefix / f"20260106T020000Z-{RUN}" / name).unlink()

    monkeypatch.setattr(module, "reconcile", purged)
    monkeypatch.setattr(module, "clock", lambda: taken("20260106T020000Z"))
    monkeypatch.setattr(module, "run_id", lambda: RUN)
    assert module.main(world["env"]) == 1
    assert "failed with exit status" in capsys.readouterr().err
    assert (prefix / "latest").read_text() == "20260105T020000Z\n"
    assert sorted(p.name for p in prefix.iterdir()) == ["20260105T020000Z", "latest"]


@needs_tools
@pytest.mark.parametrize("step", ["copyto", "check", "lsf", "cat"])
def test_failed_upload_keeps_latest(
    world: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    step: str,
) -> None:
    prefix: Path = world["prefix"]
    store(prefix, "20260105T020000Z")
    (prefix / "latest").write_text("20260105T020000Z\n")
    world["env"]["FAKE_FAIL"] = step
    code, _ = backup(world, monkeypatch, "20260106T020000Z")
    assert code == 1
    assert "failed with exit status 3" in capsys.readouterr().err
    assert (prefix / "latest").read_text() == "20260105T020000Z\n"
    assert sorted(p.name for p in prefix.iterdir()) == ["20260105T020000Z", "latest"]


@needs_tools
def test_failed_export_uploads_nothing(
    world: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    world["env"]["SITE_BACKUP_EXPORTER"] = str(world["bucket"] / "missing.py")
    code, _ = backup(world, monkeypatch, "20260106T020000Z")
    assert code == 1
    assert "the site export failed" in capsys.readouterr().err
    assert not world["log"].exists()


@needs_tools
def test_backup_passes_exclude_to_the_export(
    world: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    source: Path,
) -> None:
    (source / "data").mkdir(exist_ok=True)
    for suffix in (".h2.db", ".lock.db", ".trace.db"):
        (source / "data" / f"events-log-db{suffix}").write_text("events\n")
    (source / "data" / "kept.db").write_text("kept\n")
    argv = ("--exclude=data/events-log-db*",)
    code, stamp = backup(world, monkeypatch, "20260106T020000Z", argv=argv)
    assert code == 0, capsys.readouterr().err
    manifest = json.loads((world["prefix"] / stamp / "manifest.json").read_text())
    assert manifest["excluded"][-1] == "data/events-log-db*"
    archive = world["prefix"] / stamp / "site.tar.zst"
    listing = subprocess.run(
        ["tar", "--zstd", "-tf", str(archive)], capture_output=True, text=True
    )
    assert listing.returncode == 0, listing.stderr
    names = listing.stdout.splitlines()
    assert any(n.endswith("data/kept.db") for n in names)
    assert not [n for n in names if "events-log-db" in n]


@pytest.mark.parametrize("arg", ["--pack", "--exclude", "data/x", "--upload=s3://b/p"])
def test_backup_takes_only_exclude_arguments(
    world: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arg: str,
) -> None:
    code, _ = backup(world, monkeypatch, "20260106T020000Z", argv=(arg,))
    assert code == 1
    assert f"{arg}: site-backup takes only --exclude=<path>" in capsys.readouterr().err
    assert not world["log"].exists()


@needs_tools
def test_backup_restores_through_latest(
    world: dict[str, Any],
    pod: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, stamp = backup(world, monkeypatch, "20260106T020000Z")
    assert code == 0, capsys.readouterr().err
    env = {
        **pod["env"],
        "PATH": world["env"]["PATH"],
        "FAKE_BUCKET": world["env"]["FAKE_BUCKET"],
        "FAKE_LOG": world["env"]["FAKE_LOG"],
        "SITE_IMPORT_SOURCE_TYPE": "s3",
        "SITE_IMPORT_SOURCE_URL": f"{URL}/latest",
    }
    _, code = run_main(env, monkeypatch)
    out = capsys.readouterr()
    assert code == 0, out.err
    assert f"{URL}/latest names the backup {URL}/{stamp}" in out.out
    assert state(pod["site"])["state"] == "complete"
    assert (pod["site"] / "etc" / "gerrit.config").is_file()
    assert (pod["site"] / "git" / "apps" / "web.git").is_dir()


@pytest.mark.parametrize("value", [None, "in-progress", "failed", "not json"])
def test_backup_skips_a_site_not_complete(
    world: dict[str, Any],
    source: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    value: str | None,
) -> None:
    """No state file, or an import running or failed: nothing to back up."""
    path = source / ".chart" / "site-state.json"
    if value is None:
        path.unlink()
    elif value == "not json":
        path.write_text("{")
    else:
        set_state(source, value)
    store(world["prefix"], "20260105T020000Z")
    (world["prefix"] / "latest").write_text("20260105T020000Z\n")
    code, _ = backup(world, monkeypatch, "20260106T020000Z")
    out = capsys.readouterr()
    assert code == 0, out.err
    assert f"skipping this run: {path}" in out.out
    assert not world["log"].exists()
    assert not (Path(world["env"]["SITE_BACKUP_SCRATCH"]) / "export").exists()
    assert (world["prefix"] / "latest").read_text() == "20260105T020000Z\n"


@needs_tools
@pytest.mark.parametrize("value", ["in-progress", "complete"])
def test_backup_discards_an_export_an_import_overlapped(
    world: dict[str, Any],
    source: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    value: str,
) -> None:
    """An import that starts during the export, or starts and completes,
    rewrites the state file: the export may hold half of each site.
    """
    module = load()
    export = module.export

    def overlapped(*args: Any) -> Path:
        out: Path = export(*args)
        set_state(source, value)
        (source / ".chart" / "site-state.json").write_text(
            (source / ".chart" / "site-state.json")
            .read_text()
            .replace("01-05", "01-06")
        )
        return out

    monkeypatch.setattr(module, "export", overlapped)
    monkeypatch.setattr(module, "clock", lambda: taken("20260106T020000Z"))
    code = module.main(world["env"])
    out = capsys.readouterr()
    assert code == 0, out.err
    assert "discarding the export" in out.out
    assert not world["log"].exists()
    assert not (Path(world["env"]["SITE_BACKUP_SCRATCH"]) / "export").exists()


@pytest.mark.parametrize(
    ("name", "valid"),
    [
        ("20260105T020000Z", True),
        ("20260105T020000Z-0123abcd", True),
        ("20260105T020000Z-0123ABCD", False),
        ("20260105T020000Z-0123abc", False),
        ("20260105T020000Z-", False),
        ("20261305T020000Z-0123abcd", False),
        ("latest", False),
    ],
)
def test_prefixes_with_and_without_a_run_id(name: str, valid: bool) -> None:
    assert retention.is_stamp(name) == valid
    if valid:
        assert retention.taken(name) == taken("20260105T020000Z")


def test_retention_counts_prefixes_with_and_without_a_run_id() -> None:
    old, new = "20260105T020000Z", "20260106T020000Z-0123abcd"
    run = "20260107T020000Z-89abcdef"
    assert retain(listing({old, new, run}), run, 2, 0, 0) == {new, run}


def test_backups_published_at_the_same_moment() -> None:
    """Two runs published in the same microsecond: neither is the
    other's successor, whatever their run ids, so with the default
    retention both stay until a day after the next day's backup.
    """
    first, second = "20260105T020000Z-ffffffff", "20260105T020000Z-00000000"
    tied = at(5, 4).replace(tzinfo=datetime.UTC)
    next_day = "20260106T020000Z-12345678"
    bucket = listing(
        {first, second, next_day},
        latest=next_day,
        published_at={first: tied, second: tied},
    )
    assert retain(bucket, "20260106T043000Z", 0, 0, 24) == {first, second, next_day}
    assert retain(bucket, "20260107T020100Z", 0, 0, 24) == {next_day}


@needs_tools
def test_runs_in_the_same_second_keep_apart(
    world: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Two Jobs started in the same second write distinct prefixes, and
    the one that fails discards only its own.
    """
    prefix: Path = world["prefix"]
    code, first = backup(world, monkeypatch, "20260106T020000Z", "aaaaaaaa")
    assert code == 0
    world["env"]["FAKE_FAIL"] = "check"
    code, second = backup(world, monkeypatch, "20260106T020000Z", "bbbbbbbb")
    assert code == 1
    assert f"{second}/manifest.json" in world["log"].read_text()
    assert f"deleting the incomplete {URL}/{second}" in capsys.readouterr().out
    assert sorted(p.name for p in prefix.iterdir()) == [first, "latest"]
    assert (prefix / "latest").read_text() == f"{first}\n"
    assert (prefix / first / "published").is_file()


def test_successor_is_the_next_published() -> None:
    """Runs are serialised and each writes latest, so the backup
    published next replaced it, whatever time its run started.
    """
    early, late, next_day = "20260105T010000Z", "20260105T020000Z", "20260106T020000Z"
    published = {
        early: at(5, 5).replace(tzinfo=datetime.UTC),
        late: at(5, 4).replace(tzinfo=datetime.UTC),
        next_day: taken(next_day),
    }
    assert retention.successor(published, late) == early
    assert retention.successor(published, early) == next_day
    assert retention.successor(published, next_day) is None


def hold(path: Path) -> int:
    """Hold the backup lock, as a run in flight does."""
    fd = os.open(path, os.O_RDONLY)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def test_run_skips_while_another_holds_the_lock(
    world: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The reviewed interleavings need two runs between reading and
    writing the store; a second run while the lock is held exits 0
    without touching it.
    """
    prefix: Path = world["prefix"]
    store(prefix, "20260105T020000Z")
    (prefix / "latest").write_text("20260105T020000Z\n")
    lock: Path = world["lock"]
    fd = hold(lock)
    try:
        code, _ = backup(world, monkeypatch, "20260106T020000Z")
    finally:
        os.close(fd)
    out = capsys.readouterr()
    assert code == 0, out.err
    assert f"skipping this run: another backup holds {lock}" in out.out
    assert not world["log"].exists()
    assert not (Path(world["env"]["SITE_BACKUP_SCRATCH"]) / "export").exists()
    assert (prefix / "latest").read_text() == "20260105T020000Z\n"
    assert sorted(p.name for p in prefix.iterdir()) == ["20260105T020000Z", "latest"]


@needs_tools
def test_lock_released_when_the_holder_is_killed(
    world: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A run killed while it holds the lock, as by the Job's deadline,
    leaves no stale lock: the kernel drops it with the process.
    """
    lock = str(world["lock"])
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl, os, sys, time\n"
            "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "print('held', flush=True)\n"
            "time.sleep(600)\n",
            lock,
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline() == "held\n"
        code, _ = backup(world, monkeypatch, "20260106T020000Z")
        assert code == 0
        assert "another backup holds" in capsys.readouterr().out
        assert not world["log"].exists()
    finally:
        holder.send_signal(signal.SIGKILL)
        holder.wait()
        assert holder.stdout is not None
        holder.stdout.close()
    code, stamp = backup(world, monkeypatch, "20260106T020000Z")
    assert code == 0, capsys.readouterr().err
    assert (world["prefix"] / "latest").read_text() == f"{stamp}\n"
    # This run released the lock in turn
    os.close(hold(Path(lock)))


@pytest.mark.parametrize("missing", ["directory", "file"])
def test_run_skips_without_the_lock(
    world: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    missing: str,
) -> None:
    """A site no Gerrit pod has finished initialising under this chart
    version has no lock, which site-init creates last: the run skips,
    as for an incomplete import, rather than create anything on the
    site.
    """
    lock: Path = world["lock"]
    lock.unlink()
    if missing == "directory":
        lock.parent.rmdir()
    code, _ = backup(world, monkeypatch, "20260106T020000Z")
    out = capsys.readouterr()
    assert code == 0, out.err
    assert f"skipping this run: {lock} does not exist" in out.out
    assert not world["log"].exists()
    assert not lock.exists()


@needs_tools
def test_lock_needs_no_write_access(
    world: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The backup mounts the site read-only: flock works on a read-only
    descriptor, so a lock file it cannot write still serialises runs.
    """
    lock: Path = world["lock"]
    lock.chmod(0o400)
    lock.parent.chmod(0o500)
    try:
        fd = hold(lock)
        try:
            code, _ = backup(world, monkeypatch, "20260106T020000Z")
            assert code == 0
            assert "another backup holds" in capsys.readouterr().out
        finally:
            os.close(fd)
        code, stamp = backup(world, monkeypatch, "20260106T020000Z")
        assert code == 0, capsys.readouterr().err
        assert (world["prefix"] / "latest").read_text() == f"{stamp}\n"
    finally:
        lock.parent.chmod(0o700)


@pytest.mark.skipif(os.geteuid() == 0, reason="root opens any file")
def test_lock_that_cannot_open_fails(
    world: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    world["lock"].chmod(0)
    code, _ = backup(world, monkeypatch, "20260106T020000Z")
    assert code == 1
    assert "cannot open the backup lock" in capsys.readouterr().err
    assert not world["log"].exists()


def set_base_path(site: Path, value: str) -> None:
    config = str(site / "etc" / "gerrit.config")
    subprocess.run(
        ["git", "config", "-f", config, "gerrit.basePath", value], check=True
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("git", "git"),
        ("repos/store", "repos/store"),
        ("/var/gerrit/git", "git"),
        ("/var/gerrit/data/../repos", "repos"),
    ],
)
def test_git_store_on_the_site_volume(source: Path, value: str, expected: str) -> None:
    set_base_path(source, value)
    assert load().git_store(os.environ, source) == source / expected


@pytest.mark.parametrize(
    "value",
    ["/srv/git", "/var/gerrit", "/var/gerrit/../git", "/var/gerritx/git", "../git"],
)
def test_git_store_off_the_site_volume_fails(source: Path, value: str) -> None:
    set_base_path(source, value)
    module = load()
    with pytest.raises(module.VolumeError, match="is not on the site volume"):
        module.git_store(os.environ, source)


@needs_tools
def test_backup_of_an_absolute_base_path(
    world: dict[str, Any],
    source: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An absolute basePath names the Gerrit container's /var/gerrit:
    the backup reads that store from the site volume, not the image.
    """
    (source / "git").rename(source / "repos")
    set_base_path(source, "/var/gerrit/repos")
    code, stamp = backup(world, monkeypatch, "20260106T020000Z")
    assert code == 0, capsys.readouterr().err
    archive = world["prefix"] / stamp / "git.tar.zst"
    names = subprocess.run(
        ["tar", "--zstd", "-tf", str(archive)],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    ).stdout
    assert "All-Projects.git" in names, names


def test_backup_refuses_a_git_store_off_the_volume(
    world: dict[str, Any],
    source: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    set_base_path(source, "/srv/git")
    code, _ = backup(world, monkeypatch, "20260106T020000Z")
    assert code == 1
    err = capsys.readouterr().err
    assert "gerrit.basePath /srv/git is not on the site volume" in err
    assert "backs up a git store only under /var/gerrit" in err
    assert not world["log"].exists()
