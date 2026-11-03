# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Golden tests for the configuration files the Gerrit chart renders.

Renders the chart with ``helm template`` and each values file under
``K8S/charts/gerrit/examples/``, and compares the ``<fullname>-config``
ConfigMap data with ``tests/chart/golden/<example>/<file>``; an example
that enables replication also has a ``replication.config`` golden, and
one with the healthcheck plugin on, as by default, a
``healthcheck.config`` golden.
Each golden starts with the two SPDX comment lines, which the
comparison skips. Also checks that the NetworkPolicies let the
configured mail relay through.

After an intended change to the rendered output, regenerate the
goldens with ``UPDATE_GOLDEN=1 uv run pytest tests/chart`` and review
the diff.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "K8S" / "charts" / "gerrit"
EXAMPLES = CHART / "examples"
GOLDEN = Path(__file__).parent / "golden"
CONFIG_FILES = ("gerrit.config", "jgit.config", "notedb.config", "log4j.properties")
# Rendered only when a feature enables it; compared where the example's
# golden directory holds one
OPTIONAL_CONFIG_FILES = ("healthcheck.config", "replication.config")
# REUSE-IgnoreStart
GOLDEN_HEADER = (
    "# SPDX-License-Identifier: Apache-2.0\n"
    "# SPDX-FileCopyrightText: 2026 The Linux Foundation\n"
)
# REUSE-IgnoreEnd
EXAMPLE_NAMES = sorted(p.name for p in GOLDEN.iterdir() if p.is_dir())
# The chart has no default image tag, so every render names one
IMAGE_TAG = ("--set", "image.tag=v0.0.0")
# Nor a default authentication: renders that do not choose one use
# development accounts from this values file. It goes before a test's
# own values files, which override it.
DEVELOPMENT_AUTH = ("-f", str(Path(__file__).with_name("values-development-auth.yaml")))

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="needs helm")


def helm(*args: str, auth: bool = True) -> str:
    """Run helm and return its standard output, failing on an error.

    ``template`` and ``lint`` get :data:`IMAGE_TAG` added and, unless
    *auth* is false, :data:`DEVELOPMENT_AUTH` ahead of *args*.
    """
    command = ["helm", *args]
    if args[:1] in (("template",), ("lint",)):
        base = (*DEVELOPMENT_AUTH, *IMAGE_TAG) if auth else IMAGE_TAG
        command = ["helm", args[0], *base, *args[1:]]
    proc = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def render_config(values: Path) -> dict[str, str]:
    """Return the data of the rendered gerrit-config ConfigMap."""
    out = helm(
        "template",
        "gerrit",
        str(CHART),
        "--namespace",
        "gerrit-test",
        "-f",
        str(values),
        "--show-only",
        "templates/configmaps.yaml",
    )
    docs: list[Any] = [d for d in yaml.safe_load_all(out) if d]
    found = [d for d in docs if d["metadata"]["name"] == "gerrit-config"]
    assert len(found) == 1, [d["metadata"]["name"] for d in docs]
    data: dict[str, str] = found[0]["data"]
    return data


def egress_ports(*args: str) -> set[int]:
    """Return the TCP egress ports the chart's NetworkPolicies allow."""
    out = helm(
        "template",
        "gerrit",
        str(CHART),
        "--namespace",
        "gerrit-test",
        *args,
        "--show-only",
        "templates/networkpolicies.yaml",
    )
    return {
        port["port"]
        for doc in yaml.safe_load_all(out)
        if doc
        for rule in doc["spec"].get("egress", [])
        for port in rule.get("ports", [])
        if port["protocol"] == "TCP"
    }


def read_golden(path: Path) -> str:
    """Return a golden file's content without its SPDX header."""
    text = path.read_text(encoding="utf-8")
    assert text.startswith(GOLDEN_HEADER), f"{path} lacks the SPDX header"
    return text[len(GOLDEN_HEADER) :]


def test_every_example_has_goldens() -> None:
    examples = sorted(p.stem for p in EXAMPLES.glob("*.yaml"))
    assert examples == EXAMPLE_NAMES
    assert len(examples) >= 3


def config_files(example: str) -> tuple[str, ...]:
    """Return the config files an example renders, per its goldens."""
    extra = tuple(n for n in OPTIONAL_CONFIG_FILES if (GOLDEN / example / n).exists())
    return CONFIG_FILES + extra


