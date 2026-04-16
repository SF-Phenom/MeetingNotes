"""Tests for UpdateChecker — git fetch/compare/pull orchestration."""
from __future__ import annotations

import subprocess
import time

import pytest

from app import update_checker as uc_mod
from app.ui_bridge import UIBridge
from app.update_checker import UpdateChecker


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture
def capture_ui():
    """Collect calls made to the four main-thread callbacks."""
    log: dict[str, list] = {
        "notify": [],
        "prompt_install": [],
        "show_alert": [],
        "restart": [],
    }

    def notify(subtitle: str, message: str) -> None:
        log["notify"].append((subtitle, message))

    def prompt_install() -> None:
        log["prompt_install"].append(None)

    def show_alert(title: str, message: str) -> None:
        log["show_alert"].append((title, message))

    def restart() -> None:
        log["restart"].append(None)

    return log, notify, prompt_install, show_alert, restart


def _wait_until_drained(bridge, predicate, timeout=2.0):
    """Drain the bridge then check predicate in a loop with a hard timeout.

    UpdateChecker's worker dispatches callbacks onto the bridge from a bg
    thread; they don't take effect in our log until something calls drain().
    We do both in the same polling loop so the test doesn't race the worker.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        bridge.drain()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _make_checker(capture_ui, bridge=None):
    log, notify, prompt_install, show_alert, restart = capture_ui
    return log, UpdateChecker(
        base_dir="/tmp/does-not-matter-for-mock",
        ui_bridge=bridge or UIBridge(),
        notify=notify,
        prompt_install=prompt_install,
        show_alert=show_alert,
        restart=restart,
    )


class TestCheck:
    def test_up_to_date_notifies(self, monkeypatch, capture_ui):
        def fake_run(args, **kwargs):
            # First: fetch (no output), then rev-parse HEAD, branch, origin.
            if "rev-parse" in args and "HEAD" in args:
                return _FakeCompleted(0, "abc123\n")
            if "--abbrev-ref" in args:
                return _FakeCompleted(0, "main\n")
            if "rev-parse" in args:  # origin/main
                return _FakeCompleted(0, "abc123\n")
            return _FakeCompleted(0, "")
        monkeypatch.setattr(subprocess, "run", fake_run)

        bridge = UIBridge()
        log, checker = _make_checker(capture_ui, bridge=bridge)
        checker.check()
        # Wait for the bg thread to finish dispatching.
        assert _wait_until_drained(bridge, lambda: len(log["notify"]) + len(log["prompt_install"]) > 0)
        bridge.drain()
        assert log["notify"] == [
            ("Up to date", "You're running the latest version."),
        ]
        assert log["prompt_install"] == []

    def test_update_available_prompts(self, monkeypatch, capture_ui):
        def fake_run(args, **kwargs):
            if "rev-parse" in args and "HEAD" in args:
                return _FakeCompleted(0, "local-sha\n")
            if "--abbrev-ref" in args:
                return _FakeCompleted(0, "main\n")
            if "rev-parse" in args:
                return _FakeCompleted(0, "remote-sha\n")
            return _FakeCompleted(0, "")
        monkeypatch.setattr(subprocess, "run", fake_run)

        bridge = UIBridge()
        log, checker = _make_checker(capture_ui, bridge=bridge)
        checker.check()
        assert _wait_until_drained(bridge, lambda: len(log["notify"]) + len(log["prompt_install"]) > 0)
        bridge.drain()
        assert log["prompt_install"] == [None]
        assert log["notify"] == []

    def test_fetch_error_notifies(self, monkeypatch, capture_ui):
        def boom(args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args, timeout=30)
        monkeypatch.setattr(subprocess, "run", boom)

        bridge = UIBridge()
        log, checker = _make_checker(capture_ui, bridge=bridge)
        checker.check()
        assert _wait_until_drained(bridge, lambda: len(log["notify"]) > 0)
        bridge.drain()
        assert log["notify"][0][0] == "Update check failed"


class TestApply:
    def test_pull_success_triggers_restart(self, monkeypatch, capture_ui):
        monkeypatch.setattr(
            subprocess, "run",
            lambda args, **kwargs: _FakeCompleted(0, ""),
        )
        bridge = UIBridge()
        log, checker = _make_checker(capture_ui, bridge=bridge)
        checker.apply()
        assert _wait_until_drained(bridge, lambda: len(log["restart"]) > 0)
        bridge.drain()
        assert log["restart"] == [None]

    def test_pull_nonzero_shows_alert(self, monkeypatch, capture_ui):
        monkeypatch.setattr(
            subprocess, "run",
            lambda args, **kwargs: _FakeCompleted(1, "", "conflict"),
        )
        bridge = UIBridge()
        log, checker = _make_checker(capture_ui, bridge=bridge)
        checker.apply()
        assert _wait_until_drained(bridge, lambda: len(log["show_alert"]) > 0)
        bridge.drain()
        assert log["show_alert"][0][0] == "Update Failed"
        assert log["restart"] == []

    def test_pull_exception_shows_alert(self, monkeypatch, capture_ui):
        def boom(args, **kwargs):
            raise OSError("fake-error")
        monkeypatch.setattr(subprocess, "run", boom)

        bridge = UIBridge()
        log, checker = _make_checker(capture_ui, bridge=bridge)
        checker.apply()
        assert _wait_until_drained(bridge, lambda: len(log["show_alert"]) > 0)
        bridge.drain()
        assert log["show_alert"][0][0] == "Update Failed"
        assert "fake-error" in log["show_alert"][0][1]
        assert log["restart"] == []
