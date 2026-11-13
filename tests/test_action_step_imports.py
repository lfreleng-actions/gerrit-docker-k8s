# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Run inline ``action.yaml`` Python steps from a populated workspace.

Composite action steps start in ``$GITHUB_WORKSPACE``, which holds
whatever the caller checked out.  ``python3 -m``, ``python3 -c`` and
code read from stdin put the working directory first on ``sys.path``,
so a ``pip/``, ``nacl/``, ``json.py`` or ``pathlib.py`` there would be
imported in place of the real module.

These tests extract the inline ``run:`` scripts from ``action.yaml``
and run them with bash from a workspace holding such files.  Each
planted file records its name in ``$RUNNER_TEMP/planted-ran`` and
exits non-zero.  A ``python3`` and ``python`` placed first on ``PATH``
do the same, standing in for an interpreter the caller chose.  The
steps must succeed and the marker must not exist.

``$PYTHON``, the action's interpreter, is a throwaway virtual
environment here.  ``pip`` and ``nacl`` are stubbed in its
site-packages, so nothing is downloaded and the stub stands in for the
real packages wherever the step resolves them correctly.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
"""Repository root; also the action path the steps run against."""

ACTION_FILE = REPO_ROOT / "action.yaml"

PLANTED_BODY = (
    "import os, sys\n"
    "with open(os.path.join(os.environ['RUNNER_TEMP'], 'planted-ran'),"
    " 'a') as fh:\n"
    "    fh.write(__name__ + '\\n')\n"
    "sys.exit(97)\n"
)
"""Body of every planted workspace module."""

PLANTED_FILES = (
    "pip/__init__.py",
    "pip/__main__.py",
    "nacl/__init__.py",
    "nacl/public.py",
    "json.py",
    "pathlib.py",
    "sitecustomize.py",
)

PLANTED_INTERPRETER = '#!/bin/sh\necho "$0" >> "$RUNNER_TEMP/planted-ran"\nexit 97\n'
"""A ``python3``/``python`` on the caller's ``PATH``; never to be run."""

STUB_PIP_MAIN = (
    "import os, sys\n"
    "with open(os.path.join(os.environ['RUNNER_TEMP'], 'pip-args'),"
    " 'w') as fh:\n"
    "    fh.write('\\n'.join(sys.argv[1:]))\n"
    "with open(os.path.join(os.environ['RUNNER_TEMP'], 'pip-config-file'),"
    " 'w') as fh:\n"
    "    fh.write(os.environ.get('PIP_CONFIG_FILE', ''))\n"
)

PIP_INSTALL_STEPS = (
    "Setup action Python environment",
    "Install PyNaCl (provision mode)",
)
"""Steps that run ``pip install`` and so must ignore the caller's pip config."""

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="action steps run under bash"
)


def _load_step(name: str) -> dict[str, Any]:
    """Return the composite step called *name* from ``action.yaml``."""
    action = yaml.safe_load(ACTION_FILE.read_text(encoding="utf-8"))
    for step in action["runs"]["steps"]:
        if step.get("name") == name:
            return dict(step)
    raise AssertionError(f"step {name!r} not found in action.yaml")


def _step_env(step: dict[str, Any], python: Path) -> dict[str, str]:
    """Resolve the step's ``env:`` block for a local run."""
    known = {
        "${{ github.action_path }}": str(REPO_ROOT),
        "${{ steps.action-python.outputs.python-path }}": str(python),
    }
    resolved: dict[str, str] = {}
    for key, value in (step.get("env") or {}).items():
        text = str(value)
        if "${{" in text:
            assert text in known, f"no local value for {key}={text}"
            text = known[text]
        resolved[str(key)] = text
    return resolved


@pytest.fixture()
def sandbox(tmp_path: Path) -> dict[str, Path]:
    """Create a planted workspace, runner temp and stub virtualenv."""
    workspace = tmp_path / "workspace"
    runner_temp = tmp_path / "runner-temp"
    caller_bin = tmp_path / "caller-bin"
    for directory in (workspace, runner_temp, caller_bin):
        directory.mkdir()

    for name in ("python3", "python"):
        decoy = caller_bin / name
        decoy.write_text(PLANTED_INTERPRETER, encoding="utf-8")
        decoy.chmod(0o755)

    for rel in PLANTED_FILES:
        path = workspace / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(PLANTED_BODY, encoding="utf-8")

    venv = tmp_path / "venv"
    subprocess.run(
        [sys.executable, "-I", "-m", "venv", "--without-pip", str(venv)],
        cwd=runner_temp,
        check=True,
    )
    python = venv / "bin" / "python"
    probe = subprocess.run(
        [
            str(python),
            "-I",
            "-c",
            "import sysconfig; print(sysconfig.get_path('purelib'))",
        ],
        cwd=runner_temp,
        capture_output=True,
        text=True,
        check=True,
    )
    site_packages = Path(probe.stdout.strip())

    (site_packages / "pip").mkdir(parents=True)
    (site_packages / "pip" / "__init__.py").write_text("", encoding="utf-8")
    (site_packages / "pip" / "__main__.py").write_text(STUB_PIP_MAIN, encoding="utf-8")
    (site_packages / "nacl").mkdir()
    (site_packages / "nacl" / "__init__.py").write_text("", encoding="utf-8")
    (site_packages / "nacl" / "public.py").write_text("", encoding="utf-8")

    return {
        "workspace": workspace,
        "runner_temp": runner_temp,
        "python": python,
        "caller_bin": caller_bin,
        "tmp": tmp_path,
    }