def update_optional_goldens(example: str, data: dict[str, str]) -> None:
    """Create or remove an example's optional goldens to match *data*."""
    for name in OPTIONAL_CONFIG_FILES:
        golden = GOLDEN / example / name
        if name in data:
            golden.write_text(GOLDEN_HEADER + data[name], encoding="utf-8")
        else:
            golden.unlink(missing_ok=True)


def test_update_creates_and_removes_optional_goldens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys.modules[__name__], "GOLDEN", tmp_path)
    (tmp_path / "ex").mkdir()
    update_optional_goldens("ex", {"replication.config": "[gerrit]\n"})
    assert config_files("ex") == (*CONFIG_FILES, "replication.config")
    assert read_golden(tmp_path / "ex" / "replication.config") == "[gerrit]\n"
    update_optional_goldens("ex", {})
    assert config_files("ex") == CONFIG_FILES


@pytest.mark.parametrize("example", EXAMPLE_NAMES)
def test_rendered_config_matches_golden(example: str) -> None:
    data = render_config(EXAMPLES / f"{example}.yaml")
    if os.environ.get("UPDATE_GOLDEN"):
        update_optional_goldens(example, data)
    names = config_files(example)
    assert sorted(data) == sorted(names)
    for name in names:
        golden = GOLDEN / example / name
        if os.environ.get("UPDATE_GOLDEN"):
            golden.write_text(GOLDEN_HEADER + data[name], encoding="utf-8")
        assert data[name] == read_golden(golden), f"{example}/{name}"


@pytest.mark.parametrize("example", EXAMPLE_NAMES)
def test_rendered_config_parses_as_git_config(example: str, tmp_path: Path) -> None:
    data = render_config(EXAMPLES / f"{example}.yaml")
    # log4j.properties is a Java properties file, not a git config
    for name in (n for n in data if n.endswith(".config")):
        path = tmp_path / name
        path.write_text(data[name], encoding="utf-8")
        proc = subprocess.run(
            ["git", "config", "-f", str(path), "--list"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"{example}/{name}: {proc.stderr}"


def test_secrets_stay_out_of_the_rendered_config() -> None:
    data = render_config(EXAMPLES / "values-one-segment-prefix.yaml")
    config = data["gerrit.config"].lower()
    assert "registeremailprivatekey" not in config
    assert "smtppass" not in config


def test_no_smtp_egress_without_mail() -> None:
    assert egress_ports() == {53, 443}


def test_smtp_egress_uses_the_configured_port() -> None:
    ports = egress_ports("-f", str(EXAMPLES / "values-one-segment-prefix.yaml"))
    assert 587 in ports


@pytest.mark.parametrize(("encryption", "port"), [("none", 25), ("ssl", 465)])
def test_smtp_egress_defaults_to_gerrits_port(encryption: str, port: int) -> None:
    ports = egress_ports(
        "--set",
        "sendemail.enable=true",
        "--set",
        "sendemail.smtpServer=smtp.example.org",
        "--set",
        f"sendemail.smtpEncryption={encryption}",
    )
    assert port in ports


@pytest.mark.parametrize("example", EXAMPLE_NAMES)
def test_helm_lint(example: str) -> None:
    helm("lint", str(CHART), "-f", str(EXAMPLES / f"{example}.yaml"))


def image_of(*args: str) -> subprocess.CompletedProcess[str]:
    """Render the StatefulSet with *args* only, without :data:`IMAGE_TAG`."""
    return subprocess.run(
        [
            "helm",
            "template",
            "gerrit",
            str(CHART),
            *DEVELOPMENT_AUTH,
            *args,
            "--show-only",
            "templates/statefulset.yaml",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_image_needs_a_tag_or_digest() -> None:
    proc = image_of()
    assert proc.returncode != 0
    assert "set image.tag" in proc.stderr


def test_image_reference_uses_the_tag_or_the_digest() -> None:
    repo = "ghcr.io/lfreleng-actions/gerrit-docker-k8s/gerrit"
    digest = "sha256:" + "c" * 64
    tagged = image_of("--set", "image.tag=v1.2.3")
    assert tagged.returncode == 0, tagged.stderr
    assert f"image: {repo}:v1.2.3\n" in tagged.stdout
    pinned = image_of("--set", "image.tag=v1.2.3", "--set", f"image.digest={digest}")
    assert pinned.returncode == 0, pinned.stderr
    assert f"image: {repo}@{digest}\n" in pinned.stdout
    assert ":v1.2.3" not in pinned.stdout
