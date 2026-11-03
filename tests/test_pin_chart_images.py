# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for scripts/pin-chart-images.py, the release chart pinning step."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml
from chart.test_render import DEVELOPMENT_AUTH
from chart_digests import check_digests

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "pin-chart-images.py"
CHART = ROOT / "K8S" / "charts" / "gerrit"
REPOSITORY = "ghcr.io/example/gerrit-docker-k8s/gerrit"
TAG = "v1.2.3"
DIGEST = "sha256:" + "d" * 64

VALUES = """\
# Leading comment
nameOverride: ""

image:
  # A comment inside the block
  repository: ghcr.io/old/gerrit   # trailing comment
  tag: ""                         # vX.Y.Z
  digest: ""                      # sha256:...
  pullPolicy: IfNotPresent

toolbox:
  image:
    repository: untouched
    tag: keep
    digest: ""
"""


@pytest.fixture(scope="module")
def pin() -> ModuleType:
    """Load the script, whose name is not an importable module name."""
    spec = importlib.util.spec_from_file_location("pin_chart_images", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(pin: ModuleType, path: Path, *extra: str) -> int:
    """Run the script's ``main`` on *path* with the default pins."""
    args = ["--values", str(path), "--repository", REPOSITORY]
    args += ["--tag", TAG, "--digest", DIGEST, *extra]
    result: int = pin.main(args)
    return result


def write(tmp_path: Path, text: str) -> Path:
    """Write *text* as ``values.yaml`` under *tmp_path*."""
    path = tmp_path / "values.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_pins_the_three_values_and_keeps_the_rest(
    pin: ModuleType, tmp_path: Path
) -> None:
    path = write(tmp_path, VALUES)
    assert run(pin, path) == 0
    text = path.read_text(encoding="utf-8")
    values = yaml.safe_load(text)
    assert values["image"] == {
        "repository": REPOSITORY,
        "tag": TAG,
        "digest": DIGEST,
        "pullPolicy": "IfNotPresent",
    }
    assert values["toolbox"]["image"] == {
        "repository": "untouched",
        "tag": "keep",
        "digest": "",
    }
    changed = [
        (old, new)
        for old, new in zip(VALUES.splitlines(), text.splitlines(), strict=True)
        if old != new
    ]
    assert [old.split(":")[0].strip() for old, _ in changed] == [
        "repository",
        "tag",
        "digest",
    ]
    assert all("#" in new for _, new in changed), "comments must survive"


def test_is_idempotent(pin: ModuleType, tmp_path: Path) -> None:
    path = write(tmp_path, VALUES)
    assert run(pin, path) == 0
    first = path.read_text(encoding="utf-8")
    assert run(pin, path) == 0
    assert path.read_text(encoding="utf-8") == first


def test_fails_without_an_image_block(
    pin: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = 'nameOverride: ""\ntoolbox:\n  image:\n    repository: x\n'
    path = write(tmp_path, text)
    assert run(pin, path) == 1
    assert "image" in capsys.readouterr().err
    assert path.read_text(encoding="utf-8") == text


@pytest.mark.parametrize("missing", ["repository", "tag", "digest"])
def test_fails_when_a_key_matches_nothing(
    pin: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    missing: str,
) -> None:
    text = "\n".join(
        line for line in VALUES.splitlines() if not line.startswith(f"  {missing}:")
    )
    path = write(tmp_path, text + "\n")
    assert run(pin, path) == 1
    assert f"image.{missing}" in capsys.readouterr().err
    assert path.read_text(encoding="utf-8") == text + "\n"


def test_fails_on_a_flow_mapping(pin: ModuleType, tmp_path: Path) -> None:
    text = "image: {repository: x, tag: y, digest: z}\n"
    path = write(tmp_path, text)
    assert run(pin, path) == 1
    assert path.read_text(encoding="utf-8") == text


def test_fails_on_a_duplicate_key(pin: ModuleType, tmp_path: Path) -> None:
    text = VALUES.replace('  tag: ""', '  tag: ""\n  tag: v0')
    path = write(tmp_path, text)
    assert run(pin, path) == 1
    assert path.read_text(encoding="utf-8") == text


@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--digest", "sha256:abc"),
        ("--digest", "d" * 64),
        ("--tag", ""),
        ("--tag", "v1 2"),
        ("--repository", "ghcr.io/example/gerrit:v1"),
        ("--repository", f"ghcr.io/example/gerrit@{DIGEST}"),
        ("--repository", ""),
    ],
)
def test_rejects_malformed_arguments(
    pin: ModuleType, tmp_path: Path, option: str, value: str
) -> None:
    path = write(tmp_path, VALUES)
    args = {"--repository": REPOSITORY, "--tag": TAG, "--digest": DIGEST}
    args[option] = value
    argv = ["--values", str(path)]
    for key, arg in args.items():
        argv += [key, arg]
    assert pin.main(argv) == 2
    assert path.read_text(encoding="utf-8") == VALUES


def test_missing_file_fails(pin: ModuleType, tmp_path: Path) -> None:
    assert run(pin, tmp_path / "absent.yaml") == 1


def test_command_line(tmp_path: Path) -> None:
    path = write(tmp_path, VALUES)
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--values",
            str(path),
            "--repository",
            REPOSITORY,
            "--tag",
            TAG,
            "--digest",
            DIGEST,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["image"]["digest"] == DIGEST


@pytest.mark.skipif(shutil.which("helm") is None, reason="needs helm")
def test_pinned_chart_renders_every_image_at_the_digest(
    pin: ModuleType, tmp_path: Path
) -> None:
    chart = tmp_path / "gerrit"
    shutil.copytree(CHART, chart)
    assert run(pin, chart / "values.yaml") == 0
    proc = subprocess.run(
        [
            "helm",
            "template",
            "gerrit",
            str(chart),
            *DEVELOPMENT_AUTH,
            "--set",
            "toolbox.enabled=true",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert check_digests(proc.stdout, DIGEST) == []
    assert f"{REPOSITORY}@{DIGEST}" in proc.stdout
