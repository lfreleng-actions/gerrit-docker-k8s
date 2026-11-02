# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Pin the Gerrit chart's image to one published release.

The release workflow runs this on a copy of ``K8S/charts/gerrit``
before ``helm package``, so the chart it publishes names the image the
same release published, by digest. It rewrites ``repository``, ``tag``
and ``digest`` under the top-level ``image`` mapping of the chart's
``values.yaml`` in place, line by line, so every other line and
comment survives; a YAML round trip would drop the comments that
document the values.

The chart has one image, so there is one selector: the three keys,
each exactly once, directly under a block-style top-level ``image:``.
When any of them matches nothing, or more than once, the script fails
and leaves the file untouched. After writing, it parses the file and
checks the three values read back as given.

Usage::

    python3 scripts/pin-chart-images.py --values K8S/charts/gerrit/values.yaml \\
        --repository ghcr.io/<owner>/<repo>/gerrit --tag v1.2.3 \\
        --digest sha256:<64 hex>

Exits 0 on success, 1 when a selector matches nothing or the file
cannot be read, written or parsed, and 2 on a malformed argument.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

KEYS = ("repository", "tag", "digest")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
# An OCI tag: https://github.com/opencontainers/distribution-spec
TAG_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}")
# A repository without a tag or digest: lowercase path components,
# with an optional registry host and port first
REPOSITORY_RE = re.compile(
    r"(?:[A-Za-z0-9.-]+(?::[0-9]+)?/)?[a-z0-9]+(?:[._-][a-z0-9]+)*"
    r"(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*"
)
TOP_LEVEL_RE = re.compile(r"^[^\s#]")
IMAGE_RE = re.compile(r"^image:\s*(#.*)?$")
# value: a plain scalar or a quoted one, then an optional comment
ENTRY_RE = re.compile(
    r"^(?P<indent>[ ]+)(?P<key>[A-Za-z]+):[ ]*"
    r"(?P<value>\"(?:[^\"\\]|\\.)*\"|'(?:[^']|'')*'|[^#\s][^#]*?|)"
    r"(?P<comment>[ ]+#.*)?$"
)


class PinError(Exception):
    """A selector matched nothing, or the file did not round-trip."""


def _block(lines: list[str]) -> tuple[int, int]:
    """Return the ``[start, end)`` line range of the ``image:`` block."""
    starts = [i for i, line in enumerate(lines) if IMAGE_RE.match(line)]
    if len(starts) != 1:
        raise PinError(
            f"expected one block-style top-level 'image:' key, found {len(starts)}"
        )
    start = starts[0] + 1
    end = start
    while end < len(lines) and not TOP_LEVEL_RE.match(lines[end]):
        end += 1
    return start, end


def pin(text: str, values: dict[str, str]) -> str:
    """Return *text* with each ``image.<key>`` set from *values*."""
    lines = text.splitlines(keepends=True)
    start, end = _block(lines)
    entries = [
        (i, m)
        for i in range(start, end)
        if (m := ENTRY_RE.match(lines[i].rstrip("\n")))
    ]
    if not entries:
        raise PinError("the 'image:' block holds no keys")
    indent = min(len(m.group("indent")) for _, m in entries)
    for key in KEYS:
        found = [
            (i, m)
            for i, m in entries
            if m.group("key") == key and len(m.group("indent")) == indent
        ]
        if len(found) != 1:
            raise PinError(f"image.{key} matched {len(found)} lines, not one")
        i, m = found[0]
        newline = lines[i][len(lines[i].rstrip("\n")) :]
        new = f"{m.group('indent')}{key}: {json.dumps(values[key])}"
        comment = m.group("comment") or ""
        if comment:
            # Keep the comment in its column where the value still fits
            column = m.end("comment") - len(comment.lstrip(" "))
            comment = " " * max(1, column - len(new)) + comment.lstrip(" ")
        lines[i] = new + comment + newline
    result = "".join(lines)
    try:
        parsed = yaml.safe_load(result)
    except yaml.YAMLError as exc:
        raise PinError(f"the pinned file does not parse: {exc}") from exc
    image = parsed.get("image") if isinstance(parsed, dict) else None
    if not isinstance(image, dict) or any(image.get(k) != values[k] for k in KEYS):
        raise PinError("the pinned values do not read back as given")
    return result


def _arguments(argv: list[str] | None) -> argparse.Namespace | None:
    """Parse and check *argv*; print the problem and return None if bad."""
    parser = argparse.ArgumentParser(
        description="Pin the chart's image.repository, image.tag and image.digest."
    )
    parser.add_argument("--values", required=True, type=Path, help="values.yaml")
    parser.add_argument("--repository", required=True, help="image repository")
    parser.add_argument("--tag", required=True, help="image tag, such as v1.2.3")
    parser.add_argument("--digest", required=True, help="sha256:<64 hex>")
    args = parser.parse_args(argv)
    checks = (
        ("repository", REPOSITORY_RE, args.repository),
        ("tag", TAG_RE, args.tag),
        ("digest", DIGEST_RE, args.digest),
    )
    for name, pattern, value in checks:
        if not pattern.fullmatch(value):
            print(f"invalid --{name}: {value!r}", file=sys.stderr)
            return None
    return args


def main(argv: list[str] | None = None) -> int:
    """Pin the values file named on the command line; see the module doc."""
    args = _arguments(argv)
    if args is None:
        return 2
    values = {"repository": args.repository, "tag": args.tag, "digest": args.digest}
    path: Path = args.values
    try:
        text = path.read_text(encoding="utf-8")
        path.write_text(pin(text, values), encoding="utf-8")
    except (OSError, PinError) as exc:
        print(f"{path}: {exc}", file=sys.stderr)
        return 1
    print(f"{path}: image {args.repository}:{args.tag} pinned to {args.digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