def _run_step(
    step: dict[str, Any],
    sandbox: dict[str, Path],
    extra_env: dict[str, str],
    pythonpath: str | None,
) -> subprocess.CompletedProcess[str]:
    """Run the step's script the way the runner does for ``shell: bash``."""
    assert step.get("shell") == "bash"
    script = sandbox["tmp"] / "step.sh"
    script.write_text(str(step["run"]), encoding="utf-8")
    env = {
        "PATH": f"{sandbox['caller_bin']}{os.pathsep}{os.environ.get('PATH', '')}",
        "HOME": str(sandbox["tmp"]),
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        # The caller's own pip config; a step's env: takes precedence.
        "PIP_CONFIG_FILE": str(sandbox["tmp"] / "caller-pip.conf"),
        "RUNNER_TEMP": str(sandbox["runner_temp"]),
        "GITHUB_WORKSPACE": str(sandbox["workspace"]),
        "GITHUB_ACTION_PATH": str(REPO_ROOT),
        **_step_env(step, sandbox["python"]),
        **extra_env,
    }
    if pythonpath is not None:
        env["PYTHONPATH"] = pythonpath
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
        cwd=sandbox["workspace"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _planted_ran(sandbox: dict[str, Path]) -> str:
    marker = sandbox["runner_temp"] / "planted-ran"
    return marker.read_text(encoding="utf-8") if marker.exists() else ""


# An empty PYTHONPATH entry (":") also adds the working directory and
# makes Python run a sitecustomize.py found there at start-up.
PYTHONPATH_CASES = [None, ":"]


@pytest.mark.parametrize("pythonpath", PYTHONPATH_CASES)
def test_pynacl_step_ignores_workspace_modules(
    sandbox: dict[str, Path], pythonpath: str | None
) -> None:
    step = _load_step("Install PyNaCl (provision mode)")
    result = _run_step(step, sandbox, {}, pythonpath)

    assert _planted_ran(sandbox) == ""
    assert result.returncode == 0, result.stderr
    assert "PyNaCl OK" in result.stdout
    pip_args = (sandbox["runner_temp"] / "pip-args").read_text(encoding="utf-8")
    assert pip_args.splitlines() == [
        "install",
        "--isolated",
        "--disable-pip-version-check",
        "--upgrade",
        "PyNaCl>=1.5,<2",
    ]
    pip_config = (sandbox["runner_temp"] / "pip-config-file").read_text(
        encoding="utf-8"
    )
    assert pip_config == os.devnull


@pytest.mark.parametrize("name", PIP_INSTALL_STEPS)
def test_pip_install_steps_skip_pip_config(name: str) -> None:
    # pip --isolated still reads PIP_CONFIG_FILE; os.devnull makes pip
    # load no config file, so a caller's install target cannot apply.
    step = _load_step(name)
    assert "--isolated" in str(step["run"])
    assert (step.get("env") or {}).get("PIP_CONFIG_FILE") == os.devnull


@pytest.mark.parametrize("pythonpath", PYTHONPATH_CASES)
def test_persist_step_ignores_workspace_modules(
    sandbox: dict[str, Path], pythonpath: str | None
) -> None:
    work_dir = sandbox["tmp"] / "work"
    work_dir.mkdir()
    (work_dir / "instances.json").write_text(
        json.dumps({"persistent": {"cid": "0123abcd"}}), encoding="utf-8"
    )
    summary = sandbox["tmp"] / "summary.md"
    summary.touch()

    step = _load_step("Containers will continue/persist")
    result = _run_step(
        step,
        sandbox,
        {"WORK_DIR": str(work_dir), "GITHUB_STEP_SUMMARY": str(summary)},
        pythonpath,
    )

    assert _planted_ran(sandbox) == ""
    assert result.returncode == 0, result.stderr
    assert "docker kill 0123abcd" in result.stdout
    assert "docker kill 0123abcd" in summary.read_text(encoding="utf-8")
