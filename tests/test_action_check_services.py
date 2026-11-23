# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for the bound on the service availability check.

Composite action steps cannot set ``timeout-minutes``, so the
**Check Gerrit service availability** step runs ``check-services.py``
under coreutils ``timeout``.  These tests run the step's inline
``run:`` script with bash against a stand-in ``check-services.py`` and
a stub ``docker``, and run the Setup step's validation of the
``check_service_timeout`` input.

They also check that every ``subprocess`` call reachable from
``check-services.py`` has a timeout and captures the child's stdout
and stderr, so no child can hold the step's output open after the
script exits.
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
ACTION_FILE = REPO_ROOT / "action.yaml"
SCRIPTS_DIR = REPO_ROOT / "scripts"
LIB_DIR = SCRIPTS_DIR / "lib"

CHECK_STEP = "Check Gerrit service availability"
SETUP_STEP = "Setup action/environment"

STUB_DOCKER = """#!/bin/sh
echo "docker $*" >> "$DOCKER_LOG"
case "$*" in
  *--format*) printf 'gerrit-primary\\nunrelated\\n' ;;
  logs*) echo "log line from $4" ;;
esac
"""
"""Records each call; lists one ``gerrit-*`` container and one other."""

needs_bash = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("timeout") is None,
    reason="the step runs under bash with coreutils timeout",
)


def _load_step(name: str) -> dict[str, Any]:
    action = yaml.safe_load(ACTION_FILE.read_text(encoding="utf-8"))
    for step in action["runs"]["steps"]:
        if step.get("name") == name:
            return dict(step)
    raise AssertionError(f"step {name!r} not found in action.yaml")


def _run_script(
    script: str, tmp_path: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "step.sh"
    path.write_text(script, encoding="utf-8")
    return subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(path)],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path), **env},
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def _run_check_step(
    tmp_path: Path, stand_in: str, bound: str
) -> tuple[subprocess.CompletedProcess[str], str]:
    action_path = tmp_path / "action"
    (action_path / "scripts").mkdir(parents=True)
    (action_path / "scripts" / "check-services.py").write_text(
        stand_in, encoding="utf-8"
    )
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    docker = stub_bin / "docker"
    docker.write_text(STUB_DOCKER, encoding="utf-8")
    docker.chmod(0o755)
    docker_log = tmp_path / "docker.log"
    docker_log.touch()

    step = _load_step(CHECK_STEP)
    assert step["env"]["CHECK_TIMEOUT"] == "${{ inputs.check_service_timeout }}"
    result = _run_script(
        str(step["run"]),
        tmp_path,
        {
            "PATH": f"{stub_bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "ACTION_PATH": str(action_path),
            "PYTHON": sys.executable,
            "CHECK_TIMEOUT": bound,
            "DOCKER_LOG": str(docker_log),
        },
    )
    return result, docker_log.read_text(encoding="utf-8")


@needs_bash
def test_timed_out_check_fails_with_error_and_diagnostics(tmp_path: Path) -> None:
    result, docker_calls = _run_check_step(
        tmp_path, "import time\ntime.sleep(60)\n", "2"
    )

    assert result.returncode == 124, result.stderr
    assert (
        "::error::Check Gerrit service availability did not finish "
        "within 2s; raise the check_service_timeout input"
    ) in result.stdout
    assert "docker ps -a\n" in docker_calls
    assert "docker logs --tail 200 gerrit-primary" in docker_calls
    assert "docker logs --tail 200 unrelated" not in docker_calls
    assert "log line from gerrit-primary" in result.stdout
    # Each diagnostic command must also be killed if it ignores SIGTERM.
    for line in str(_load_step(CHECK_STEP)["run"]).splitlines():
        if "timeout " in line and "docker" in line:
            assert "--kill-after=" in line, line


