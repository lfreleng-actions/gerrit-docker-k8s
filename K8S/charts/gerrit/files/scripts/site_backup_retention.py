# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""Retention for the chart's backup CronJob, ``site-backup.py``.

Pure functions over a listing of the backup prefix: which backup
prefixes a run keeps. ``retain`` states the model.

A backup prefix is ``<UTC timestamp>-<run id>``, the run's start time
as ``%Y%m%dT%H%M%SZ`` and eight hex digits, so that two runs started in
the same second never share one. A bare timestamp, as runs before the
run id wrote, is a backup prefix too.
"""

from __future__ import annotations

import datetime
import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

STAMP = re.compile(r"[0-9]{8}T[0-9]{6}Z(?:-[0-9a-f]{8})?")
STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
STAMP_LENGTH = len("20260105T020000Z")
# publishedAt, to the microsecond: the one format a marker holds
RFC3339 = "%Y-%m-%dT%H:%M:%S.%fZ"


@dataclass(frozen=True)
class Listing:
    found: set[str]  # every backup prefix
    # Those holding a published marker that records a valid time, with
    # that time; a marker without one leaves its prefix unpublished
    published: dict[str, datetime.datetime]
    latest: str | None  # the backup prefix latest names


def published_at(text: str) -> datetime.datetime | None:
    """The time a published marker records, or None if it holds no
    valid publishedAt.
    """
    try:
        value = json.loads(text)["publishedAt"]
        moment = datetime.datetime.strptime(value, RFC3339)
    except (ValueError, TypeError, KeyError):
        return None
    return moment.replace(tzinfo=datetime.UTC)


def week(stamp: str) -> tuple[int, int]:
    day = datetime.datetime.strptime(stamp[:8], "%Y%m%d").date()
    year, number, _ = day.isocalendar()
    return year, number


def taken(stamp: str) -> datetime.datetime:
    """When the run that wrote a backup prefix started."""
    moment = datetime.datetime.strptime(stamp[:STAMP_LENGTH], STAMP_FORMAT)
    return moment.replace(tzinfo=datetime.UTC)


def is_stamp(name: str) -> bool:
    """Whether name is a backup prefix; other names are left alone."""
    if not STAMP.fullmatch(name):
        return False
    try:
        taken(name)
    except ValueError:
        return False
    return True


def young(stamps: Iterable[str], moment: datetime.datetime, hours: int) -> set[str]:
    """The backups taken no more than hours before moment."""
    cutoff = moment - datetime.timedelta(hours=hours)
    return {other for other in stamps if taken(other) >= cutoff}


def by_publication(published: Mapping[str, datetime.datetime]) -> list[str]:
    """The published backups, oldest first, in publishedAt order: the
    order latest named them in, which neither a prefix's timestamp nor
    its random run id gives. Prefixes break ties, for a stable order.
    """
    return sorted(published, key=lambda stamp: (published[stamp], stamp))


def successor(published: Mapping[str, datetime.datetime], stamp: str) -> str | None:
    """The first backup published strictly after stamp: the one that
    moved latest on from it. Backups published at the same moment are
    not each other's successors. None while there is none.
    """
    return next(
        (
            other
            for other in by_publication(published)
            if published[other] > published[stamp]
        ),
        None,
    )


def restorable(
    published: Mapping[str, datetime.datetime], moment: datetime.datetime, hours: int
) -> set[str]:
    """Each backup with no successor yet, and each whose successor was
    published under hours before moment.
    """
    cutoff = moment - datetime.timedelta(hours=hours)
    keep: set[str] = set()
    for stamp in published:
        later = successor(published, stamp)
        if later is None or published[later] > cutoff:
            keep.add(stamp)
    return keep


def kept(
    published: Mapping[str, datetime.datetime], daily: int, weekly: int
) -> set[str]:
    """The backups retention keeps: the last published of each recent
    day and week its run started in.
    """
    newest_first = list(reversed(by_publication(published)))
    keep: set[str] = set()
    rules: list[tuple[int, Callable[[str], object]]] = [
        (daily, lambda stamp: stamp[:8]),
        (weekly, week),
    ]
    for limit, period in rules:
        seen: list[object] = []
        for stamp in newest_first:
            key = period(stamp)
            if key in seen:
                continue
            if len(seen) == limit:
                break
            seen.append(key)
            keep.add(stamp)
    return keep


def retain(
    listing: Listing,
    moment: datetime.datetime,
    daily: int,
    weekly: int,
    hours: int,
    run_hours: int,
) -> set[str]:
    """The backup prefixes a run pruning at moment keeps; it deletes
    the rest.

    Runs are serialised by a lock on the site volume, and a run that
    finds it held skips, so one run at a time writes latest, its
    marker and deletes prefixes; no other run is in flight while this
    one prunes.

    A backup is published once its run has written latest and then
    ``<timestamp>/published``, the run's last write, which records as
    ``publishedAt``, to the microsecond, when latest moved. The
    published sequence is every prefix holding that marker with a
    valid ``publishedAt``, plus the one latest names now even without
    it: a run stopped between the two writes still published its
    backup, and counts as published at moment. A marker without a
    valid ``publishedAt`` publishes nothing: its prefix is an
    unpublished leftover. Retention counts that sequence alone, in
    publishedAt order.

    Each run writes latest, so a backup's successor, the backup that
    moved latest on from it, is the first published strictly after it;
    backups published at the same moment are not each other's
    successors, and the restore window of each runs from that moment.
    Then:

    - the backup latest names is kept;
    - a published backup is kept while it has no successor, or its
      successor was published less than hours before moment: latest
      named it until the successor's publishedAt, so a restore may
      have resolved it that late, however long the successor's run
      took;
    - daily and weekly retention keep the last published backup of
      each of the last daily days and weekly ISO weeks that hold one,
      by the day its run started, in publishedAt order, as the
      restore-window rule counts, never in prefix order: two runs
      started in the same second differ only by a random run id;
    - an unpublished prefix was never named by latest: as no other
      run is in flight, an interrupted run left it, and it is deleted
      once its own timestamp is more than run_hours + 1 hours older
      than moment, run_hours being the Job's activeDeadlineSeconds in
      hours of the pruning run.

    An unpublished upload therefore never shortens another backup's
    restore window.
    """
    named = {listing.latest} & listing.found if listing.latest else set()
    published = {
        stamp: when
        for stamp, when in listing.published.items()
        if stamp in listing.found
    }
    published.update(dict.fromkeys(named - published.keys(), moment))
    return (
        named
        | restorable(published, moment, hours)
        | kept(published, daily, weekly)
        | young(listing.found - published.keys(), moment, run_hours + 1)
    )
