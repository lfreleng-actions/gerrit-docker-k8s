# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Gerrit releases, and what ``site-upgrade.py`` does about them.

Compares the release a site records with the image's, as releases
(major, minor, patch), and decides from ``siteUpgrade.mode`` whether
the start does nothing, warns, refuses or upgrades the site.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

OFF, REFUSE, AUTO = "off", "refuse", "auto"
MODES = (OFF, REFUSE, AUTO)
NOTHING, WARN, STOP, UPGRADE = "nothing", "warn", "refuse", "upgrade"

# A release, with an optional "v" and any suffix after "-", "+" or "~"
RELEASE = re.compile(r"v?(\d+)\.(\d+)(?:\.(\d+))?(?:[-+~].*)?")

ENABLE = (
    "set siteUpgrade.mode: auto, or chart.siteUpgrade.mode=auto in the "
    "runtime overrides ConfigMap, to upgrade the site in place"
)
RESTORE = "restore a backup taken before the upgrade"


class UpgradeError(Exception):
    """A failure that stops the start; the message is safe to log."""


def parse_release(text: str | None) -> tuple[int, int, int] | None:
    """Return (major, minor, patch) for a release string, or None."""
    if not text:
        return None
    match = RELEASE.fullmatch(text.strip())
    if not match:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch or 0)


def compare(site: str | None, image: str | None) -> int | None:
    """Return 1 when the image's release is newer than the site's, -1
    when it is older, 0 when they are the same release, and None when
    either is not a release.
    """
    have, want = parse_release(site), parse_release(image)
    if have is None or want is None:
        return None
    return (want > have) - (want < have)


@dataclass(frozen=True)
class Decision:
    action: str
    message: str = ""


def decide(
    mode: str,
    site: str | None,
    image: str | None,
    pending: bool = False,
    target: str | None = None,
) -> Decision:
    """Decide what the step does from the mode and the two releases.

    *site* is the marker's gerritVersion, or the release an unfinished
    upgrade started from when *pending*; *image* the image's release;
    *target* the release an unfinished upgrade was heading for.
    """
    if mode not in MODES:
        raise UpgradeError(
            f"siteUpgrade.mode {mode!r} is not one of {', '.join(MODES)}"
        )
    if site is None and mode == OFF and not pending:
        return Decision(NOTHING)
    if parse_release(image) is None:
        text = "cannot read the Gerrit release of the image's gerrit.war"
        if mode == OFF and not pending:
            return Decision(WARN, f"{text}; not comparing it with the site's {site}")
        return Decision(
            STOP, f"{text}; set siteUpgrade.mode: off to start without comparing"
        )
    if parse_release(site) is None:
        recorded = (
            "records no Gerrit release"
            if site is None
            else (f"records {site!r}, which is not a Gerrit release")
        )
        text = f".chart/initialised {recorded}"
        if mode == OFF and not pending:
            return Decision(WARN, f"{text}; not comparing it with the image's {image}")
        return Decision(
            STOP,
            f"{text}; set its gerritVersion to the release that last initialised "
            "the site, or set siteUpgrade.mode: off",
        )
    order = compare(site, image)
    if pending:
        if order == 1 and mode == AUTO:
            # init may have migrated the site to the target already, so
            # a retry never runs an older release than the target
            if parse_release(target) is None:
                return Decision(
                    STOP,
                    f"an upgrade from Gerrit {site} did not finish, and "
                    ".chart/upgrade-pending records no release it was heading "
                    f"for, so init may have migrated the site past {image}; "
                    f"{RESTORE}",
                )
            if compare(target, image) == -1:
                return Decision(
                    STOP,
                    f"an upgrade from Gerrit {site} to {target} did not finish, "
                    f"and init may have migrated the site to {target}, so the "
                    f"image's Gerrit {image} cannot run it; deploy an image with "
                    f"Gerrit {target} or newer to retry the upgrade, or {RESTORE}",
                )
            return Decision(
                UPGRADE,
                f"resuming the unfinished upgrade from Gerrit {site} to {image}",
            )
        retry = (
            f"set siteUpgrade.mode: auto with an image carrying Gerrit {target} "
            "or newer to retry it, or "
            if parse_release(target)
            else ""
        )
        return Decision(
            STOP,
            f"an upgrade from Gerrit {site} did not finish, so the site has no "
            f".chart/initialised and the image carries Gerrit {image}; "
            f"{retry}{RESTORE}",
        )
    if order == 0:
        return Decision(NOTHING)
    if order == -1:
        text = (
            f"the image carries Gerrit {image}, older than the site's {site}, and "
            f"Gerrit does not support downgrades; deploy an image with Gerrit {site}, "
            f"or {RESTORE}"
        )
        return Decision(WARN if mode == OFF else STOP, text)
    text = f"the site runs Gerrit {site} and the image carries {image}"
    if mode == OFF:
        return Decision(
            WARN, f"{text}; Gerrit starts without init or an offline reindex; {ENABLE}"
        )
    if mode == REFUSE:
        return Decision(
            STOP,
            f"{text}, and siteUpgrade.mode is refuse; {ENABLE}, or deploy an image "
            f"with Gerrit {site}",
        )
    return Decision(UPGRADE, f"upgrading the site from Gerrit {site} to {image}")