@needs_bash
def test_failing_check_keeps_its_exit_status(tmp_path: Path) -> None:
    result, docker_calls = _run_check_step(tmp_path, "import sys\nsys.exit(1)\n", "10")

    assert result.returncode == 1
    assert "::error::" not in result.stdout
    assert docker_calls == ""


@needs_bash
@pytest.mark.skipif(shutil.which("jq") is None, reason="Setup step uses jq")
@pytest.mark.parametrize("value", ["", "0", "abc", "-5", "1.5"])
def test_setup_rejects_invalid_check_service_timeout(
    tmp_path: Path, value: str
) -> None:
    step = _load_step(SETUP_STEP)
    assert (
        step["env"]["INPUT_CHECK_SERVICE_TIMEOUT"]
        == "${{ inputs.check_service_timeout }}"
    )
    result = _run_script(
        str(step["run"]),
        tmp_path,
        {
            "INPUT_GERRIT_SETUP": "[]",
            "INPUT_AUTH_TYPE": "ssh",
            "INPUT_SSH_PRIVATE_KEY": "stand-in",
            "INPUT_BASE_HTTP_PORT": "8080",
            "INPUT_BASE_SSH_PORT": "29418",
            "INPUT_FETCH_EVERY": "60s",
            "INPUT_CHECK_SERVICE_TIMEOUT": value,
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
            "GITHUB_ENV": str(tmp_path / "github-env"),
        },
    )

    assert result.returncode == 1
    assert (
        "::error::check_service_timeout must be a positive whole number"
    ) in result.stdout
    assert not (tmp_path / "github-env").exists()


def _local_imports(path: Path) -> set[Path]:
    """Return the ``scripts/lib`` modules *path* imports directly."""
    found: set[Path] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names = [node.module]
        for name in names:
            candidate = LIB_DIR / f"{name.split('.')[0]}.py"
            if candidate.is_file():
                found.add(candidate)
    return found


def _modules_reachable_from_check_services() -> set[Path]:
    pending = [SCRIPTS_DIR / "check-services.py"]
    seen: set[Path] = set()
    while pending:
        module = pending.pop()
        if module in seen:
            continue
        seen.add(module)
        pending.extend(_local_imports(module) - seen)
    return seen


def _is_none(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def _is_sys_stream(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "sys"
        and node.attr in {"stdout", "stderr", "__stdout__", "__stderr__"}
    )


def _inherits_stream(node: ast.expr) -> bool:
    """Return True when *node* hands the child the parent's stream.

    That is ``None``, a ``sys`` stream, its ``fileno()``, or a
    non-negative descriptor literal: 1 and 2 are the parent's own
    stdout and stderr.  The negative ``subprocess`` sentinels (PIPE,
    STDOUT, DEVNULL) do not inherit anything.
    """
    if _is_none(node) or _is_sys_stream(node):
        return True
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int)
        and not isinstance(node.value, bool)
    ):
        return node.value >= 0
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "fileno"
        and _is_sys_stream(node.func.value)
    )


def _subprocess_call_problems(node: ast.Call) -> list[str]:
    """Return why *node* may leave a child running or holding output.

    Values are checked as well as keyword names: ``timeout=None``
    disables the bound, and ``stdout=None`` or ``capture_output=False``
    lets the child inherit the step's output.
    """
    assert isinstance(node.func, ast.Attribute)
    problems: list[str] = []
    # Popen and friends return before the child exits.
    if node.func.attr != "run":
        problems.append(f"subprocess.{node.func.attr}")
    if any(kw.arg is None for kw in node.keywords):
        problems.append("**kwargs cannot be checked")
    keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg}

    timeout = keywords.get("timeout")
    if timeout is None or _is_none(timeout):
        problems.append("no timeout")
    elif isinstance(timeout, ast.Constant) and not (
        isinstance(timeout.value, (int, float))
        and not isinstance(timeout.value, bool)
        and timeout.value > 0
    ):
        problems.append("timeout is not a positive number")

    capture = keywords.get("capture_output")
    captured = isinstance(capture, ast.Constant) and capture.value is True
    if capture is not None and not isinstance(capture, ast.Constant):
        problems.append("capture_output is not a literal")
    for stream in ("stdout", "stderr"):
        value = keywords.get(stream)
        if value is None and not captured:
            problems.append(f"{stream} not captured")
        elif value is not None and _inherits_stream(value):
            problems.append(f"{stream} inherits the parent's stream")
    return problems


