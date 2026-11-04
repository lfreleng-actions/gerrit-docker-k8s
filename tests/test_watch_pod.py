# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation

"""Tests for ``tests/k8s/watch_pod.py``, which the import legs wait on."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

WATCH = Path(__file__).parent / "k8s" / "watch_pod.py"


@pytest.fixture(scope="module")
def watch() -> ModuleType:
    spec = importlib.util.spec_from_file_location("watch_pod", WATCH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pod(
    init: list[dict[str, Any]] | None = None,
    main: list[dict[str, Any]] | None = None,
    ready: bool = False,
) -> dict[str, Any]:
    return {
        "status": {
            "initContainerStatuses": init or [],
            "containerStatuses": main or [],
            "conditions": [{"type": "Ready", "status": str(ready)}],
        }
    }


DONE = {"name": "site-import", "state": {"terminated": {"exitCode": 0}}}
RUNNING = {"name": "gerrit", "state": {"running": {}}, "restartCount": 0}


def test_ready(watch: ModuleType) -> None:
    assert watch.verdict(pod([DONE], [RUNNING], ready=True)) == ("ready", "")


def test_still_starting(watch: ModuleType) -> None:
    starting = {"name": "site-init", "state": {"running": {}}}
    assert watch.verdict(pod([DONE, starting])) == ("waiting", "")
    assert watch.verdict({}) == ("waiting", "")


@pytest.mark.parametrize(
    ("init", "main", "message"),
    [
        (
            [
                {
                    "name": "site-import",
                    "state": {"waiting": {"reason": "PodInitializing"}},
                    "lastState": {"terminated": {"exitCode": 1, "reason": "Error"}},
                }
            ],
            [],
            "init container site-import exited 1 (Error)",
        ),
        (
            [
                {
                    "name": "site-init",
                    "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                }
            ],
            [],
            "init container site-init is in CrashLoopBackOff",
        ),
        (
            [DONE],
            [
                {
                    "name": "gerrit",
                    "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                    "lastState": {"terminated": {"exitCode": 1, "reason": "Error"}},
                    "restartCount": 2,
                }
            ],
            "container gerrit exited 1 (Error)",
        ),
        (
            [DONE],
            [{"name": "gerrit", "state": {"running": {}}, "restartCount": 1}],
            "container gerrit restarted 1 times",
        ),
    ],
)
def test_fails_fast(
    watch: ModuleType,
    init: list[dict[str, Any]],
    main: list[dict[str, Any]],
    message: str,
) -> None:
    assert watch.verdict(pod(init, main)) == ("failed", message)


def test_main_times_out(watch: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(watch, "get_pod", lambda _ns, _pod: None)
    assert watch.main(["--namespace", "n", "--pod", "p", "--timeout", "0"]) == 1


def test_main_returns_on_failure(
    watch: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    crashing = {"name": "gerrit", "state": {"running": {}}, "restartCount": 3}
    monkeypatch.setattr(watch, "get_pod", lambda _ns, _pod: pod([DONE], [crashing]))
    assert watch.main(["--namespace", "n", "--pod", "p", "--interval", "0"]) == 1
