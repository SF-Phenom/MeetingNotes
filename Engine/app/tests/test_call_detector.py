"""Tests for `call_detector.is_call_still_active` Zoom AX probe path.

The other branches (browser via process check, Slack/Webex/FaceTime via
window title) drive AppleScript or psutil and are exercised on the live
system; only the new zoom-observer probe wiring is unit-testable in
isolation. Tests pin the contract between `_zoom_in_meeting` and the
exit codes documented in `Engine/ZoomObserver/Sources/main.swift` so a
drift on either side fails here instead of silently breaking auto-stop.
"""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from app import call_detector


@pytest.fixture
def fake_zoom_bin(monkeypatch, tmp_path):
    """Stand in for ZOOM_OBSERVER_BIN so probe-path tests don't depend
    on the real binary or its AX permission state."""
    fake = tmp_path / "zoom-observer"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setattr(call_detector, "ZOOM_OBSERVER_BIN", str(fake))
    return fake


def _stub_run(returncode: int):
    def runner(*args, **kwargs):
        return SimpleNamespace(returncode=returncode, stdout=b"", stderr=b"")
    return runner


class TestZoomInMeetingProbe:
    def test_in_meeting_returns_true(self, monkeypatch, fake_zoom_bin):
        monkeypatch.setattr(subprocess, "run", _stub_run(0))
        assert call_detector._zoom_in_meeting() is True

    def test_not_in_meeting_returns_false(self, monkeypatch, fake_zoom_bin):
        monkeypatch.setattr(subprocess, "run", _stub_run(11))
        assert call_detector._zoom_in_meeting() is False

    def test_zoom_not_running_returns_false(self, monkeypatch, fake_zoom_bin):
        monkeypatch.setattr(subprocess, "run", _stub_run(12))
        assert call_detector._zoom_in_meeting() is False

    def test_ax_denied_falls_back_to_true(self, monkeypatch, fake_zoom_bin):
        # AX denied (10) is a real failure mode — fall back rather than
        # auto-stop a recording the user can't get the probe to see.
        monkeypatch.setattr(subprocess, "run", _stub_run(10))
        assert call_detector._zoom_in_meeting() is True

    def test_unexpected_exit_falls_back_to_true(self, monkeypatch, fake_zoom_bin):
        monkeypatch.setattr(subprocess, "run", _stub_run(99))
        assert call_detector._zoom_in_meeting() is True

    def test_timeout_falls_back_to_true(self, monkeypatch, fake_zoom_bin):
        def boom(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args[0], timeout=5.0)
        monkeypatch.setattr(subprocess, "run", boom)
        assert call_detector._zoom_in_meeting() is True

    def test_oserror_falls_back_to_true(self, monkeypatch, fake_zoom_bin):
        def boom(*args, **kwargs):
            raise OSError("bin gone")
        monkeypatch.setattr(subprocess, "run", boom)
        assert call_detector._zoom_in_meeting() is True

    def test_missing_binary_falls_back_to_true(self, monkeypatch, tmp_path):
        # Binary path that doesn't exist — should never invoke subprocess.
        monkeypatch.setattr(
            call_detector, "ZOOM_OBSERVER_BIN", str(tmp_path / "nope")
        )

        def fail_if_called(*args, **kwargs):
            raise AssertionError("subprocess.run should not run when bin is missing")

        monkeypatch.setattr(subprocess, "run", fail_if_called)
        assert call_detector._zoom_in_meeting() is True


class TestIsCallStillActiveZoom:
    def test_zoom_routes_to_probe_when_running(self, monkeypatch):
        monkeypatch.setattr(call_detector, "_process_running", lambda name: True)
        monkeypatch.setattr(call_detector, "_zoom_in_meeting", lambda: False)
        assert call_detector.is_call_still_active("zoom") is False

    def test_zoom_returns_false_when_process_dead(self, monkeypatch):
        monkeypatch.setattr(call_detector, "_process_running", lambda name: False)
        # Probe must not be called once the process is gone — guard it.
        monkeypatch.setattr(
            call_detector,
            "_zoom_in_meeting",
            lambda: pytest.fail("probe ran despite zoom process gone"),
        )
        assert call_detector.is_call_still_active("zoom") is False

    def test_zoom_returns_true_when_in_meeting(self, monkeypatch):
        monkeypatch.setattr(call_detector, "_process_running", lambda name: True)
        monkeypatch.setattr(call_detector, "_zoom_in_meeting", lambda: True)
        assert call_detector.is_call_still_active("zoom") is True