def _parse_call(source: str) -> ast.Call:
    node = ast.parse(source, mode="eval").body
    assert isinstance(node, ast.Call)
    return node


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("subprocess.run(c, capture_output=True, timeout=None)", "no timeout"),
        ("subprocess.run(c, capture_output=True)", "no timeout"),
        (
            "subprocess.run(c, capture_output=True, timeout=0)",
            "timeout is not a positive number",
        ),
        ("subprocess.run(c, timeout=None, capture_output=False)", "no timeout"),
        ("subprocess.run(c, timeout=5, capture_output=False)", "stdout not captured"),
        (
            "subprocess.run(c, timeout=5, stdout=None, stderr=None)",
            "stdout inherits the parent's stream",
        ),
        (
            "subprocess.run(c, timeout=5, stdout=subprocess.PIPE, stderr=sys.stderr)",
            "stderr inherits the parent's stream",
        ),
        (
            "subprocess.run(c, timeout=5, capture_output=True, stdout=None)",
            "stdout inherits the parent's stream",
        ),
        (
            "subprocess.run(c, timeout=5, stdout=1, stderr=subprocess.PIPE)",
            "stdout inherits the parent's stream",
        ),
        (
            "subprocess.run(c, timeout=5, stdout=subprocess.PIPE, stderr=2)",
            "stderr inherits the parent's stream",
        ),
        (
            "subprocess.run(c, timeout=5, stdout=subprocess.PIPE, "
            "stderr=sys.stderr.fileno())",
            "stderr inherits the parent's stream",
        ),
        (
            "subprocess.run(c, timeout=5, capture_output=flag)",
            "capture_output is not a literal",
        ),
        ("subprocess.run(c, **opts)", "**kwargs cannot be checked"),
        (
            "subprocess.Popen(c, capture_output=True, timeout=5)",
            "subprocess.Popen",
        ),
    ],
)
def test_subprocess_guard_rejects_unsafe_calls(source: str, expected: str) -> None:
    assert expected in _subprocess_call_problems(_parse_call(source))


@pytest.mark.parametrize(
    "source",
    [
        "subprocess.run(c, capture_output=True, text=True, timeout=timeout)",
        "subprocess.run(c, capture_output=True, timeout=10)",
        "subprocess.run(c, stdout=subprocess.PIPE, "
        "stderr=subprocess.STDOUT, timeout=2.5)",
        "subprocess.run(c, stdout=subprocess.DEVNULL, "
        "stderr=subprocess.DEVNULL, timeout=t + 5)",
        "subprocess.run(c, stdout=-1, stderr=-2, timeout=5)",
    ],
)
def test_subprocess_guard_accepts_safe_calls(source: str) -> None:
    assert _subprocess_call_problems(_parse_call(source)) == []


def test_check_services_children_are_bounded_and_captured() -> None:
    modules = _modules_reachable_from_check_services()
    assert LIB_DIR / "docker_cli.py" in modules
    assert LIB_DIR / "health_check_network.py" in modules

    calls: list[str] = []
    problems: list[str] = []
    for module in sorted(modules):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "subprocess"
            ):
                continue
            where = f"{module.relative_to(REPO_ROOT)}:{node.lineno}"
            calls.append(where)
            problems.extend(
                f"{where}: {problem}" for problem in _subprocess_call_problems(node)
            )

    assert calls, "expected subprocess calls under check-services.py"
    assert problems == []
